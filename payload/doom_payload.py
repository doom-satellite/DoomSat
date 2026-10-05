"""Doom payload: the "instrument" the flight software carries.

Runs Doom (via ViZDoom) at 35 Hz, keeps the last uplinked controls held, and serves an observation
summary plus JPEG frames (and the map it builds) to the F Prime Doom component over a local TCP socket.

Everything the payload reports comes from what a player could see, never from the level file:
  - the in-game automap in the mode that shows only lines the player has already seen. ZDoom draws
    those lines by category (wall, floor step, ceiling change such as a door, locked door in the
    colour of its key, exit line once looked at); the payload only recolours them so code can read
    the categories off the pixels,
  - the depth buffer (a range camera): free space ahead, and the map cells swept clean of unknown,
  - the labels buffer (an object detector): visible enemies, pickups and keys, remembered once seen,
  - the HUD variables: health, armor, ammo, position and heading (odometry).

There is no route planner. The payload reports, for the four directions around the player, how far
the way is open and how much of that ground has already been walked, what is at arm's length ahead
(a wall, a door, the exit switch, a locked door, something the map does not show), where an exit
line or a key or a pickup was seen, and whether the player is stuck. The ground decides where to go.

Protocol (big-endian; the payload is the server on 127.0.0.1:4242):
  payload -> flight   'D' kind:u8 length:u16 body
     kind 1 STATUS  fixed struct (see STATUS_FMT)
     kind 2 FRAME   seq:u32 jpeg bytes (seq with the high bit set: the map, a PNG)
     kind 3 WAD     what became of a LOAD_WAD; also sent on connect (payload/wad_uplink.py)
  flight -> payload   'D' kind:u8 length:u16 body
     kind 0x10 CONTROL      move:i8 strafe:i8 turn:f32 (degrees to turn, +left) fire:u8 use:u8 weapon:u8
     kind 0x11 SET_GOAL     goal:u8
     kind 0x12 RESET
     kind 0x13 FRAME_RATE   hz:u8 quality:u8
     kind 0x14 EXPLORE_HINT bearing:i16 (degrees, positive left) ttl:u8 (seconds)
     kind 0x15 INTENT       see INTENT_FMT
     kind 0x16 LOAD_WAD     iwad, pwad, map (payload/wad_uplink.py)
"""
import argparse
import io
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque

import numpy as np
import vizdoom as vzd
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import executor as ex_mod            # noqa: E402  the onboard executor (charter 3.1)
import seen_geometry as geom_mod     # noqa: E402  exact lines, gated on the automap having drawn them
import world_model as wm_mod         # noqa: E402  frontiers, objects, the planner (charter 3.2)
import wad_uplink as wu              # noqa: E402  LOAD_WAD: its link records and which names may load

TICRATE = 35
STATUS_FMT = "!hhhhBBHfffBfHHHHHHHHBBBBBHfHfHHHHfffBBBIHBBHBBBhHHHHBBBBBBBBBBBB"   # 120 bytes, 64 fields (see pack_status)
# Charter 3.3: the ground scores candidate targets, and building the list needs the map, which is onboard.
# Eight is what the charter prunes to; each is (kind, bearing, path distance, novelty, flags).
MAX_CANDIDATES = 8
# (kind, world x, world y, path distance, novelty, flags). The world position rather than a bearing,
# because the ground has to be able to aim an intent at the place itself: a bearing plus a path distance
# does not locate anything once the route bends, and the index alone is not safe because the list is
# rebuilt while an intent is in flight.
CAND_FMT = "BffHBBBBHHB"
# What is threatening the PLAYER, as against what is near a candidate: the worst visible monster
# class and how many are in view. Facts; how dangerous that is comes from the knowledge file on the
# ground (charter 4, the `engage` head).
THREAT_FMT = "BB"
# Presses, and presses that opened something. door_precision is the ratio, and it is the number
# that says whether the senses are telling the truth about what is a door.
DOOR_FMT = "HH"
STATUS_FULL_FMT = STATUS_FMT + "B" + CAND_FMT * MAX_CANDIDATES + THREAT_FMT + DOOR_FMT
STATUS_LEN = struct.calcsize(STATUS_FULL_FMT)
# INTENT, charter 3.1: intent_id, based_on_tic, mode, target x/y, has_target, stance, fire policy,
# fire target, weapon slot, use at target, time to live.
INTENT_FMT = "!HIBffBBBBBBH"
INTENT_LEN = struct.calcsize(INTENT_FMT)
GOALS = ["EXPLORE", "KILL_ENEMY", "STOCK_AMMO", "RESTORE_HEALTH", "ADD_ARMOR", "UPGRADE_WEAPON", "SCOUT", "HOLD"]
AHEAD_KINDS = ["nothing", "wall", "door", "exit", "locked", "barrier", "thing"]
ENEMIES = {"DoomImp", "Zombieman", "ShotgunGuy", "Demon", "Spectre", "ChaingunGuy", "Cacodemon", "HellKnight",
           "BaronOfHell", "LostSoul", "Revenant", "Arachnotron", "Fatso", "PainElemental", "Archvile", "WolfensteinSS"}
ITEM_KIND = {"Stimpack": "health", "Medikit": "health", "HealthBonus": "health", "Soulsphere": "health",
             "Clip": "ammo", "ClipBox": "ammo", "Shell": "ammo", "ShellBox": "ammo",
             "GreenArmor": "armor", "BlueArmor": "armor", "ArmorBonus": "armor",
             "Shotgun": "weapon", "Chaingun": "weapon", "Chainsaw": "weapon",
             "RedCard": "key", "BlueCard": "key", "YellowCard": "key", "RedSkull": "key", "BlueSkull": "key", "YellowSkull": "key"}
KEY_COLOUR = {"RedCard": "red", "RedSkull": "red", "BlueCard": "blue", "BlueSkull": "blue", "YellowCard": "yellow", "YellowSkull": "yellow"}
KEY_BIT = {"red": 1, "blue": 2, "yellow": 4}
# Charter 3.5. Engine behaviour: on some maps the way out only opens when these die. The list is
# a property of the game, not of any level, and knowledge/doom_rules.yaml is the source of record.
BOSS_CLASSES = ("BaronOfHell", "Cyberdemon", "SpiderMastermind")
SOLID_THINGS = {"ExplosiveBarrel", "BurningBarrel", "Column", "TechPillar", "ShortGreenColumn", "TallGreenColumn", "ShortRedColumn",
                "TallRedColumn", "SkullColumn", "HeartColumn", "EvilEye", "FloatingSkull", "TorchTree", "BlueTorch", "GreenTorch",
                "RedTorch", "ShortBlueTorch", "ShortGreenTorch", "ShortRedTorch", "Stalagtite", "Stalagmite", "BigTree", "TechLamp",
                "TechLamp2", "Candelabra", "Meat2", "Meat3", "Meat4", "Meat5", "HangNoGuts", "HangBNoBrain", "HangTLookingDown",
                "HangTSkull", "HangTLookingUp", "HangTNoBrain"}
# Charter 3.5: slots 1, 4 and 5 were missing, so the player could never take the fist or the chainsaw, the
# chaingun or the rocket launcher. An episode's last level is a boss fight against two monsters with a
# thousand hit points each, which is a very long afternoon on a pistol and a shotgun.
BUTTONS = [vzd.Button.MOVE_FORWARD_BACKWARD_DELTA, vzd.Button.MOVE_LEFT_RIGHT_DELTA, vzd.Button.TURN_LEFT_RIGHT_DELTA,
           vzd.Button.ATTACK, vzd.Button.USE,
           vzd.Button.SELECT_WEAPON1, vzd.Button.SELECT_WEAPON2, vzd.Button.SELECT_WEAPON3,
           vzd.Button.SELECT_WEAPON4, vzd.Button.SELECT_WEAPON5]
WEAPON_BUTTON = {1: vzd.Button.SELECT_WEAPON1, 2: vzd.Button.SELECT_WEAPON2, 3: vzd.Button.SELECT_WEAPON3,
                 4: vzd.Button.SELECT_WEAPON4, 5: vzd.Button.SELECT_WEAPON5}
BUTTON_INDEX = {b: i for i, b in enumerate(BUTTONS)}
# Doom's own forwardmove and sidemove at a run. Measured with payload/speed_probe.py: this delta reaches
# 507 units/s and the engine caps there, while the 14 the payload used before reached 141. Every speed
# measured before 22 September was taken against a ceiling of 28% of running.
RUN_FORWARD, RUN_STRAFE = 50, 40
from mapclasses import (NONE, STEP, DOOR, LOCK_RED, LOCK_BLUE, LOCK_YELLOW, LOCKED, EXIT, WALL, BARRIER,  # noqa: E402,F401
                        LOCK_KEY, BLOCKING, GRID, WPX, ENEMY_INDEX, NO_ENEMY, ENEMY_CLASSES)                                                     # noqa: E402,F401

FOV = 90.0             # ViZDoom default horizontal field of view
DEPTH_UNITS = 7.16     # map units per depth-buffer step; the buffer holds perpendicular (z) distance (depth_calib_probe*.py)
# Looking down at the floor, to see the thing the eye-level band cannot. A step up of more than 24 units
# stops the player dead, and its top is below eye height, so the camera looks straight over it and reports
# four hundred units of clear floor: the wedges in every flight log read `rel=1 ahead=nothing fwd=400` at
# full throttle with the player not moving. On level ground a fixed row below the horizon always returns
# the same distance, whatever the room, because the eye height is fixed -- so the payload can calibrate it
# from its own camera and needs no geometry it was not given. Shorter than that with the eye-level band
# still clear means something is raised in front of the feet.
STEP_BAND = (0.60, 0.74)   # fraction of image height: the floor a short way in front of the player
STEP_RATIO = 0.65          # this much closer than level ground is a step, not a floor
STEP_CALIB_MIN = 40        # samples before the calibration is worth believing
STEP_ACTS = False          # off: clamping the clearance put the avoidance guard at its harshest
STEP_MARKS_BARRIER = False  # on: tell the planner instead, so it routes round rather than crawls along
DEPTH_FAR = 56         # depth steps beyond which the range camera is not trusted (~400 units)
SENSE_EVERY = 7        # tics between automap stamps and the slower sensing (5 Hz)
UPLINK_TIMEOUT_S = 3.0  # no CONTROL for this long -> release everything (safe mode)
WAD_PROBE_TIMEOUT_S = 15.0  # a LOAD_WAD whose game has not flown a second in a child process by now is refused
PROBE_LOADED = "[probe] the game loaded the file"   # what the child says between loading and starting the map
DOOR_TRIES = 10        # use presses at a door before it counts as "does not open for me now"
DOOR_RETRY_S = 120.0   # a door that did not open is treated as a wall for this long
# How long a barrier learned by bumping into something is believed. It used to be an hour, which inside a
# three-minute attempt is forever: payload/ray_class_probe.py found that 92% of the map rays that came
# back short were stopped by one of these rather than by anything in the level, so one bump against a door
# frame walled off a corridor for the rest of the attempt and the planner believed it. EXP-0001.
STUCK_BARRIER_S = 25.0
# Off, on the evidence. The learner had been wired to the legacy CONTROL command and so had not fired
# since the executor landed; connecting it to the command actually issued made every flight worse. With a
# healthy uplink it remembers about 130 barriers in 180 seconds, because it cannot tell a step it truly
# cannot climb from a shoulder brushed while turning -- and a false barrier does not expire out of the
# planner's opinion, it just moves the player somewhere else to get stuck. Flown either way on the same
# level: learner off reached 0.84, 0.52 and 0.28 of the way to the exit; learner on reached 0.06, 0.22,
# 0.17 and 0.24, with rubbing climbing to 56%.
#
# The wiring stays fixed and the mechanism stays here, because the thing it is trying to sense is real:
# the depth camera is a horizontal band, so a step reads as open floor. Sensing it needs the vertical
# extent of the depth buffer, not an inference from having failed to move.
LEARN_BARRIERS_BY_PUSHING = False
RAY_MAX = 400          # how far the map rays look (units)
DOOR_OPEN_WAIT_S = 1.0  # game seconds to wait after a Use press before judging whether anything opened.
                        # A Doom door takes about a second and a half to rise clear; at 0.6 the verdict
                        # was being taken while it was still moving.
