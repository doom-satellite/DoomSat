"""The payload's own record of each episode, written as a small file the ground can downlink (SDS Phase D).

Off unless the payload runs with --records on (scripts/flight.sh: RECORDS=on). Then, for every episode, it
keeps a running account of what the payload itself sent: how many STATUS records, the first and last tic, the
final values, a position every second of game time, and the context the episode began in (the base and patch
WAD being flown, which LOAD_WAD can switch, map, skill, seed). When the episode ends it writes
$DOOMSAT_HOME/run/rec/<episode>-<last tic>.json. The ground's science data system asks for that file with
cfdpManager's SendFile, which downlinks it over CFDP, and checks it against the Level 1 record it built from the
Yamcs archive: same episode, same ending, and how many of the statuses sent actually reached the ground.

The name carries the last tic because the episode number restarts with the payload; the ground knows both (the
PlayerDied/LevelFinished event says the tic), so it can name the file without asking the payload anything. Keep
it short: F' takes command strings of at most 40 characters on board, and the ground keeps to 39.

An episode with no death or exit is closed when the next one's first status arrives, as "reset". A LOAD_WAD that
switches the game starts a new episode too, and the one it cut short is closed the same way: this account sees
only the episode number change (the ground, which hears of the switch, calls that outcome wad_switch).

Nothing on board reads these files. The accumulator is a write-only account of the current episode, started
afresh with every new episode and never consulted by the world model, the executor or the pilot (charter 2.2: no
map survives an attempt). A failure to write is reported and ignored; it can never stop the game.
"""
import json
import os
import struct
import time

PATH_EVERY_TICS = 35          # one position per second of game time


def f32(v):
    """A float as the ground sees it after a trip through an F32 channel, so positions compare exactly."""
    return float("%.9g" % struct.unpack("!f", struct.pack("!f", float(v)))[0])


def directory():
    return os.path.join(os.environ.get("DOOMSAT_HOME") or os.path.expanduser("~/doom"), "run", "rec")


def from_args(args, payload):
    """The recorder for a payload, or None. Read with getattr: the bench builds args without this option."""
    if getattr(args, "records", "off") != "on":
        return None
    return EpisodeRecorder(directory(), lambda: {
        "wad": os.path.basename(payload.wad), "pwad": os.path.basename(payload.pwad) if payload.pwad else None,
        "map": payload.map, "level": payload.level,
        "skill": getattr(args, "skill", None), "seed": getattr(args, "seed", None),
        "geometry": payload.geometry, "oracle": payload.oracle})


class EpisodeRecorder:
    def __init__(self, path, context):
        self.path = path
        self.context = context          # called at the start of each episode
        self.started = time.time()
        self.cur = None

    def _start(self, o):
        self.cur = {"episode": int(o["episode"]), "context": self.context(), "statuses_sent": 0,
                    "first_tic": int(o["tic"]), "start_wall": time.time(), "health_min": int(o["health"]),
                    "path": [], "written": False}

    def status(self, o):
        """A STATUS with these values has just been (or is about to be) sent."""
        if self.cur is not None and int(o["episode"]) != self.cur["episode"]:
            if not self.cur["written"]:
                self.close("reset")         # a new episode with no death or exit in between: RESET_GAME or LOAD_WAD
            self.cur = None
        if self.cur is None:
            self._start(o)
        c = self.cur
        # The death or exit path sends last_obs once more; when the last periodic status was that same tic, it is
        # the same status again, and the ground (which counts distinct tics) would never see it as a second one.
        if "last" not in c or int(o["tic"]) != c["last"]["tic"]:
            c["statuses_sent"] += 1
        c["last"] = {"tic": int(o["tic"]), "health": int(o["health"]), "kills": int(o["kills"]),
                     "x": f32(o["x"]), "y": f32(o["y"]), "explored": int(o["explored"]), "keys": int(o["keys"]),
                     "level": int(o["level"]), "dead": int(o["dead"]), "level_done": int(o["level_done"])}
        c["health_min"] = min(c["health_min"], int(o["health"]))
        if not c["path"] or int(o["tic"]) - c["path"][-1][0] >= PATH_EVERY_TICS:
            c["path"].append([int(o["tic"]), f32(o["x"]), f32(o["y"])])

    def close(self, outcome):
        """The current episode ended this way: write its file now (before the ground hears of it)."""
        c = self.cur
        if c is None or c["written"] or "last" not in c:
            return None
        c["written"] = True
        last_tic = c["last"]["tic"]
        if c["path"][-1][0] != last_tic:
            c["path"].append([last_tic, c["last"]["x"], c["last"]["y"]])
        now = time.time()
        record = {
            "record": {"format": "doomsat-episode-record", "version": 1},
            "episode": c["episode"], "outcome": outcome, "context": c["context"],
            "payload": {"pid": os.getpid(), "started_unix": round(self.started, 3)},
            "wall": {"start_unix": round(c["start_wall"], 3), "end_unix": round(now, 3),
                     "duration_s": round(now - c["start_wall"], 3)},
            "statuses_sent": c["statuses_sent"], "first_tic": c["first_tic"], "last_tic": last_tic,
            "final": c["last"], "health_min": c["health_min"],
            "path_every_tics": PATH_EVERY_TICS, "path": c["path"],
        }
        name = os.path.join(self.path, "%d-%d.json" % (c["episode"], last_tic))
        try:
            os.makedirs(self.path, exist_ok=True)
            tmp = name + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, name)
            print("[payload] episode record %s (%s)" % (name, outcome), flush=True)
            return name
        except (OSError, TypeError, ValueError) as e:
            print("[payload] episode record not written: %s" % e, flush=True)
            return None