DOOR_OPEN_UNITS = 100   # the clearance ahead has to grow by this much for the press to count as an open
# Charter 2.3, the one borderline call that adds information rather than hiding it. ZDoom colours an exit
# line on the automap from its special type, so the colour is readable from across a level the moment the
# line is drawn -- which a player looking at a wall cannot do. An exit line is therefore only RECOGNISED
# while it is within this many units; recognition is then remembered, the way seeing a thing is. Set it to
# 0 to remove exit colouring entirely and make the pilot find the exit switch by looking at it.
EXIT_LINE_MAX_UNITS = 512

# ---- the automap as a sensor: ViZDoom renders it at screen size, centred on the player, viz_am_scale 2.5 = 0.5 px/unit
AM_W, AM_H, AM_SCALE, AM_CX, AM_CY = 640, 480, 0.5, 320, 240
WORLD_HALF = 6144      # world raster covers +-6144 units around the level start
BARRIER_S = 45.0
# How far out a ray has to be before a floor-height change stops it. The player is usually standing on or
# beside one -- the lip of the floor it is on -- and stopping at nought units would map nothing at all.
SEE_OVER_LEDGE_UNITS = 48.0
CLASS_RGB = {WALL: (255, 255, 255), STEP: (83, 175, 71), DOOR: (115, 115, 255), LOCK_RED: (255, 0, 0), LOCK_BLUE: (0, 0, 255),
             LOCK_YELLOW: (255, 255, 0), LOCKED: (255, 123, 123), EXIT: (255, 127, 27)}
RENDER_RGB = {**CLASS_RGB, BARRIER: (255, 160, 90)}
ARROW_RGB = (255, 0, 255)
# ZDoom's automap categories, given colours code can tell apart (the categories themselves are the engine's defaults:
# am_showkeys on, exit lines coloured, trigger lines off). The palette maps "00 ff 00" to (83,175,71) etc.
AM_CVARS = ['am_backcolor "00 00 00"', 'am_wallcolor "ff ff ff"', 'am_fdwallcolor "00 ff 00"', 'am_cdwallcolor "80 80 ff"',
            'am_lockedcolor "ff 80 80"', 'am_interlevelcolor "ff 80 00"', 'am_intralevelcolor "00 00 00"', 'am_yourcolor "ff 00 ff"',
            'am_tswallcolor "00 00 00"', 'am_secretwallcolor "ff ff ff"', 'am_secretsectorcolor "ff ff ff"', 'am_efwallcolor "ff ff ff"',
            'am_notseencolor "00 00 00"', 'am_gridcolor "00 00 00"', 'am_xhaircolor "00 00 00"', 'am_showgrid 0',
            'am_showtriggerlines 0', 'am_showkeys 1']
_VECS = np.array([np.array(c, np.float32) / np.linalg.norm(c) for c in list(CLASS_RGB.values()) + [ARROW_RGB]])
_VEC_CLASS = np.array(list(CLASS_RGB.keys()) + [NONE], np.uint8)


class Phase:
    """A stopwatch that adds to the payload's running per-phase total. One `with` per thing worth timing."""

    __slots__ = ("store", "name", "t0")

    def __init__(self, store, name):
        self.store, self.name = store, name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *_exc):
        self.store[self.name] = self.store.get(self.name, 0.0) + (time.perf_counter() - self.t0)
        return False


def bearing_deg(x, y, angle, tx, ty):
    """Signed bearing to (tx, ty) relative to heading `angle`; positive means left."""
    return (math.degrees(math.atan2(ty - y, tx - x)) - angle + 180) % 360 - 180


def classify(am):
    """Automap RGB -> class per pixel. Lines are anti-aliased, so classify by colour direction, any brightness."""
    bright = am.max(axis=2)
    bright[AM_CY - 4:AM_CY + 5, AM_CX - 4:AM_CX + 5] = 0   # the player marker is not a line
    cls = np.zeros(bright.shape, np.uint8)
    ys, xs = np.nonzero(bright >= 40)
    if len(xs):
        p = am[ys, xs].astype(np.float32)
        unit = p / (np.linalg.norm(p, axis=1, keepdims=True) + 1e-6)
        dots = unit @ _VECS.T
        best = dots.argmax(axis=1)
        c = _VEC_CLASS[best]
        c[dots.max(axis=1) < 0.965] = NONE
        cls[ys, xs] = c
    return cls


def next_map(name):
    m = re.fullmatch(r"E(\d)M(\d)", name)
    if m:
        return f"E{m.group(1)}M{int(m.group(2)) + 1}"
    m = re.fullmatch(r"MAP(\d\d)", name)
    return f"MAP{int(m.group(1)) + 1:02d}" if m else name


class Explorer:
    """The map the payload builds for itself for one level attempt: the in-game automap (lines the player has
    seen) stamped into a world raster, floor swept by the range camera, where the player has walked, what it
    remembers seeing (items, keys) and what it learned by bumping into (barriers)."""

    def __init__(self, x0, y0):
        self.ox, self.oy = x0 - WORLD_HALF, y0 + WORLD_HALF   # raster origin: top-left corner in map units
        self.n = 2 * WORLD_HALF // WPX
        self.raster = np.zeros((self.n, self.n), np.uint8)
        self.barrier_t = np.full((self.n, self.n), -1e9, np.float32)   # when a barrier was confirmed there
        self.ux = (np.arange(AM_W) - AM_CX) / AM_SCALE          # automap column -> x offset from the player (units)
        self.vy = -(np.arange(AM_H) - AM_CY) / AM_SCALE         # automap row -> y offset (map y is up)
        self.free = set()
        # What the automap has drawn, ever, as a mask. The raster holds a CLASS per pixel and is rewritten
        # window by window; this only ever grows, because a line the player has seen stays on the automap.
        # It is what `seen_geometry` gates on, so it has to be the record of looking rather than of
        # classifying -- the two came apart the moment the classes stopped coming from the colours.
        self.drawn = np.zeros((self.n, self.n), bool)
        self.exit_px = set()   # raster pixels recognised as an exit line from close enough to read it
        self.exit_search_px = 150   # 600 units: an exit line counts while it is in view (charter 2.3)
        self.visited = {}      # cell -> tics the player has stood in it (the walk, remembered)
        self.items = {}        # (kind, rounded x, rounded y) -> {"kind", "name", "x", "y", "seen"}
        self.keys = set()      # colours of keys picked up
        self.hint = None       # (absolute bearing, expiry)
        self.stamps = 0
        self.geom = None       # set by the payload when the geometry sensor owns the raster
        self._mask_key, self._mask = None, None
        self._cellblk_key, self._cellblk = None, None

    @staticmethod
    def cell(x, y):
        return int(math.floor(x / GRID)), int(math.floor(y / GRID))

    def wpx(self, x, y):
        return int((x - self.ox) / WPX), int((self.oy - y) / WPX)

    # ------------------------------------------------------------------ sensing
    def stamp(self, am, px, py):
        """Write the automap window (everything mapped within it) into the world raster, replacing the region."""
        cls = classify(am)
        ys, xs = np.nonzero(cls)
        ix0, iy0 = self.wpx(px + self.ux[0], py + self.vy[0])
        ix1, iy1 = self.wpx(px + self.ux[-1], py + self.vy[-1])
        a, b, c, d = max(0, ix0 + 2), min(self.n, ix1 - 1), max(0, iy0 + 2), min(self.n, iy1 - 1)
        cx_, cy_ = self.wpx(px, py)
        under = self.raster[cy_ - 5:cy_ + 6, cx_ - 5:cx_ + 6].copy()   # the arrow hides the lines beneath it
        if a < b and c < d and self.geom is None:
            # When the geometry sensor owns the raster, the automap is a record of looking and nothing
            # more: blanking the window here would wipe the exact lines and leave the pilot steering by
            # whatever the colour classifier happened to think, which is the pipeline being replaced.
            self.raster[c:d, a:b] = 0
        if len(xs):
            ix = ((px + self.ux[xs] - self.ox) / WPX).astype(np.intp)
            iy = ((self.oy - (py + self.vy[ys])) / WPX).astype(np.intp)
            ok = (ix >= 0) & (ix < self.n) & (iy >= 0) & (iy < self.n)
            # Looking, recorded separately from classifying. Cumulative, and never cleared by the window
            # rewrite above: the automap does not forget a line, so neither does this.
            self.drawn[iy[ok], ix[ok]] = True
            # An exit line is a wall until it has been seen from within EXIT_LINE_MAX_UNITS; once it has, the
            # pixel is remembered as an exit for the rest of the attempt (the window is rewritten every stamp,
            # so the memory has to live outside the raster).
            close = ok & (cls[ys, xs] == EXIT) & (np.hypot(self.ux[xs], self.vy[ys]) <= EXIT_LINE_MAX_UNITS)
            self.exit_px.update(zip(iy[close].tolist(), ix[close].tolist()))
            if self.geom is None:
                vals = cls[ys[ok], xs[ok]].copy()
                vals[vals == EXIT] = WALL
                np.maximum.at(self.raster, (iy[ok], ix[ok]), vals)
        blk = self.raster[cy_ - 5:cy_ + 6, cx_ - 5:cx_ + 6]
        if blk.shape == under.shape and self.geom is None:
            np.maximum(blk, under, out=blk)
        for ey_, ex_ in self.exit_px:
            if c <= ey_ < d and a <= ex_ < b:
                self.raster[ey_, ex_] = EXIT
        self.stamps += 1

    def sweep(self, x, y, angle, depth_row):
        """One range-camera sweep: every cell a ray passes through is floor the player has seen -- up to the
        first floor-height change, because seeing floor is not the same as being able to walk onto it.

        The camera sits at eye height and looks straight over a ledge, so a ray that crosses one keeps
        reporting floor for hundreds of units past ground the player cannot reach. The planner then routes
        onto it and the player pushes into the ledge at full throttle with its heading dead on and four
        hundred units of clear space ahead, which is every wedge in every flight log. On one dev level,
        five seeds all stopped within the same five hundred units, at a ledge with one passable gap in it,
        and none of them found the gap.

        The automap draws floor-height changes in their own colour, so this uses nothing but what has
        already been seen, and it is self-correcting: climb the step and the sweep from up there maps what
        is beyond. Conservative about ground it has only looked at, not about ground it has stood on.
        """
        here = self.cell(x, y)
        self.free.add(here)
        self.visited[here] = self.visited.get(here, 0) + 1
        w = len(depth_row)
        for col in range(0, w, 16):
            rel = math.degrees(math.atan((0.5 - col / (w - 1)) * 2 * math.tan(math.radians(FOV / 2))))
            dist = min(int(depth_row[col]), DEPTH_FAR) * DEPTH_UNITS / math.cos(math.radians(rel))
            a = math.radians(angle + rel)
            ca, sa = math.cos(a), math.sin(a)
            r = GRID / 2
            while r < dist - GRID / 2:
                px, py = x + r * ca, y + r * sa
                if (self.geom is None and r > SEE_OVER_LEDGE_UNITS
                        and self.near_class(*self.wpx(px, py), STEP, 1)):
                    # Only while the colours are the source. The automap draws a 16-unit stair tread and
                    # a 200-unit drop in the same colour, so the sweep has to stop at both; with exact
                    # geometry a STEP is a rise the engine lets the player climb and a ledge is already
                    # a WALL, so stopping here would blind the camera to floor the player can reach.
                    break
                self.free.add(self.cell(px, py))
                r += GRID / 2

    def remember_items(self, x, y, labels):
        for lab in labels:
            kind = ITEM_KIND.get(lab.object_name)
            if kind:
                key = (kind, round(lab.object_position_x / 16), round(lab.object_position_y / 16))
                self.items[key] = {"kind": kind, "name": lab.object_name, "x": lab.object_position_x,
                                   "y": lab.object_position_y, "seen": time.time()}
        for key in [k for k, v in self.items.items() if math.hypot(v["x"] - x, v["y"] - y) < 40]:
            if self.items[key]["kind"] == "key":
                self.keys.add(KEY_COLOUR.get(self.items[key]["name"], "red"))
                self.barrier_t[:] = np.minimum(self.barrier_t, time.time() - 1e6)   # locked doors deserve a fresh try
                print(f"[payload] picked up the {self.items[key]['name']}", flush=True)
            del self.items[key]  # walked over it: picked up (or not a pickup we can take)

    def nearest_item(self, x, y, kind):
        best = None
        for v in self.items.values():
            if v["kind"] == kind:
                d = math.hypot(v["x"] - x, v["y"] - y)
                if best is None or d < best[0]:
                    best = (d, v)
        return best

    def mark_barrier(self, x, y, heading, seconds, dist=24):
        """Something the map does not show blocks the way `heading` from (x, y): mark it across its width.

        Three marks twelve units apart covered less than one 32-unit cell, so the planner sent the player
        thirty-two units along the same ledge and it pushed into the identical obstacle again: one flight
        learned 133 barriers and was still grinding 40% of its ticks, all of them at x=1520 with only y
        changing. What stops a player with four hundred units of clear floor ahead is a step it cannot
        climb, and a step is a line, not a point.
        """
        dist = max(24, dist)
        a = math.radians(heading)
        bx, by = x + dist * math.cos(a), y + dist * math.sin(a)
        lx, ly = -math.sin(a), math.cos(a)
        until = time.time() + seconds - BARRIER_S
        for k in (-12, 0, 12):
            ix, iy = self.wpx(bx + k * lx, by + k * ly)
            if 1 <= ix < self.n - 1 and 1 <= iy < self.n - 1:
                self.barrier_t[iy - 1:iy + 2, ix - 1:ix + 2] = until

    def near_class(self, ix, iy, cls, r=1):
        """Is any pixel of this class in the window? `klass` takes the maximum, and the merge priority
        puts STEP below everything, so a floor-height-change line beside a wall is invisible to it."""
        if not (r <= ix < self.n - r and r <= iy < self.n - r):
            return False
        return bool((self.raster[iy - r:iy + r + 1, ix - r:ix + r + 1] == cls).any())

    def blocked_mask(self, now, r):
        """Which raster pixels a player of radius `r` pixels cannot stand on, over the explored window.

        `klass` answers that one pixel at a time with a (2r+1)^2 max and a barrier check, and the world
        model asks it once per candidate cell, three or four times per decision. That was fine while the
        floor came from a 400-unit camera sweep and there were a few hundred cells; with the visibility
        fill a single look around a hall adds two thousand, and the same loop went to 160 ms a call --
        five times the whole tic budget.

        A max filter is the same question asked once for everybody. Separable, and only over the box the
        player has actually seen, so it costs a few milliseconds rather than a hundred.
        """
        key = (round(now, 3), r, len(self.free))
        if self._mask_key == key:
            return self._mask
        if not self.free:
            self._mask_key, self._mask = key, None
            return None
        xs = [c[0] for c in self.free]
        ys = [c[1] for c in self.free]
        pad = 3 * r + 4
        ix0 = max(0, int((min(xs) * GRID - self.ox) / WPX) - pad)
        ix1 = min(self.n, int((max(xs) * GRID + GRID - self.ox) / WPX) + pad)
        iy0 = max(0, int((self.oy - (max(ys) * GRID + GRID)) / WPX) - pad)
        iy1 = min(self.n, int((self.oy - min(ys) * GRID) / WPX) + pad)
        if ix1 <= ix0 or iy1 <= iy0:
            self._mask_key, self._mask = key, None
            return None
        win = self.raster[iy0:iy1, ix0:ix1]
        blk = np.zeros(win.shape, bool)
        for cls in BLOCKING:
            blk |= (win == cls)
        blk |= (now - self.barrier_t[iy0:iy1, ix0:ix1]) < BARRIER_S
        out = blk.copy()
        for d in range(1, r + 1):                       # separable dilation by r pixels
            out[:, d:] |= blk[:, :-d]
            out[:, :-d] |= blk[:, d:]
        col = out.copy()
        for d in range(1, r + 1):
            out[d:, :] |= col[:-d, :]
            out[:-d, :] |= col[d:, :]
        self._mask_key, self._mask = key, (out, ix0, iy0)
        return self._mask

    CELL_PX = GRID // WPX          # raster pixels across one planner cell: 32 units at 4 units a pixel

    def cell_blocked(self, now, r):
        """Which 32-unit cells the player cannot stand ANYWHERE in.

        The distinction is the whole of it. `blocked_mask` answers "can the player stand on this exact
        point", and asking that of a cell CENTRE is a different and much harsher question: the grid is 32
        units, the player is 16 across, so in a 64-unit corridor every cell centre sits exactly at the
        limit and rounding decides. With the camera sweep that never showed, because `free` only held
        ground the player had already been near. With sightlines filling whole rooms it showed at once --
        an oracle run with the entire level walkable reported no route to any of twelve frontiers, with
        16,848 walkable cells on the map, because the corridors between the rooms had every cell centre
        just inside a wall and the graph fell into disconnected pieces.

        A player can stand anywhere in a cell, so a cell is usable when any point in it is clear.
        """
        key = (round(now, 3), r, len(self.free))
        if self._cellblk_key == key:
            return self._cellblk
        box = self.blocked_mask(now, r)
        if box is None:
            self._cellblk_key, self._cellblk = key, None
            return None
        blk, ix0, iy0 = box
        c = self.CELL_PX
        # trim to whole cells, aligned to the raster's own cell grid
        ax = ix0 + (-ix0) % c
        ay = iy0 + (-iy0) % c
        h = ((iy0 + blk.shape[0]) - ay) // c
        w = ((ix0 + blk.shape[1]) - ax) // c
        if h <= 0 or w <= 0:
            self._cellblk_key, self._cellblk = key, None
            return None
        sub = blk[ay - iy0:ay - iy0 + h * c, ax - ix0:ax - ix0 + w * c]
        cells = sub.reshape(h, c, w, c).all(axis=(1, 3))
        self._cellblk_key = key
        self._cellblk = (cells, ax // c, ay // c)     # in raster-cell coordinates, y increasing downward
        return self._cellblk

    def cell_is_free(self, cx, cy, now, r):
        """Can the player stand somewhere in world cell (cx, cy)?"""
        got = self.cell_blocked(now, r)
        if got is None:
            return None
        cells, jx0, jy0 = got
        ix, iy = self.wpx((cx + 0.5) * GRID, (cy + 0.5) * GRID)
        jx, jy = ix // self.CELL_PX - jx0, iy // self.CELL_PX - jy0
        if not (0 <= jy < cells.shape[0] and 0 <= jx < cells.shape[1]):
            return None
        return not bool(cells[jy, jx])

    def klass(self, ix, iy, now, r=1):
        """Class of the raster around a pixel ((2r+1)^2 window), barriers included."""
        if not (r <= ix < self.n - r and r <= iy < self.n - r):
            return NONE
        c = int(self.raster[iy - r:iy + r + 1, ix - r:ix + r + 1].max())
        if (now - self.barrier_t[iy - r:iy + r + 1, ix - r:ix + r + 1]).min() < BARRIER_S:
            return BARRIER
        return c

    def probe(self, x, y, heading, dist, now):
        """What the map says about the surface the camera sees at `dist` along `heading`: look a little short
        of and beyond the hit (the range is quantised) and report the most telling category found."""
        a = math.radians(heading)
        found = set()
        for r in range(max(4, int(dist) - 14), int(dist) + 22, 4):
            c = self.klass(*self.wpx(x + r * math.cos(a), y + r * math.sin(a)), now, r=2)
            if c != NONE:
                found.add(c)
        for c in (EXIT, DOOR, LOCK_RED, LOCK_BLUE, LOCK_YELLOW, LOCKED, BARRIER, WALL, STEP):
            if c in found:
                return c
        return NONE

    def sector(self, x, y, heading, now):
        """A direction judged over three rays: (space, novelty). Unseen ground on the way beats everything seen:
        space is then how far the seen floor goes (at least 64) and novelty is 255, the word for unexplored."""
        best = None
        for off in (-15, 0, 15):
            r = self.ray(x, y, heading + off, now)
            if r[4] and r[4] < r[0]:
                cand = (max(64, r[4]), r[1], r[2], r[3], r[4]), 255
                score = 1000 + r[4]
            else:
                nov = self.novelty(x, y, heading + off, r[0])
                cand = (r, nov)
                score = r[0] + 2 * nov
            if best is None or score > best[0]:
                best = (score, cand)
        return best[1]

    def ray(self, x, y, heading, now, max_units=RAY_MAX):
        """Walk the map from the player in one direction: (distance to the first wall-like thing or max_units,
        distance to the first door on the way or 0, distance to an exit line or 0, class that stopped the ray,
        distance to the first ground never seen or 0)."""
        a = math.radians(heading)
        ca, sa = math.cos(a), math.sin(a)
        door, unseen = 0, 0
        for r in range(20, max_units + 1, 4):   # the player's own body is free space
            px_, py_ = x + r * ca, y + r * sa
            ix, iy = self.wpx(px_, py_)
            c = self.klass(ix, iy, now)
            if c in (WALL, BARRIER, LOCKED):
                return r, door, 0, c, unseen
            if not unseen and r % 16 == 0 and r >= 32 and self.cell(px_, py_) not in self.free:
                unseen = r
            if c in LOCK_KEY:
                if LOCK_KEY[c] in self.keys:
                    door = door or r
                else:
                    return r, door, 0, c, unseen
            elif c == EXIT:
                return r, door, r, c, unseen
            elif c == DOOR and not door:
                door = r
        return max_units, door, 0, NONE, unseen

    def novelty(self, x, y, heading, dist):
        """How much of the ground that way has not been walked: 0..100 (100 = all new), over cells up to `dist`."""
        a = math.radians(heading)
        if dist < 48:
            return 0   # a wall at arm's length: nothing to walk there
        cells, new = 0, 0
        for r in range(32, int(min(dist, 288)) + 1, 32):
            c = self.cell(x + r * math.cos(a), y + r * math.sin(a))
            cells += 1
            if self.visited.get(c, 0) == 0:
                new += 1
        return int(100 * new / cells) if cells else 100

    def nearest_exit(self, x, y, now):
        """Nearest exit-line pixel seen within 600 units, or None.

        The radius is a perception limit: an exit line counts while it is in view, not for the rest of the
        attempt (charter 2.3). `exit_search_px` is how the ORACLE widens it, and nothing else touches it --
        without that, rung L0 was handed the exit's position and then could not see it until it had walked
        to within six hundred units, which is not the rung the brief describes.
        """
        ix, iy = self.wpx(x, y)
        r = self.exit_search_px
        win = self.raster[max(0, iy - r):iy + r, max(0, ix - r):ix + r]
        ys, xs = np.nonzero(win == EXIT)
        if not len(xs):
            return None
        ex = self.ox + (max(0, ix - r) + xs) * WPX
        ey = self.oy - (max(0, iy - r) + ys) * WPX
        d = np.hypot(ex - x, ey - y)
        k = int(d.argmin())
        return float(ex[k]), float(ey[k]), float(d[k])

    def render(self, x, y, angle):
        """Draw the self-built map (for the ground display and the map product)."""
        if not self.free:
            return None
        xs = [c[0] for c in self.free]
        ys = [c[1] for c in self.free]
        cx0, cx1, cy0, cy1 = min(xs) - 2, max(xs) + 3, min(ys) - 2, max(ys) + 3
        W, H = cx1 - cx0, cy1 - cy0
        S = 5
        img = Image.new("RGB", (W * S, H * S), (24, 24, 28))
        d = ImageDraw.Draw(img)
        px = lambda c: ((c[0] - cx0) * S, (cy1 - 1 - c[1]) * S)   # map y up
        for c in self.free:
            a, b = px(c)
            d.rectangle((a, b, a + S - 1, b + S - 1), fill=(64, 64, 72))
        for c, n in self.visited.items():
            a, b = px(c)
            d.rectangle((a + 1, b + 1, a + S - 2, b + S - 2), fill=(40, min(255, 90 + n), 60))
        ix0, iy0 = self.wpx(cx0 * GRID, cy1 * GRID)
        ix1, iy1 = self.wpx(cx1 * GRID, cy0 * GRID)
        sub = self.raster[max(0, iy0):iy1, max(0, ix0):ix1]
        now = time.time()
        bar = (now - self.barrier_t[max(0, iy0):iy1, max(0, ix0):ix1]) < BARRIER_S
        ys_, xs_ = np.nonzero((sub > 0) | bar)
        for u, v in zip(xs_, ys_):
            cls = BARRIER if bar[v, u] else int(sub[v, u])
            wx, wy = self.ox + (max(0, ix0) + u) * WPX, self.oy - (max(0, iy0) + v) * WPX
            d.point(((wx / GRID - cx0) * S, (cy1 - wy / GRID) * S), fill=RENDER_RGB.get(cls, (200, 200, 200)))
        a, b = (x / GRID - cx0) * S, (cy1 - y / GRID) * S
        d.ellipse((a - 4, b - 4, a + 4, b + 4), fill=(255, 60, 60))
        d.line((a, b, a + 12 * math.cos(math.radians(angle)), b - 12 * math.sin(math.radians(angle))), fill=(255, 60, 60), width=2)
        return img


class Payload:
    def __init__(self, args):
        self.args = args
        self.wad = self._find_wad(args.wad)
        self.pwad = self._find_wad(args.pwad) if getattr(args, "pwad", None) else None
        self.map = args.map
        # Two switches, and the difference between them is the whole knowledge boundary.
        #
        # `geometry` turns on the exact-lines sensor (payload/seen_geometry.py). It is honest: a line
        # reaches the pilot only once the automap has drawn it, which is what a player's own automap
        # shows. `oracle` opens that gate, and is a diagnostic: an attempt that ran with it says so in
        # its own record and the grader refuses to score it (payload/oracle.py).
        self.geometry = getattr(args, "geometry", "off")
        self.oracle = getattr(args, "oracle", "off")
        if self.oracle in ("L0", "L1"):
            self.geometry = "on"
        elif self.oracle == "L2":
            self.geometry = "off"
        # "seen" unless the oracle says otherwise, and the oracle is the only thing that may say so:
        # honesty test 2b fails a payload that opens the gate on its own account.
        self.geom_reveal = "seen"
        self.oracle_exits, self.oracle_mod = [], None
        if self.oracle in ("L0", "L1"):
            import oracle as _oracle             # ORACLE only: never imported on an honest run
            self.oracle_mod = _oracle
            self.geom_reveal = _oracle.reveal_for(self.oracle)
            self.oracle_exits = _oracle.exit_positions(self.wad, self.map)
            print("[payload] ORACLE %s: geometry reveal=%s, %d exit line(s) known in advance"
                  % (self.oracle, self.geom_reveal, len(self.oracle_exits)), flush=True)
        self.geom = None
        self.game = self._make_game()
        self.explorer = None
        self.explorer_map = None
        self.goal = "EXPLORE"
        self.control = dict(move=0, strafe=0, turn=0.0, fire=0, use=0, weapon=0)
        self.last_control_time = 0.0
        self.frame_hz, self.quality = args.fps, args.quality
        self.frame_seq = 0
        self.episode = 0
        self.level = 0
        self.positions = deque(maxlen=35)
        self.motions = deque(maxlen=35)
        self.stuck = False
        self.cmd_count = 0
        self.last_obs = None
        self.turn_remaining = 0.0
        self.carry = None
        self.map_png_bytes, self.map_seq = None, 0
        self.sense = None            # the slower sensing (rays, novelty, exit) refreshed every SENSE_EVERY tics
        self.door_presses, self.door_at = 0, None
        self.last_move = (0.0, 0.0)   # what actually drove the player last tic, whichever path issued it
        self.floor_seen = deque(maxlen=400)   # how far the down-looking band reads; level ground dominates
        self.step_said = 0.0
        self.empty_said = 0.0
        self.use_ok = False
        # door_precision: presses that opened something, over presses. A ceiling-change line that is not
        # a door absorbs presses and opens nothing, so this is the number that says whether the senses
        # are telling the truth about doors.
        self.press_total, self.press_opened, self.doors_settled = 0, 0, 0
        self.frontiers_dropped = 0
        self.replanned_elsewhere, self.unroutable = 0, 0
        self.press_watch = None      # (cell key, clearance when first pressed, game time)
        self.world = None            # charter 3.2, rebuilt every episode
        self.executor = None         # charter 3.1, rebuilt every episode
        # The executor runs on GAME time, not wall time. In flight the two agree, because the loop is
        # paced at 35 Hz in real time. On the bench they do not: the game advances by the latency the
        # harness injects, so half a second spent waiting for a real model answer is half a second of
        # wall clock in which no tic passes. Measured against the wall, the watchdog then sees a player
        # that has not moved and calls it a freeze -- 37 in 15 jev attempts against 6 in 30 code ones,
        # which is a property of the decider's latency and nothing to do with the pilot.
        self.game_time = 0.0
        self.candidates = []
        self.exec_obs = None
        # Where a tic goes. The flight loop has 28.6 ms and has been measured over it on one tic in
        # seven; without this the only honest thing anyone could say about it was "slow".
        self.phase_s = {}
        self.exit_bearing_of = None
        # LOAD_WAD: switches made, the request being proven in a child process, and reports for the flight
        # software that wait for the main loop (which holds the link).
        self.wad_loads, self.wad_job, self.outbox = 0, None, []
        self.wad_serial, self.wad_pinned = 0, None   # LOAD_WAD's pinned links (wad_uplink.pin): the request, the one flying
        self.new_episode()

    @staticmethod
    def _find_wad(name):
        for p in (name, os.path.join(os.environ.get("DOOMSAT_HOME", os.path.expanduser("~/doom")), "wads", name), os.path.join(os.path.dirname(vzd.__file__), name)):
            if os.path.isfile(p):
                return p
        raise SystemExit(f"WAD not found: {name}")

    def _make_game(self):
        g = vzd.DoomGame()
        g.set_doom_game_path(self.wad)
        if self.pwad:
            g.set_doom_scenario_path(self.pwad)
        g.set_doom_map(self.map)
        g.set_doom_skill(self.args.skill)
        g.set_available_buttons(BUTTONS)
        g.set_button_max_value(vzd.Button.TURN_LEFT_RIGHT_DELTA, 10)
        g.set_button_max_value(vzd.Button.MOVE_FORWARD_BACKWARD_DELTA, RUN_FORWARD)
        g.set_button_max_value(vzd.Button.MOVE_LEFT_RIGHT_DELTA, RUN_STRAFE)
        g.set_window_visible(False)
        g.set_screen_resolution(vzd.ScreenResolution.RES_640X480)
        g.set_screen_format(vzd.ScreenFormat.RGB24)
        g.set_render_hud(True)
        g.set_render_crosshair(False)   # it is drawn into the depth buffer too (depth 0 at the centre)
        g.set_depth_buffer_enabled(True)
        g.set_labels_buffer_enabled(True)
        g.set_automap_buffer_enabled(True)
        g.set_automap_mode(vzd.AutomapMode.NORMAL)      # only what the player has seen
        g.set_automap_rotate(False)
        g.set_automap_render_textures(False)
        # Exact geometry, for the sensor that gates it on the automap. Off unless asked for, so the
        # default build is the one the honesty suite has always described.
        g.set_sectors_info_enabled(self.geometry != "off")
        g.set_sound_enabled(False)
        g.set_episode_timeout(0)
        g.set_seed(self.args.seed)
        g.set_mode(vzd.Mode.PLAYER)
        g.add_game_args("+am_colorset 0 +am_drawmapback 0 +viz_am_scale 2.5")
        g.init()
        for c in AM_CVARS:
            g.send_game_command(c)
        if getattr(self.args, "probe", False):
            print(PROBE_LOADED, flush=True)
        return g

    def new_episode(self):
        """A fresh attempt: the level restarts and the world model is thrown away (weapons carry over between levels).

        Charter 2.2: no map survives an attempt. This used to keep the raster whenever the map name had not
        changed, on the reasoning that a player remembers a layout between tries. The effect was that every
        run after the first started with a map the payload already believed was walled in (EXPLORED_CELLS 662
        against 1 for a fresh one), which is the level knowledge the charter forbids and which confounded a
        whole afternoon of measurement. Honesty test 3 reads this method and fails the run if the rebuild
        ever becomes conditional again.
        """
        self.game.set_doom_map(self.map)
        self.game.new_episode()
        loadout = self.carry or {"shotgun": True, "shells": 4, "bullets": 30}
        cmds = ["take Shell 999", "take Clip 999", f"give Shell {loadout['shells']}", f"give Clip {loadout['bullets']}"]
        if loadout["shotgun"]:
            cmds.insert(0, "give shotgun")
        for cmd in cmds:
            self.game.send_game_command(cmd)
        self.game.make_action([0] * len(BUTTONS), 1)
        self.episode += 1
        if self.explorer_map != self.map:
            self.level += 1
        # The world model goes; the tally does not. Freezes and looks are counted per ATTEMPT, and an
        # attempt survives a death -- without this the numbers reported were whichever episode happened
        # to be last, which quietly undercounted the one thing charter phase 2 is measured on.
        carried_trips = dict(self.executor.watchdog.trips) if self.executor is not None else {}
        carried_log = list(self.executor.watchdog.trip_log) if self.executor is not None else []
        carried_stats = dict(self.executor.stats) if self.executor is not None else {}
        self.explorer = Explorer(self.var("POSITION_X"), self.var("POSITION_Y"))
        # Thrown away with everything else (charter 2.2): the geometry sensor remembers which lines have
        # been drawn, and that is exactly the kind of memory an attempt is not allowed to inherit.
        self.geom = (geom_mod.SeenGeometry(self.explorer, reveal=self.geom_reveal)
                     if self.geometry != "off" else None)
        self.explorer.geom = self.geom
        self.world = wm_mod.WorldModel(self.explorer, enemies=ENEMIES, item_kind=ITEM_KIND)
        self.executor = ex_mod.Executor(self.world)
        self.executor.watchdog.trips.update(carried_trips)
        self.executor.watchdog.trip_log[:0] = carried_log
        for k, v in carried_stats.items():
            self.executor.stats[k] = self.executor.stats.get(k, 0) + v
        self.candidates = []
        self.episode_wall = time.time()
        self.phase_said = {}
        self.oracle_flooded = False
        self.explorer_map = self.map
        self.positions.clear()
        self.motions.clear()
        self.stuck = False
        self.goal = "EXPLORE"
        self.control = dict(move=0, strafe=0, turn=0.0, fire=0, use=0, weapon=0)
        self.sense, self.door_presses, self.door_at = None, 0, None
        self.last_move = (0.0, 0.0)
        self.use_ok = False
        self.press_total, self.press_opened, self.press_watch = 0, 0, None
        self.doors_settled, self.frontiers_dropped = 0, 0
        self.replanned_elsewhere, self.unroutable = 0, 0
        print(f"[payload] episode {self.episode} started on {self.map} (level {self.level})", flush=True)

    def level_finished(self):
        """Carry weapons and ammunition into the next map, as the game does (keys stay behind)."""
        self.carry = {"shotgun": self.var("WEAPON3") > 0, "shells": int(self.var("AMMO3")), "bullets": int(self.var("AMMO2"))}
        # Over a PWAD (LOAD_WAD) the next map would be the IWAD's, or one the engine cannot find, and
        # new_episode() on a missing map never returns: fly the uplinked one again instead.
        if not self.pwad:
            self.map = next_map(self.map)
        self.new_episode()

    # ------------------------------------------------------------------ observation
    def var(self, name):
        return float(self.game.get_game_variable(getattr(vzd.GameVariable, name)))

    @staticmethod
    def band_clearance(depth_row, lo_deg, hi_deg):
        """Nearest range (map units) in a bearing band of the range camera; positive bearings are left."""
        w = len(depth_row)
        best = None
        for col in range(0, w, 8):
            rel = math.degrees(math.atan((0.5 - col / (w - 1)) * 2 * math.tan(math.radians(FOV / 2))))
            if lo_deg <= rel <= hi_deg:
                dist = min(int(depth_row[col]), DEPTH_FAR) * DEPTH_UNITS / math.cos(math.radians(rel))
                best = dist if best is None else min(best, dist)
        return 2000 if best is None else int(best)

    @staticmethod
    def depth_at(near_row, rel_deg, half_width=4.0):
        """How far the range camera can see at a relative bearing, in map units, or None if outside it."""
        if abs(rel_deg) > FOV / 2 - 2:
            return None
        w = len(near_row)
        lo = Payload._col_for(-rel_deg - half_width, w)
        hi = Payload._col_for(-rel_deg + half_width, w)
        lo, hi = max(0, min(lo, hi)), min(w - 1, max(lo, hi))
        band = near_row[lo:hi + 1]
        if not len(band):
            return None
        return float(band.max()) * DEPTH_UNITS / math.cos(math.radians(rel_deg))

    @staticmethod
    def _col_for(rel_deg, w):
        """Screen column for a bearing, inverting the perspective the sweep uses."""
        t = math.tan(math.radians(rel_deg)) / (2 * math.tan(math.radians(FOV / 2)))
        return int(round((0.5 + t) * (w - 1)))

    def slow_sense(self, x, y, angle, now, clear_fwd, enemies):
        """Map rays, novelty, what is ahead, exit position: refreshed every few tics."""
        ex = self.explorer
        ix, iy = ex.wpx(x, y)
        ex.barrier_t[iy - 3:iy + 4, ix - 3:ix + 4] = -1e9   # the player stands here: nothing solid within 12 units
        dirs = (("fwd", 0), ("al", 45), ("left", 90), ("bl", 135), ("back", 180), ("br", -135), ("right", -90), ("ar", -45))
        rays, nov = {}, {}
        rays["fwd"], nov["fwd"] = ex.sector(x, y, angle, now)
        for name, off in dirs[1:]:
            rays[name], nov[name] = ex.sector(x, y, angle + off, now)
        # A door on the way used to overwrite the novelty value, so the ground could not tell whether a door
        # led anywhere new. Doors now ride their own channel: distance / 8 units, 0 = no door (issue 9).
        doors = {name: (max(1, min(254, int(rays[name][1]) // 8)) if rays[name][1] and rays[name][1] < 440 else 0)
                 for name in rays}
        # what is at arm's length ahead, from the camera's range and the map's category there
        ahead_kind, ahead_dist = "nothing", 0
        if clear_fwd < 120:
            ahead_dist = clear_fwd
            c = ex.probe(x, y, angle, clear_fwd, now)
            if enemies and abs(enemies[0][1]) < 15 and abs(enemies[0][2] - clear_fwd) < 40:
                ahead_kind = "thing"
            elif c == EXIT:
                ahead_kind = "exit"
            elif c == DOOR or (c in LOCK_KEY and LOCK_KEY[c] in ex.keys):
                ahead_kind = "door"
            elif c in LOCK_KEY or c == LOCKED:
                ahead_kind = "locked"
            elif c == WALL:
                ahead_kind = "wall"
            elif c == BARRIER:
                ahead_kind = "barrier"
            elif c == STEP:
                ahead_kind = "wall"       # a step the camera sees as a wall is a ledge
            elif clear_fwd < 70:
                ahead_kind = "barrier"    # the map shows nothing here, the eyes do (bars, a fake door)
            else:
                ahead_kind, ahead_dist = "nothing", 0   # too far to be sure; walk up and look
        # a door on the forward ray counts as "door ahead" when close, even if the camera looks past its frame
        if ahead_kind == "nothing" and rays["fwd"][1] and rays["fwd"][1] < 100:
            ahead_kind, ahead_dist = "door", rays["fwd"][1]
        return dict(rays=rays, nov=nov, doors=doors, ahead_kind=ahead_kind, ahead_dist=ahead_dist,
                    exit_seen=ex.nearest_exit(x, y, now))

    def observe(self, state):
        x, y, angle = self.var("POSITION_X"), self.var("POSITION_Y"), self.var("ANGLE")
        ex = self.explorer
        now = time.time()
        depth = np.where(state.depth_buffer == 0, 255, state.depth_buffer)   # 0 is the sky (nothing there): far
        band = depth[depth.shape[0] * 5 // 12: depth.shape[0] * 7 // 12]
        depth_row = band.max(axis=0)                                            # farthest surface: sees past bars and sprites
        near_row = depth[196 * depth.shape[0] // 480: 222 * depth.shape[0] // 480].min(axis=0)   # nearest surface at eye level
        slow = state.tic % 3 == 0 or self.sense is None
        # Staggered, not synchronised. Every one of these ran on tic % 7 == 0 and nothing ran on the
        # other six, so one tic in seven carried the whole sensing budget: the mean was 4.5 ms and the
        # p95 was 21. Spreading them over the cycle changes no rate and flattens the spike.
        if state.tic % SENSE_EVERY == 0 or ex.stamps == 0:
            with Phase(self.phase_s, "automap stamp"):
                ex.stamp(state.automap_buffer, x, y)
        self.world.note_here(x, y)
        if self.geom is not None and state.tic % SENSE_EVERY == 2:
            # The only read of state.sectors in the payload. Everything downstream sees `ex.raster`,
            # which holds the lines this has released, and nothing else (payload/seen_geometry.py).
            with Phase(self.phase_s, "geometry"):
                self.geom.observe(state.sectors, x, y)
                ex.free.update(self.geom.visible_cells(x, y, now))
            if self.geom.reveal == "all" and not self.oracle_flooded:
                # ORACLE L0 only. With every line released the flood is the level's exact walkable map,
                # which is what this rung is for: the executor is asked to walk to a known exit across
                # known ground, and nothing about seeing is in the way of the answer.
                ex.free.update(self.geom.flood_open(ex.cell(x, y), now))
                self.oracle_flooded = True
                print("[payload] ORACLE L0: %d cells of true walkable floor" % len(ex.free), flush=True)
            self.reveal_oracle_exit(x, y)
        if self.geom is None:
            with Phase(self.phase_s, "camera sweep"):
                ex.sweep(x, y, angle, depth_row)
        else:
            # Brief 6.5: the sweep and the visibility fill do the same job, so only one of them runs.
            # The camera still drives local avoidance in the executor -- that is a reflex about the next
            # half second, not a claim about where the floor is.
            ex.visited[ex.cell(x, y)] = ex.visited.get(ex.cell(x, y), 0) + 1
            ex.free.add(ex.cell(x, y))
        ex.remember_items(x, y, state.labels)
        for lab in state.labels:
            if lab.object_name in SOLID_THINGS and 24 < math.hypot(lab.object_position_x - x, lab.object_position_y - y) < 400:
                ix, iy = ex.wpx(lab.object_position_x, lab.object_position_y)
                if 1 <= ix < ex.n - 1 and 1 <= iy < ex.n - 1:
                    ex.barrier_t[iy - 1:iy + 2, ix - 1:ix + 2] = now   # a barrel or pillar: an obstacle while remembered
        enemies = []
        for lab in state.labels:
            if lab.object_name in ENEMIES:
                b = bearing_deg(x, y, angle, lab.object_position_x, lab.object_position_y)
                enemies.append((abs(b), b, math.hypot(lab.object_position_x - x, lab.object_position_y - y), lab))
        enemies.sort(key=lambda e: e[0])
        clear_fwd = self.band_clearance(near_row, -12, 12)
        clear_fl = self.band_clearance(near_row, 20, 45)
        clear_fr = self.band_clearance(near_row, -45, -20)
        h = depth.shape[0]
        floor_row = depth[int(h * STEP_BAND[0]):int(h * STEP_BAND[1])].max(axis=0)
        floor_ahead = self.band_clearance(floor_row, -12, 12)
        self.floor_seen.append(floor_ahead)
        step_ahead = False
        # Brief 6.5: the step detector, the barrier learner and the clearance workaround are all ways of
        # guessing at a floor height from a camera that cannot see one. With the heights themselves in
        # hand they are not a second opinion, they are noise -- and a barrier mark the geometry disagrees
        # with is a hole punched in a map that was right.
        if self.geom is None and len(self.floor_seen) >= STEP_CALIB_MIN:
            level = sorted(self.floor_seen)[int(0.8 * len(self.floor_seen))]
            step_ahead = floor_ahead < STEP_RATIO * level and clear_fwd > 120
            if step_ahead and STEP_MARKS_BARRIER and self.last_move[0] > 0:
                # A barrier the planner can route around, triggered by seeing the step rather than by
                # having failed to move. That distinction is the whole difference: the push heuristic
                # fired about 130 times in 180 seconds, could not tell a step from a shoulder brushed
                # while turning, and fragmented the map until the player was down to nineteen cells.
                # This fires about eighteen times, and only when the camera can point at the thing.
                ex.mark_barrier(x, y, angle, STUCK_BARRIER_S, dist=max(24, floor_ahead))
            if step_ahead and self.game_time - self.step_said > 2.0:
                self.step_said = self.game_time
                print("[payload] step ahead at (%.0f,%.0f): floor reads %d where level ground reads %d, "
                      "eye level clear to %d" % (x, y, floor_ahead, level, clear_fwd), flush=True)
        # Sensed, not acted on. The detector finds the right thing -- it fires at (1325,-3196) and
        # (1327,-3227), which is where the rub log had the player wedged at full throttle with its heading
        # dead on -- but handing it to the executor as an obstacle made that flight worse: clamping the
        # clearance puts the avoidance guard at its harshest, and a raised floor is not always a wall,
        # since Doom lets the player climb anything up to 24 units. Which of those it is takes the height,
        # not just the range, and it takes more than one flight to tell: the same code has reached 0.06
        # and 0.84 of the way to this exit on different runs. STEP_ACTS is the switch for that experiment.
        if step_ahead and STEP_ACTS:
            clear_fwd = min(clear_fwd, 40)
        if slow:
            with Phase(self.phase_s, "map rays"):
                self.sense = self.slow_sense(x, y, angle, now, clear_fwd, enemies)
        s = self.sense
        # stuck: a motion command has been held for a while and the player did not get anywhere
        self.positions.append((x, y))
        # Whatever actually drove the player this tic. This read self.control, which is the legacy CONTROL
        # command -- never populated when the executor is flying, so `pushing` was always false and the
        # barrier learner has been dead for the whole executor era. That is why a flight could spend 41%
        # of its ticks at full throttle with rel=1 and four hundred units of clear floor ahead, grinding
        # on a step the depth camera cannot see, and never once remember a barrier there: the mechanism
        # for learning "something is in the way that I cannot see" was watching a variable nobody wrote.
        self.motions.append(self.last_move)
        # Measured, against three attempts to improve it. Requiring nearly every tic of the second instead
        # of eight of thirty-five produced *more* barriers, not fewer, because it was changed alongside a
        # shorter expiry and the player simply re-learned the same ledge as each one lapsed; widening the
        # mark to cover the whole step made it worse again. Barriers per flight went 41, 95, 133, 216 and
        # the distance reached went 0.75, 0.06, 0.22, 0.17. This is the setting that produced the 0.75.
        pushing = (len(self.motions) == self.motions.maxlen
                   and sum(1 for m in self.motions if m != (0, 0)) >= 8)
        was_stuck = self.stuck
        self.stuck = bool(pushing and math.hypot(x - self.positions[0][0], y - self.positions[0][1]) < 12)
        if self.stuck and not was_stuck and LEARN_BARRIERS_BY_PUSHING and self.geom is None:
            mv, st = self.control["move"], self.control["strafe"]
            push = math.degrees(math.atan2(-st, mv)) if (mv or st) else 0.0
            if s["ahead_kind"] not in ("door", "exit") or abs(push) > 45:
                ex.mark_barrier(x, y, angle + push, STUCK_BARRIER_S)
                print(f"[payload] stuck pushing at ({x:.0f},{y:.0f}) toward {(angle + push) % 360:.0f} deg: barrier remembered", flush=True)
                self.sense = None
        # Settle the last press: did the way ahead open up?
        if self.press_watch is not None and self.game_time - self.press_watch[3] > DOOR_OPEN_WAIT_S:
            px, py, before, _t = self.press_watch
            opened = clear_fwd > before + DOOR_OPEN_UNITS
            if self.world.note_door_try(px, py, now, opened=opened) and opened:
                self.press_opened += 1
            elif opened:
                self.press_opened += 1
            self.press_watch = None
        # A suspect the player is standing in front of, with nothing to open, is settled here and now.
        # Only while there are suspects: with exact geometry a door is a sector whose ceiling is at its
        # own floor, and there is nothing to disprove.
        if self.geom is None:
            self.doors_settled += self.world.settle_by_arrival(x, y, angle, s["ahead_kind"], s["ahead_dist"])
        if state.tic % SENSE_EVERY == 6:
            self.frontiers_dropped += self.world.note_frontier_reached(x, y, now)
        # doors: presses are counted while something usable is at arm's length; a door that never opens becomes a wall for a while
        usable = s["ahead_kind"] in ("door", "exit") and s["ahead_dist"] <= 80
        self.use_ok = usable
        if usable:
            at = (round(x / 32), round(y / 32), round(angle / 45))
            if at != self.door_at:
                self.door_at, self.door_presses = at, 0
            if self.door_presses >= DOOR_TRIES and s["ahead_kind"] == "door":
                ex.mark_barrier(x, y, angle, DOOR_RETRY_S, dist=s["ahead_dist"] + 6)
                print(f"[payload] door at ({x:.0f},{y:.0f}) did not open after {self.door_presses} presses; a wall for a while", flush=True)
                self.door_presses, self.sense = 0, None
        elif clear_fwd > 120:
            self.door_presses = 0
        # Where the flight's time goes. The executor's tick accounting is not downlinked -- there is no
        # channel for it -- so on a flight this line is the only way to tell a player that is walking from
        # one that is aiming. The bench and the flight run the same executor and came back 209 units per
        # second against 93, which is a difference nobody could explain from the telemetry that exists.
        if (self.world.empty_reason is not None and self.game_time - self.empty_said > 5.0):
            self.empty_said = self.game_time
            print("[payload] nowhere to go at (%.0f,%.0f): %s" % (x, y, self.world.empty_reason),
                  flush=True)
            self.world.empty_reason = None
        if state.tic % (TICRATE * 30) == 0 and self.executor is not None:
            # The tic rate itself, every 30 s. The charter's budget is 35 Hz and the brief's floor is 33;
            # until this line existed the only evidence either way was that flight crossed a level at
            # half the speed the bench did, which could have been anything.
            wall = time.time() - self.episode_wall
            self.phase_said = {k: v for k, v in self.phase_s.items()}
            print("[payload] tic rate %.1f/s over %.0f s of wall clock (floor 33)"
                  % (state.tic / max(1e-6, wall), wall), flush=True)
            st, n = self.executor.stats, max(1, self.executor.stats.get("ticks", 1))
            print("[payload] %4.0fs  move %d%%  full %d%%  turn %d%%  look %d%%  recover %d%%  rub %d%%"
                  "  safe %d%%  noplan %d%%  intents %d stale %d" %
                  (self.game_time, *[round(100 * st.get(k, 0) / n) for k in
                                     ("move_ticks", "full_speed_ticks", "turn_ticks", "look_ticks",
                                      "recover_ticks", "rub_ticks", "safe_ticks", "no_plan_ticks")],
                   st.get("intents", 0), st.get("stale_dropped", 0)), flush=True)
        # Only when something is going to look at it. On the bench nothing is, and building a PIL image
        # of the whole map once a second is pure cost on the one loop that has a deadline.
        if state.tic % 35 == 0 and (self.args.map_png or self.frame_hz):
            img = ex.render(x, y, angle)
            if img is not None:
                if state.tic % 175 == 0:
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    self.map_png_bytes = buf.getvalue()
                if self.args.map_png:
                    try:
                        img.save(self.args.map_png + ".tmp.png")
                        os.replace(self.args.map_png + ".tmp.png", self.args.map_png)
                    except OSError:
                        pass
        # ---- the world model for this attempt (charter 3.2): what is here, where it can go, and the way there
        self.world.see_objects(x, y, state.labels, int(state.tic))
        # Ask the camera about every door suspect in view. A closed door is solid; a window, a ledge or a
        # step in the ceiling is not, and the camera sees straight past it.
        if state.tic % SENSE_EVERY == 5 and self.geom is None:
            self.world.confirm_doors(x, y, angle, lambda rel: self.depth_at(near_row, rel), now)
        if state.tic % SENSE_EVERY == 4:
            with Phase(self.phase_s, "candidates"):
                self.candidates = self.world.candidates(x, y, angle, now, keys_held=ex.keys,
                                                        need=self.need_now(), boss_names=BOSS_CLASSES)
        # what the executor gets every tic, at control rate
        all_blocked = all(r[0] <= 48 for r in s["rays"].values())
        self.exec_obs = {"x": x, "y": y, "angle": angle, "clear_fwd": clear_fwd, "clear_fl": clear_fl,
                         "clear_fr": clear_fr, "clear_back": min(s["rays"]["back"][0], 65535),
                         "enemies": enemies, "ahead_kind": s["ahead_kind"], "ahead_dist": s["ahead_dist"],
                         "all_blocked": all_blocked, "expire_barriers": self.expire_barriers,
                         "has_ammo": self.have_ammo()}
        # the worst class in view, by the fixed order the ground reads the byte with
        worst_enemy = NO_ENEMY
        for _b, _rel, _d, lab in enemies:
            idx = ENEMY_INDEX.get(lab.object_name)
            if idx is not None and (worst_enemy == NO_ENEMY or idx > worst_enemy):
                worst_enemy = idx
        weapon = {1: 0, 2: 1, 3: 2}.get(int(self.var("SELECTED_WEAPON")), 3)
        item = lambda k: ex.nearest_item(x, y, k)
        item_b = lambda k: bearing_deg(x, y, angle, item(k)[1]["x"], item(k)[1]["y"]) if item(k) else 0.0
        item_d = lambda k: int(min(item(k)[0], 65535)) if item(k) else 0
        hint = ex.hint if ex.hint and now < ex.hint[1] else None
        exit_b = bearing_deg(x, y, angle, s["exit_seen"][0], s["exit_seen"][1]) if s["exit_seen"] else 0.0
        rays, nov, doors = s["rays"], s["nov"], s["doors"]
        return dict(
            health=int(self.var("HEALTH")), armor=int(self.var("ARMOR")),
            shells=int(self.var("AMMO3")), bullets=int(self.var("AMMO2")),
            weapon=weapon, own_shotgun=int(self.var("WEAPON3") > 0), kills=int(self.var("KILLCOUNT")),
            x=x, y=y, angle=angle,
            enemy_count=len(enemies), enemy_bearing=enemies[0][1] if enemies else 0.0,
            enemy_dist=int(min(enemies[0][2], 65535)) if enemies else 0,
            clear_fwd=clear_fwd, clear_fl=clear_fl, clear_fr=clear_fr,
            clear_left=min(rays["left"][0], 65535), clear_right=min(rays["right"][0], 65535), clear_back=min(rays["back"][0], 65535),
            clear_map_fwd=min(rays["fwd"][0], 65535),
            new_fwd=nov["fwd"], new_left=nov["left"], new_right=nov["right"], new_back=nov["back"],
            ahead_kind=AHEAD_KINDS.index(s["ahead_kind"]), ahead_dist=int(min(s["ahead_dist"], 65535)),
            exit_bearing=exit_b, exit_dist=int(min(s["exit_seen"][2], 65535)) if s["exit_seen"] else 0,
            key_bearing=item_b("key"), key_dist=item_d("key"),
            health_item=item_d("health"), ammo_item=item_d("ammo"), armor_item=item_d("armor"),
            health_bearing=item_b("health"), ammo_bearing=item_b("ammo"), armor_bearing=item_b("armor"),
            stuck=int(self.stuck), door_ahead=int(usable), goal=GOALS.index(self.goal),
            tic=int(state.tic), episode=self.episode,
            dead=int(self.game.is_player_dead()),
            level_done=int(self.game.is_episode_finished() and not self.game.is_player_dead()),
            explored=min(len(ex.visited), 65535),
            level=self.level, keys=sum(KEY_BIT[k] for k in ex.keys),
            hint_active=int(hint is not None),
            hint_rel=int(round(((hint[0] - angle + 180) % 360) - 180)) if hint else 0,
            clear_al=min(rays["al"][0], 65535), clear_ar=min(rays["ar"][0], 65535), clear_bl=min(rays["bl"][0], 65535), clear_br=min(rays["br"][0], 65535),
            new_al=nov["al"], new_ar=nov["ar"], new_bl=nov["bl"], new_br=nov["br"],
            door_fwd=doors["fwd"], door_al=doors["al"], door_left=doors["left"], door_bl=doors["bl"],
            door_back=doors["back"], door_br=doors["br"], door_right=doors["right"], door_ar=doors["ar"],
            cand_count=len(self.candidates),
            threat_class=worst_enemy, threat_count=min(255, len(enemies)),
            door_presses_total=self.press_total, door_opens_total=self.press_opened,
            candidates=[self.pack_candidate(c, x, y, angle, self.world.outwardness(c, x, y))
                        for c in self.candidates])

    def reveal_oracle_exit(self, x, y):
        """ORACLE: hand the exit over, on the rung of the ladder this attempt is flying.

        The work is in payload/oracle.py, which is the only file allowed to write a map class it did not
        see; keeping it out of here is what lets honesty test 5 stay strict about everything else.
        """
        if self.oracle_mod is not None and self.oracle_exits:
            self.oracle_mod.reveal_exit(self.explorer, self.oracle_exits, self.oracle == "L0", x, y)

    def need_now(self):
        """What a detour would actually be for. Nothing, most of the time."""
        if self.var("HEALTH") < 50:
            return "health"
        if self.var("AMMO3") < 6 and self.var("AMMO2") < 20:
            return "ammo"
        if self.var("ARMOR") < 25:
            return "armor"
        return None

    def have_ammo(self):
        w = int(self.var("SELECTED_WEAPON"))
        return self.var("AMMO3") > 0 if w == 3 else (self.var("AMMO2") > 0 if w == 2 else True)

    def expire_barriers(self):
        """Charter 4: barrier marks need an expiry, or late in a run every direction reads blocked."""
        self.explorer.barrier_t[:] = -1e9
        self.sense = None
        print("[payload] barrier marks expired (watchdog)", flush=True)

    @staticmethod
    def pack_candidate(c, x, y, angle, outward=1):
        colour = {"red": 1, "blue": 2, "yellow": 3}.get(c.colour, 0)
        return (c.kind, float(c.x), float(c.y), int(min(65535, c.path_units)), int(min(255, c.novelty)),
                # bits 0-1 key colour, bits 2-5 tries, bits 6-7 how far out it is from the spawn
                colour | (min(15, c.tries) << 2) | ((outward & 3) << 6),
                c.threat_class, c.threat_count,
                int(min(65535, c.opening)), int(min(65535, c.depth)), int(bool(c.away)))

    @staticmethod
    def pack_status(o):
        cands = list(o.get("candidates") or [])[:MAX_CANDIDATES]
        tail = [min(MAX_CANDIDATES, int(o["cand_count"]))]
        for c in cands:
            tail.extend(c)
        tail.extend([0, 0.0, 0.0, 0, 0, 0, 255, 0, 0, 0, 0] * (MAX_CANDIDATES - len(cands)))
        tail.extend([int(o.get("threat_class", 255)), int(o.get("threat_count", 0))])
        tail.extend([min(65535, int(o.get("door_presses_total", 0))),
                     min(65535, int(o.get("door_opens_total", 0)))])
        return Payload._pack_core(o) + struct.pack(
            "!" + "B" + CAND_FMT * MAX_CANDIDATES + THREAT_FMT + DOOR_FMT, *tail)

    @staticmethod
    def _pack_core(o):
        return struct.pack(STATUS_FMT, o["health"], o["armor"], o["shells"], o["bullets"], o["weapon"], o["own_shotgun"],
                           o["kills"], o["x"], o["y"], o["angle"], o["enemy_count"], o["enemy_bearing"], o["enemy_dist"],
                           o["clear_fwd"], o["clear_fl"], o["clear_fr"], o["clear_left"], o["clear_right"], o["clear_back"], o["clear_map_fwd"],
                           o["new_fwd"], o["new_left"], o["new_right"], o["new_back"],
                           o["ahead_kind"], o["ahead_dist"], o["exit_bearing"], o["exit_dist"], o["key_bearing"], o["key_dist"],
                           o["health_item"], o["ammo_item"], o["armor_item"], o["health_bearing"], o["ammo_bearing"], o["armor_bearing"],
                           o["stuck"], o["door_ahead"], o["goal"], o["tic"], o["episode"], o["dead"], o["level_done"], o["explored"],
                           o["level"], o["keys"], o["hint_active"], o["hint_rel"],
                           o["clear_al"], o["clear_ar"], o["clear_bl"], o["clear_br"], o["new_al"], o["new_ar"], o["new_bl"], o["new_br"],
                           o["door_fwd"], o["door_al"], o["door_left"], o["door_bl"],
                           o["door_back"], o["door_br"], o["door_right"], o["door_ar"])

    # ------------------------------------------------------------------ uplink
    def handle(self, kind, body):
        self.cmd_count += 1
        if kind == 0x10 and len(body) >= 9:
            move, strafe, turn, fire, use, weapon = struct.unpack("!bbfBBB", body[:9])
            self.control = dict(move=move, strafe=strafe, turn=max(-180.0, min(180.0, turn)), fire=fire, use=use, weapon=weapon)
            self.turn_remaining = self.control["turn"]  # degrees still to turn, positive left
            self.last_control_time = time.time()
        elif kind == 0x11 and body:
            self.goal = GOALS[body[0]] if body[0] < len(GOALS) else "HOLD"
            print(f"[payload] goal -> {self.goal}", flush=True)
        elif kind == 0x15 and len(body) >= INTENT_LEN:
            # INTENT (charter 3.1): what to do and for how long, instead of buttons for one tic.
            (iid, based, mode, tx, ty, has_t, stance, fire, ftid, weapon, use_at, ttl) = struct.unpack(INTENT_FMT, body[:INTENT_LEN])
            self.executor.set_intent(ex_mod.Intent(
                intent_id=iid, based_on_tic=based, mode=ex_mod.MODES[mode] if mode < len(ex_mod.MODES) else "EXPLORE",
                target_x=tx, target_y=ty, has_target=bool(has_t),
                stance=ex_mod.STANCES[stance] if stance < len(ex_mod.STANCES) else "advance",
                fire_policy=fire, fire_target_id=ftid, weapon=weapon, use_at_target=bool(use_at), ttl_ms=ttl),
                now=self.game_time)
            self.last_control_time = time.time()
            if has_t and self.exec_obs is not None:
                cand = self._candidate_at(tx, ty)
                px, py = self.exec_obs["x"], self.exec_obs["y"]
                plan = self.world.route_to(px, py, cand, time.time()) if cand else None
                if plan is None or not plan.cells:
                    # No route to what the ground asked for. Walking the straight line at it is what
                    # froze the player; take the nearest thing there IS a route to instead, and if
                    # there is nothing, say so rather than aiming at a wall.
                    for alt in sorted(self.candidates, key=lambda c: c.path_units):
                        if alt is cand:
                            continue
                        plan = self.world.route_to(px, py, alt, time.time(), force=True)
                        if plan and plan.cells:
                            self.executor.intent.target_x, self.executor.intent.target_y = alt.x, alt.y
                            self.replanned_elsewhere += 1
                            break
                    else:
                        self.executor.intent.has_target = False
                        self.unroutable += 1
        elif kind == 0x12:
            self.new_episode()
        elif kind == 0x13 and len(body) >= 2:
            self.frame_hz, self.quality = body[0], max(10, min(95, body[1]))
            print(f"[payload] frames {self.frame_hz} Hz quality {self.quality}", flush=True)
        elif kind == 0x14 and len(body) >= 3:
            rel, ttl = struct.unpack("!hB", body[:3])
            self.explorer.hint = (self.var("ANGLE") + rel, time.time() + ttl)
            print(f"[payload] explore hint {rel:+d} deg for {ttl} s", flush=True)
        elif kind == wu.KIND_LOAD_WAD:
            self.request_wad(body)
        else:
            print(f"[payload] unknown uplink kind {kind:#x}", flush=True)

    # ------------------------------------------------------------------ LOAD_WAD
    def wad_report(self, result, name="", reason=""):
        return wu.encode_wad_report(result, self.wad_loads, os.path.basename(self.wad),
                                    os.path.basename(self.pwad) if self.pwad else "", name, self.map, reason)

    def request_wad(self, body):
        """Check a LOAD_WAD, and start proving the game runs on it in a child process.

        Proven elsewhere first because a damaged WAD does not raise in ViZDoom: it kills the process (a
        truncated doom1.wad segfaults in init), and this process holds the flight link. The game here keeps
        running until the child has flown a second on the new file; only then does it switch.
        """
        try:
            iwad, pwad, map_name = wu.decode_load_wad(body)
        except ValueError:
            self.outbox.append(self.wad_report(wu.FAILED, "?", "malformed LOAD_WAD record"))
            return
        name, map_name = wu.display_name(iwad, pwad), map_name.upper()   # next_map() reads upper case
        ipath = ppath = None
        if self.wad_job is not None:
            why = "another LOAD_WAD is still being checked"
        elif self.oracle != "off":
            why = "the diagnostic ladder (--oracle) only knows the level it was launched on"
        else:
            ipath, ppath, why = wu.resolve(iwad, pwad, map_name)
        if why:
            print(f"[payload] LOAD_WAD {name}: {why}", flush=True)
            self.outbox.append(self.wad_report(wu.FAILED, name, why))
            return
        # The child proves, and then this process loads, the same file: pin it (a hard link), or failing that
        # remember what it was and check nothing was renamed over it in between.
        self.wad_serial += 1
        pinned = [wu.pin(p, self.wad_serial) if p else None for p in (ipath, ppath)]
        if pinned[0] and (pinned[1] or not ppath):
            ipath, ppath = pinned
        else:
            wu.unpin(self.wad_serial)
        ident = (wu.identity(ipath), wu.identity(ppath) if ppath else None)
        cmd = [sys.executable, os.path.abspath(__file__), "--probe", "--wad", ipath, "--map", map_name,
               "--skill", str(self.args.skill), "--seed", str(self.args.seed), "--geometry", self.geometry]
        if ppath:
            cmd += ["--pwad", ppath]
        out = tempfile.TemporaryFile()
        # Its own process group: a child that dies or is killed leaves its ViZDoom engine running (as an
        # orphan, still loading, still growing), so it is the group that gets killed.
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        request = dict(name=name, map=map_name, ipath=ipath, ppath=ppath, serial=self.wad_serial, ident=ident)
        self.wad_job = (proc, out, request, time.time())
        print(f"[payload] LOAD_WAD {name} on {map_name}: proving the game starts on it", flush=True)

    def poll_wad(self):
        """Finish a LOAD_WAD once its child is done. True when the game here was rebuilt."""
        proc, out, request, started = self.wad_job
        code = proc.poll()
        if code is None and time.time() - started < WAD_PROBE_TIMEOUT_S:
            return False
        self.wad_job = None
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # the child's engine, whatever became of the child
        except OSError:
            pass
        proc.wait()
        out.seek(0)
        said = [ln.strip() for ln in out.read().decode("utf-8", "replace").splitlines()
                if ln.strip() and not ln.startswith("[payload]")]
        out.close()
        if code is None:
            # Both hang silently: a damaged PWAD while loading, and a map the WAD does not have once loaded
            why = (f"the map would not start within {WAD_PROBE_TIMEOUT_S:.0f} s (is it in that WAD?)"
                   if PROBE_LOADED in said else
                   f"the game had not loaded it after {WAD_PROBE_TIMEOUT_S:.0f} s (a truncated or damaged file?)")
        elif code != 0:
            said = [ln for ln in said if ln != PROBE_LOADED]
            how = f"killed by signal {-code}" if code < 0 else f"exit {code}"
            why = f"the game would not start on it ({how}{': ' + said[-1][:60] if said else ''})"
        elif (wu.identity(request["ipath"]), wu.identity(request["ppath"]) if request["ppath"] else None) != request["ident"]:
            why = "the file changed while it was being proven (a new uplink of the same name?); send LOAD_WAD again"
        else:
            why = self.switch_wad(request["ipath"], request["ppath"], request["map"])
        if why is None:
            if self.wad_pinned is not None:
                wu.unpin(self.wad_pinned)               # the links of the WAD that just stopped flying
            self.wad_pinned = request["serial"]
            self.wad_loads += 1
            self.outbox.append(self.wad_report(wu.LOADED, request["name"]))
            print(f"[payload] LOAD_WAD {request['name']}: now flying it on {self.map}", flush=True)
            return True
        print(f"[payload] LOAD_WAD {request['name']}: {why}", flush=True)
        self.outbox.append(self.wad_report(wu.FAILED, request["name"], why))
        wu.unpin(request["serial"])
        return False

    def switch_wad(self, ipath, ppath, map_name):
        """Rebuild the game on another WAD; the process, and with it the flight link, stays up.

        The new game is built before the old one is closed, so a build that fails leaves the old game flying.
        """
        old = (self.wad, self.pwad, self.map)
        self.wad, self.pwad, self.map = ipath, ppath, map_name
        try:
            game = self._make_game()
        except Exception as e:  # noqa: BLE001  the old game never stopped
            self.wad, self.pwad, self.map = old
            return f"the game would not start on it ({type(e).__name__}: {str(e)[:60]})"
        self.game.close()
        self.game = game
        # A different game: nothing carries over, not the loadout, the count of levels played, the last
        # observation of the old one, or a map image of it still waiting to go down
        self.carry, self.level, self.explorer_map = None, 0, None
        self.last_obs = self.exec_obs = self.map_png_bytes = None
        self.new_episode()
        return None

    def probe(self, tics=TICRATE):
        """--probe: fly a second on this WAD through the whole sensing path, then exit 0 (LOAD_WAD's check)."""
        for tic in range(tics):
            if self.game.is_episode_finished() or self.game.is_player_dead():
                break
            obs = self.observe(self.game.get_state())
            self.game.make_action(self.action(tic), 1)
            self.pack_status(obs)
        self.game.close()
        return 0

    def _candidate_at(self, tx, ty):
        """The offered candidate the ground meant. Never an invented one.

        This used to fall back to "go to that point as a bare frontier" when nothing on the list matched
        within 64 units -- and the list is rebuilt while an intent is in flight, so a near-miss was
        common. A bare point has no route: the planner finds nothing, the executor is left with a target
        and no plan, and it walks the straight line into whatever is between. Every freeze in a dev
        attempt looked exactly like that: APPROACH, advance, a target, no plan, 150 units of clear space
        ahead and nothing attacking.

        The ground can only ever mean something it was offered, so match to the nearest one.
        """
        if not self.candidates:
            return None
        return min(self.candidates, key=lambda c: math.hypot(c.x - tx, c.y - ty))

    def buttons(self, cmd):
        """A command from the executor as the button vector ViZDoom wants.

        `turn` is in degrees with positive meaning left, the way every bearing in this project is signed;
        the engine's TURN_LEFT_RIGHT_DELTA runs the other way, and the sign flip belongs here rather than
        in six separate callers.
        """
        out = [0.0] * len(BUTTONS)
        out[BUTTON_INDEX[vzd.Button.MOVE_FORWARD_BACKWARD_DELTA]] = cmd["move"]
        out[BUTTON_INDEX[vzd.Button.MOVE_LEFT_RIGHT_DELTA]] = cmd["strafe"]
        out[BUTTON_INDEX[vzd.Button.TURN_LEFT_RIGHT_DELTA]] = -cmd["turn"]
        out[BUTTON_INDEX[vzd.Button.ATTACK]] = int(cmd["fire"])
        out[BUTTON_INDEX[vzd.Button.USE]] = int(cmd["use"])
        slot = int(cmd.get("weapon", ex_mod.WEAPON_KEEP))
        if slot in WEAPON_BUTTON:
            out[BUTTON_INDEX[WEAPON_BUTTON[slot]]] = 1
        return out

    def action(self, tic):
        """One tic of control.

        The executor drives whenever the ground has sent an INTENT. The CONTROL path is kept underneath it
        so the graph from before the charter still flies and the two can be compared on the bench; it is
        the executor that the charter's phase 2 exit test measures.
        """
        self.game_time += 1.0 / TICRATE
        if self.executor is not None and self.executor.intent is not None and self.exec_obs is not None:
            cmd = self.executor.step(self.exec_obs, self.game_time)
            self.last_move = (cmd["move"], cmd["strafe"])
            if cmd["use"] and self.use_ok:
                self.door_presses += 1
                self.press_total += 1
                # Watch the way ahead. A door that opens reveals the room behind it, so the clearance
                # jumps; a wall absorbs the press and nothing changes. Judged a moment later, in
                # observe(), because the engine takes a few tics to move the ceiling.
                #
                # Only one press is ever in flight. Presses are pulsed every eight tics and the verdict
                # needs twenty-one, so re-arming on every press overwrote the pending watch before it
                # could ever be read: note_door_try was reached almost never, no suspect was ever marked
                # not-a-door, and one flight spent 568 Use presses proving nothing. The first press is
                # the one that answers the question anyway.
                if self.press_watch is None:
                    self.press_watch = (self.exec_obs["x"], self.exec_obs["y"],
                                        self.exec_obs["clear_fwd"], self.game_time)
            return self.buttons(cmd)
        c = self.control
        if time.time() - self.last_control_time > UPLINK_TIMEOUT_S:
            return [0.0] * len(BUTTONS)  # safe mode: no uplink, hold still
        self.last_move = (RUN_FORWARD * c["move"], RUN_STRAFE * c["strafe"])
        use = int(c["use"]) and int(tic % 8 == 0)  # Doom triggers USE on the press edge: pulse a held use
        if use and self.use_ok:
            self.door_presses += 1
        step = max(-10.0, min(10.0, self.turn_remaining))  # onboard attitude loop: turn to the setpoint, then stop
        self.turn_remaining -= step
        return self.buttons({"move": RUN_FORWARD * c["move"], "strafe": RUN_STRAFE * c["strafe"], "turn": step,
                             "fire": int(c["fire"]), "use": use,
                             "weapon": {1: 2, 2: 3}.get(int(c["weapon"]), ex_mod.WEAPON_KEEP)})

    # ------------------------------------------------------------------ main loop
    def serve(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.args.port))
        srv.listen(1)
        print(f"[payload] listening on {self.args.port}", flush=True)
        while True:
            conn, _ = srv.accept()
            conn.setblocking(False)
            print("[payload] flight software connected", flush=True)
            try:
                self.run(conn)
            except (ConnectionError, OSError) as e:
                print(f"[payload] link dropped: {e}", flush=True)
            finally:
                conn.close()

    def run(self, conn):
        self.send(conn, wu.KIND_WAD, self.wad_report(wu.REPORT))   # which file the game is running
        inbuf = b""
        next_frame = 0.0
        t0 = time.perf_counter()
        tic = 0
        while True:
            try:
                data = conn.recv(65536)
                if data == b"":
                    return
                inbuf += data
            except BlockingIOError:
                pass
            while len(inbuf) >= 4 and inbuf[0] == ord("D"):
                kind, length = inbuf[1], struct.unpack("!H", inbuf[2:4])[0]
                if len(inbuf) < 4 + length:
                    break
                self.handle(kind, inbuf[4:4 + length])
                inbuf = inbuf[4 + length:]
            if inbuf and inbuf[0] != ord("D"):
                inbuf = b""  # resync
            if self.wad_job is not None and self.poll_wad():
                t0, tic = time.perf_counter(), 0
            while self.outbox:
                self.send(conn, wu.KIND_WAD, self.outbox.pop(0))
            if self.game.is_episode_finished() or self.game.is_player_dead():
                died = self.game.is_player_dead()
                if self.last_obs is not None:
                    self.last_obs["dead"] = int(died)
                    self.last_obs["level_done"] = int(not died)
                    self.send(conn, 1, self.pack_status(self.last_obs))
                print(f"[payload] episode {self.episode} over on {self.map}: {'died' if died else 'LEVEL FINISHED'} at tic {tic}", flush=True)
                time.sleep(2.0)
                if died:
                    self.new_episode()
                else:
                    self.level_finished()
                t0, tic = time.perf_counter(), 0
                continue
            state = self.game.get_state()
            obs = self.observe(state)
            self.last_obs = obs
            self.game.make_action(self.action(tic), 1)
            tic += 1
            if tic % self.args.status_every == 0:
                self.send(conn, 1, self.pack_status(obs))
            now = time.perf_counter()
            if self.frame_hz and now >= next_frame:
                next_frame = now + 1.0 / self.frame_hz
                buf = io.BytesIO()
                Image.fromarray(state.screen_buffer).resize((320, 240), Image.BILINEAR).save(buf, format="JPEG", quality=self.quality)
                self.frame_seq += 1
                self.send(conn, 2, struct.pack("!I", self.frame_seq) + buf.getvalue())
            if self.map_png_bytes and self.frame_hz and len(self.map_png_bytes) < 60000:
                self.map_seq += 1   # map products share the frame path; the high bit of seq marks them
                self.send(conn, 2, struct.pack("!I", 0x80000000 | self.map_seq) + self.map_png_bytes)
                self.map_png_bytes = None
            remaining = t0 + tic / TICRATE - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            elif remaining < -1.0:
                t0 = time.perf_counter() - tic / TICRATE

    @staticmethod
    def send(conn, kind, body):
        conn.sendall(b"D" + bytes([kind]) + struct.pack("!H", len(body)) + body)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=4242)
    p.add_argument("--wad", default="doom1.wad", help="IWAD file name or path (shareware doom1.wad, freedoom1.wad, freedoom2.wad)")
    p.add_argument("--map", default="E1M1", help="first map; the payload advances to the next map when a level is finished")
    p.add_argument("--skill", type=int, default=2)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--fps", type=int, default=10, help="frame downlink rate")
    p.add_argument("--quality", type=int, default=45, help="JPEG quality")
    p.add_argument("--status-every", type=int, default=3, help="status record every N tics (35 Hz game)")
    p.add_argument("--map-png", default=None, help="also write the self-built map here every second (diagnostics)")
    p.add_argument("--geometry", default="off", choices=["off", "on"],
                   help="exact lines from the engine, released only once the automap has drawn them "
                        "(payload/seen_geometry.py). Honest; off is the pre-23-September sensing.")
    p.add_argument("--oracle", default="off", choices=["off", "L0", "L1", "L2"],
                   help="DIAGNOSTIC LADDER, never scored: L0 the whole level and the exit, L1 seen "
                        "geometry with the exit revealed once looked at, L2 the stack as flown")
    p.add_argument("--pwad", default=None, help="a PWAD to load over the IWAD (LOAD_WAD switches both in flight)")
    p.add_argument("--probe", action="store_true",
                   help="fly one second on --wad/--pwad and exit 0 if the game ran: LOAD_WAD's check, run "
                        "in a child process because a damaged WAD kills the process rather than raising")
    args = p.parse_args()
    if args.probe:
        # If the payload that started this probe dies before it can, a watchdog takes the probe and its engine
        # down. A process, not a thread: ViZDoom holds the GIL while it hangs, so a thread would never run.
        # The payload's killpg when the probe ends takes the watchdog with it.
        if os.getpgrp() == os.getpid() and os.fork() == 0:
            time.sleep(WAD_PROBE_TIMEOUT_S + 5)
            os.killpg(0, signal.SIGKILL)
            os._exit(0)
        sys.exit(Payload(args).probe())
    wu.unpin()   # pinned links a previous flight left behind (only the flight payload pins or clears them)
    payload = Payload(args)

    def _stop(*_):
        # scripts/flight.sh stop and every restart send SIGTERM; without this the ViZDoom engine (and a
        # LOAD_WAD probe's) outlives the payload, one more orphan per restart
        if payload.wad_job is not None:
            try:
                os.killpg(payload.wad_job[0].pid, signal.SIGKILL)
            except OSError:
                pass
        payload.game.close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    payload.serve()


if __name__ == "__main__":
    main()
