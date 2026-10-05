"""The ground pilot: plays Doom through Yamcs.

Live play (every ~0.5 s):
  telemetry (Yamcs WebSocket) -> a structured state -> jev (System One): one Score per open sector, a
  danger Score when an enemy is in view, a goal Choice every few ticks -> code picks the direction, the
  mode and the buttons -> CONTROL / SET_GOAL commands (Yamcs -> F Prime -> payload)
  frames (FRAME_CHUNK records) -> reassembled JPEG -> Yamcs bucket + DoomFrame parameter (Open MCT)
Between episodes (death, level finished, run end):
  code writes an after-action report -> Claude Sonnet 5 (System Two) revises the decision graph
  jev plays with (question wording, rubric levels, thresholds, selection numbers) -> next episode uses it.

Code owns the loop, the mode machine, the thresholds, the hysteresis and the command mapping; jev judges,
Sonnet reviews. Every row of the decision log carries the exact state jev saw, so a run can be replayed
against a new graph without the game (tools/replay.py).

Without jev (no key needed):
  --system-one code    the exact rules answer every head: the bench's code baseline, flown on the full stack
  --system-one manual  a person drives from the dashboard (http://localhost:8070); its keys go up as CONTROL
                       commands through Yamcs. The pilot does the rest of the ground segment: frames, ground
                       parameters, and a log in out/manual.jsonl.
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path

from yamcs.client import YamcsClient

import after_action
import decision_graph as dg
import graph_config as gc
import targeting
from providers import make_system_one, make_system_two

HERE = Path(__file__).resolve().parent
SPACE_SYSTEM = "/DoomSat_DoomSat/DoomSat/doom"
GROUND = "/DoomGround"
STATUS_CHANNELS = ["HEALTH", "ARMOR", "SHELLS", "BULLETS", "WEAPON", "OWN_SHOTGUN", "KILLS", "POS_X", "POS_Y", "ANGLE",
                   "ENEMY_COUNT", "ENEMY_BEARING", "ENEMY_DIST", "CLEAR_FWD", "CLEAR_LEFT", "CLEAR_RIGHT", "CLEAR_BACK",
                   "CLEAR_FL", "CLEAR_FR", "CLEAR_MAP_FWD", "NEW_FWD", "NEW_LEFT", "NEW_RIGHT", "NEW_BACK", "AHEAD_KIND", "AHEAD_DIST",
                   "EXIT_BEARING", "EXIT_DIST", "KEY_BEARING", "KEY_DIST", "HEALTH_ITEM_DIST", "AMMO_ITEM_DIST", "ARMOR_ITEM_DIST",
                   "HEALTH_BEARING", "AMMO_BEARING", "ARMOR_BEARING", "STUCK", "DOOR_AHEAD", "GOAL", "TIC", "EPISODE", "DEAD",
                   "LEVEL_DONE", "EXPLORED_CELLS", "LEVEL", "KEYS", "HINT_ACTIVE", "HINT_REL",
                   "CLEAR_AL", "CLEAR_AR", "CLEAR_BL", "CLEAR_BR", "NEW_AL", "NEW_AR", "NEW_BL", "NEW_BR",
                   "DOOR_FWD", "DOOR_AL", "DOOR_LEFT", "DOOR_BL", "DOOR_BACK", "DOOR_BR", "DOOR_RIGHT", "DOOR_AR",
                   "FRAMES_SENT", "CHUNKS_SENT", "FRAME_BYTES", "PAYLOAD_LINK", "CMDS_RECEIVED",
                   # charter 3.3: the candidate targets the onboard world model offers for scoring
                   "CAND_COUNT", "CAND0", "CAND1", "CAND2", "CAND3", "CAND4", "CAND5", "CAND6", "CAND7",
                   "INTENT_ID", "WATCHDOG_TRIPS", "DOOR_PRESSES", "DOOR_OPENS",
                   # what is actually threatening the player; without these the engage head
                   # is asked about "something" and can only guess
                   "THREAT_CLASS", "THREAT_COUNT"]
CHUNK_HEADER = struct.Struct("!IHHH")  # seq, index, count, length (then 960 data bytes)
_FIRE_NAME = {0: "NONE", 1: "ANY_ATTACKER", 2: "NEAREST", 3: "TARGET"}


# Everything build_state reads, so a logged row can be replayed exactly (verification step 1).
RAW_KEYS = ("CLEAR_FWD", "CLEAR_LEFT", "CLEAR_RIGHT", "CLEAR_BACK", "CLEAR_AL", "CLEAR_AR", "CLEAR_BL", "CLEAR_BR",
            "CLEAR_MAP_FWD", "NEW_FWD", "NEW_LEFT", "NEW_RIGHT", "NEW_BACK", "NEW_AL", "NEW_AR", "NEW_BL", "NEW_BR",
            "DOOR_FWD", "DOOR_AL", "DOOR_LEFT", "DOOR_BL", "DOOR_BACK", "DOOR_BR", "DOOR_RIGHT", "DOOR_AR",
            "AHEAD_KIND", "AHEAD_DIST", "EXIT_DIST", "EXIT_BEARING", "KEY_DIST", "KEY_BEARING",
            "HEALTH_ITEM_DIST", "HEALTH_BEARING", "AMMO_ITEM_DIST", "AMMO_BEARING", "ARMOR_ITEM_DIST", "ARMOR_BEARING",
            "STUCK", "POS_X", "POS_Y", "ANGLE", "ENEMY_COUNT", "ENEMY_BEARING", "ENEMY_DIST",
            "HEALTH", "ARMOR", "SHELLS", "BULLETS", "WEAPON", "OWN_SHOTGUN",
            "EXPLORED_CELLS", "LEVEL", "LEVEL_DONE", "KEYS", "HINT_ACTIVE", "HINT_REL",
            "DOOR_PRESSES", "DOOR_OPENS", "EXIT_DIST", "THREAT_CLASS", "THREAT_COUNT")


def _knowledge():
    """How Doom works (charter 2.1). Values only; the file names no level and a test enforces that."""
    import yaml
    path = HERE.parent / "knowledge" / "doom_rules.yaml"
    return yaml.safe_load(open(path, encoding="utf-8"))


class FrameAssembler:
    """Puts FRAME_CHUNK records back together into JPEG files; tolerates loss and reordering."""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.partial = {}
        self.complete = 0
        self.incomplete = 0
        self.maps = 0
        self.last_path = None

    def add(self, value):
        raw = value if isinstance(value, (bytes, bytearray)) else self._from_aggregate(value)
        if raw is None or len(raw) < CHUNK_HEADER.size:
            return None
        seq, index, count, length = CHUNK_HEADER.unpack_from(raw)
        frame = self.partial.setdefault(seq, {"count": count, "parts": {}, "t0": time.time()})
        frame["parts"][index] = bytes(raw[CHUNK_HEADER.size:CHUNK_HEADER.size + length])
        done = None
        if len(frame["parts"]) == count:
            data = b"".join(frame["parts"][i] for i in range(count))
            if seq & 0x80000000:   # the navigator's map, a PNG product
                path = self.out_dir / "latest_map.png"
                path.write_bytes(data)
                self.maps += 1
            else:
                path = self.out_dir / f"frame-{seq:06d}.jpg"
                path.write_bytes(data)
                (self.out_dir / "latest.jpg").write_bytes(data)
                self.complete += 1
                self.last_path = path
            del self.partial[seq]
            done = (seq, path)
        for old in [s for s, f in self.partial.items() if time.time() - f["t0"] > 3.0]:
            self.incomplete += 1
            del self.partial[old]
        return done

    @staticmethod
    def _from_aggregate(value):
        try:
            d = value if isinstance(value, dict) else dict(value)
            data = d["data"] if isinstance(d["data"], (bytes, bytearray)) else bytes(int(b) & 0xFF for b in d["data"])
            return CHUNK_HEADER.pack(int(d["seq"]), int(d["index"]), int(d["count"]), int(d["length"])) + data
        except Exception:
            return None


class Pilot:
    def __init__(self, args):
        self.args = args
        self.client = YamcsClient(args.yamcs)             # commands + telemetry subscription
        self.pub_client = YamcsClient(args.yamcs)         # frame uploads and ground parameters, off the command path
        self.instance = args.instance
        self.processor = self.client.get_processor(args.instance, "realtime")
        self.pub_processor = self.pub_client.get_processor(args.instance, "realtime")
        self.cfg = gc.load()
        # The graph carries the System One version its numbers were tuned against; the flag still overrides it.
        args.system_one_model = args.system_one_model or self.cfg.get("model")
        self.manual = args.system_one == "manual"
        self.system_one = None if self.manual else make_system_one(args.system_one, args, self.cfg)
        self.system_two = make_system_two(args.system_two, args) if args.after_action else None
        self.pilot_name = "manual" if self.manual else self.system_one.name
        self.telemetry = {}
        self.telemetry_time = 0.0
        self.subscription = None
        self.resubscribes = 0
        self.frames = FrameAssembler(args.out_dir / "frames")
        self.goal = "EXPLORE"
        self.control_count = 0
        self.last_cmd_ms = 0
        self.pending_turn, self.pending_turn_t, self.angle_at_cmd = 0.0, 0.0, None
        self.nav_memory = dg.NavMemory(self.cfg)
        self.target_memory = targeting.TargetMemory(self.cfg)
        self.decision_cache = targeting.DecisionCache()   # charter 3.4
        self.rules = _knowledge()
        self.mode = "EXPLORE"
        self.skipped_mid_turn = 0
        self.code_only_ticks = 0
        self.last_publish = 0.0
        self.last_stats = 0.0
        self.last_print = 0.0
        self.announce_ok = True
        # A person's drive is not a decision log: kept apart so no tool mistakes it for a pilot's flight.
        log_name = "manual.jsonl" if self.manual else "decisions.jsonl"
        self.log = open(args.out_dir / log_name, "a", buffering=1, encoding="utf-8")
        self.rows = []                 # this run's decision rows (for after-action)
        self.episode = None
        self.level = None
        self.episode_start_row = 0
        self.episode_outcome = None    # "died" / "level finished" seen in telemetry
        self.review_busy = False
        self.progress = []           # (time, explored cells, level) samples
        self.last_hint_t = time.time()
        self.bump_busy = False
        self.level_start_t = time.time()
        self.attempt = 1
        self.bucket = self._ensure_bucket()

    # ------------------------------------------------------------ Yamcs plumbing
    def _ensure_bucket(self):
        storage = self.pub_client.get_storage_client()
        bucket = None
        for b in storage.list_buckets():
            if b.name == "doomframes":
                bucket = b
        if bucket is None:
            storage.create_bucket("doomframes")
            bucket = storage.get_bucket("doomframes")
        # Yamcs caps a bucket at 1000 objects: start clean so uploads never fail on leftovers from earlier runs
        try:
            for o in list(bucket.list_objects().objects):
                bucket.delete_object(o.name)
        except Exception as e:
            print(f"[pilot] bucket cleanup failed: {e}", file=sys.stderr)
        return bucket

    def subscribe(self):
        names = [f"{SPACE_SYSTEM}/{c}" for c in STATUS_CHANNELS + ["FRAME_CHUNK"]]
        self.subscription = self.processor.create_parameter_subscription(names, on_data=self.on_data)
        self.telemetry_time = time.time()

    def resubscribe(self):
        """The Yamcs WebSocket drops now and then; rebuild the client and the subscription."""
        try:
            self.subscription.cancel()
        except Exception:
            pass
        try:
            self.client = YamcsClient(self.args.yamcs)
            self.processor = self.client.get_processor(self.instance, "realtime")
            self.subscribe()
            self.resubscribes += 1
            print(f"[pilot] telemetry went stale: resubscribed (#{self.resubscribes})", flush=True)
        except Exception as e:
            print(f"[pilot] resubscribe failed: {e}", file=sys.stderr)

    def on_data(self, data):
        for pv in data.parameters:
            name = pv.name.rsplit("/", 1)[-1]
            if name == "FRAME_CHUNK":
                done = self.frames.add(pv.raw_value if pv.raw_value is not None else pv.eng_value)
                if done:
                    self.publish_frame(*done)
            else:
                v = pv.eng_value
                if v == "True" or v == "False":  # F´ bools arrive as enumerated strings; "False" must not be truthy
                    v = v == "True"
                self.telemetry[name] = v
                self.telemetry_time = time.time()
                if name == "DEAD" and v:
                    self.episode_outcome = "died"
                elif name == "LEVEL_DONE" and v:
                    self.episode_outcome = "level finished"

    def publish_frame(self, seq, path):
        """Put the image product in the Yamcs bucket for Open MCT, at most --publish-hz times a second (maps: every time)."""
        if seq & 0x80000000:
            try:
                with open(path, "rb") as f:
                    self.bucket.upload_object("map.png", f)
                self.set_ground({"DoomMap": f"{self.args.yamcs_public}/api/buckets/{self.instance}/doomframes/objects/map.png?s={seq & 0xFFFF}"})
            except Exception as e:
                print(f"[pilot] map publish failed: {e}", file=sys.stderr)
            return
        if time.time() - self.last_publish < 1.0 / self.args.publish_hz:
            return
        self.last_publish = time.time()
        name = f"frame-{seq % 20:02d}.jpg"   # a ring of 20 objects: the bucket never fills, nothing to delete
        try:
            with open(path, "rb") as f:
                self.bucket.upload_object(name, f)
            url = f"{self.args.yamcs_public}/api/buckets/{self.instance}/doomframes/objects/{name}?s={seq}"
            self.set_ground({"DoomFrame": url, "FrameSeq": seq, "FramesComplete": self.frames.complete,
                             "FramesIncomplete": self.frames.incomplete})
            if seq > 40:
                (self.frames.out_dir / f"frame-{seq - 40:06d}.jpg").unlink(missing_ok=True)
        except Exception as e:
            print(f"[pilot] frame publish failed: {e}", file=sys.stderr)

    def set_ground(self, values):
        try:
            for k, v in values.items():
                self.pub_processor.set_parameter_value(f"{GROUND}/{k}", v)
        except Exception as e:
            print(f"[pilot] ground parameter set failed: {e}", file=sys.stderr)

    def announce(self):
        """Tell the displays who is flying (jev, code or manual). A call of its own: a Yamcs started with an
        older ground database has no PilotMode, and that must not take the other ground values down too."""
        if not self.announce_ok:
            return
        try:
            self.pub_processor.set_parameter_value(f"{GROUND}/PilotMode", self.pilot_name)
        except Exception as e:
            self.announce_ok = False
            print(f"[pilot] PilotMode is not in this Yamcs's ground database (restart Yamcs to load it): {e}",
                  file=sys.stderr)

    def command(self, name, args=None):
        t0 = time.time()
        r = self.processor.issue_command(f"{SPACE_SYSTEM}/{name}", args=args or {})
        self.last_cmd_ms = int((time.time() - t0) * 1000)
        return r

    # ------------------------------------------------------------ System One: live play
    def control_step(self, n):
        if time.time() - self.telemetry_time > 4.0:
            self.resubscribe()
            return None
        if time.time() - self.telemetry_time > 2.0:
            return None  # stale telemetry: the payload holds the last controls, then its own uplink timeout releases them
        t = dict(self.telemetry)
        cfg = self.cfg
        # Never judge the sectors while a commanded turn is still swinging. Every sector is named relative to
        # the heading, so a decision taken mid-turn is a decision about a world that has already moved; the old
        # code only waited for turns of 60 degrees or more and compensated a bearing nothing subscribed to.
        settle = float(cfg["thresholds"]["turn_settle_deg"])
        if abs(self.pending_turn) > settle and self.angle_at_cmd is not None and "ANGLE" in t:
            done = (t["ANGLE"] - self.angle_at_cmd + 180) % 360 - 180
            if abs(self.pending_turn - done) > settle and time.time() - self.pending_turn_t < 1.5:
                self.skipped_mid_turn += 1
                return None
        self.pending_turn = 0.0

        # How stale the telemetry already was when this decision started. It is the first of the three
        # terms in decision age (charter 7), and the only one nothing else can recover after the fact.
        tel_age_ms = round((time.time() - self.telemetry_time) * 1000.0)
        if self.args.control == "intent":
            return self.intent_step(n, t, cfg, tel_age_ms)
        # One decision, in the same function the bench runner calls. A harness that reimplemented this
        # would be measuring the harness.
        d = dg.decide(t, cfg, self.nav_memory, self.goal, self.system_one, n)
        state, mode, sent = d["state"], d["mode"], d["sent"]
        questions, answers, reply = d["questions"], d["answers"], d["reply"]
        pick, detail, cargs = d["pick"], d["detail"], d["control"]
        self.mode = mode
        if d["code_only"]:
            self.code_only_ticks += 1          # OPERATE / RECOVER / DONE: exact rules, nothing to judge
        self.command("CONTROL", cargs)
        self.pending_turn, self.pending_turn_t, self.angle_at_cmd = cargs["turn"], time.time(), t.get("ANGLE")
        self.control_count += 1
        if "goal" in answers:
            self.set_goal(dg.GOAL_FROM_CHOICE.get(answers["goal"].get("choice"), self.goal))
        row = {"t": time.time(), "kind": "control", "episode": self.episode, "graph_version": cfg.get("version"),
               "pinned_model": cfg.get("model"), "latency_ms": reply.get("latency_ms", 0),
               "model": reply.get("model"), "request_id": reply.get("request_id"), "usage": reply.get("usage"),
               "cmd_ms": self.last_cmd_ms, "tel_age_ms": tel_age_ms,
               "mode": mode, "goal": self.goal, "pick": pick, "select": detail,
               "answers": {k: dg.answer_label(v) for k, v in answers.items()},
               "confidence": {k: round(dg.answer_confidence(v), 2) for k, v in answers.items()},
               "probabilities": {k: {o: round(float(pv), 2) for o, pv in (v.get("probabilities") or {}).items()}
                                 for k, v in answers.items() if isinstance(v, dict) and v.get("probabilities")},
               "control": cargs, "health": t.get("HEALTH"), "kills": t.get("KILLS"), "tic": t.get("TIC"),
               # exactly what jev saw, so the run can be replayed against another graph (verification step 1)
               "state": sent, "here": state["here"], "seen": state["sectors"],
               "questions_sha": hashlib.sha1(json.dumps(questions, sort_keys=True).encode()).hexdigest()[:12],
               "raw": {k: t.get(k) for k in RAW_KEYS}}
        if self.args.log_questions:
            row["questions"] = questions
        self.log.write(json.dumps(row) + "\n")
        self.rows.append(row)
        if time.time() - self.last_stats > 1.0:
            self.last_stats = time.time()
            self.set_ground({"SystemOneLatencyMs": float(reply.get("latency_ms", 0)), "ControlCommands": self.control_count,
                             "Controls": f"{mode} {pick or '-'} | " +
                                         " ".join(f"{k}={dg.answer_label(v)}" for k, v in answers.items())})
        return row

    def intent_step(self, n, t, cfg, tel_age_ms):
        """One decision under the charter's architecture: score the targets, send an INTENT with a TTL.

        The player does not stop while this is in flight -- the onboard executor is still carrying out the
        last intent -- which is the whole point of charter 3.1 and the difference between an EXPLORE speed
        of 66 units per second and one worth reporting.
        """
        cands = targeting.candidates_from(t)
        ask_need = bool(cfg["goal_every"]) and n % cfg["goal_every"] == 0
        d = targeting.decide(t, cands, cfg, self.target_memory, self.system_one, self.rules, n,
                             ask_need=ask_need, cache=self.decision_cache)
        if d["code_only"]:
            self.code_only_ticks += 1
        it = d["intent"]
        self.mode = it["mode"]
        self.command("INTENT", {
            "intentId": it["intent_id"], "basedOnTic": it["based_on_tic"], "mode": it["mode"],
            "targetX": it["target_x"], "targetY": it["target_y"], "hasTarget": it["has_target"],
            "stance": it["stance"].upper(), "firePolicy": _FIRE_NAME[it["fire_policy"]],
            "fireTargetId": it["fire_target_id"], "weapon": it["weapon"],
            "useAtTarget": it["use_at_target"], "ttlMs": it["ttl_ms"]})
        self.control_count += 1
        reply = d["reply"]
        row = {"t": time.time(), "kind": "control", "episode": self.episode, "graph_version": cfg.get("version"),
               "pinned_model": cfg.get("model"), "latency_ms": reply.get("latency_ms", 0),
               "model": reply.get("model"), "request_id": reply.get("request_id"), "usage": reply.get("usage"),
               "cmd_ms": self.last_cmd_ms, "tel_age_ms": tel_age_ms,
               "mode": it["mode"], "goal": self.goal, "pick": d["pick"], "select": d["detail"],
               "answers": {k: dg.answer_label(v) for k, v in d["answers"].items()},
               "confidence": {k: round(dg.answer_confidence(v), 2) for k, v in d["answers"].items()},
               "control": it, "candidates": len(cands), "cached": d["cached"],
               # where the candidates were, for the post-flight overlay. Ground-side diagnostics only;
               # nothing reads it back into a decision.
               "cand_xy": [{"kind": c["kind"], "x": round(c["x"], 1), "y": round(c["y"], 1)} for c in cands],
               "health": t.get("HEALTH"), "kills": t.get("KILLS"), "tic": t.get("TIC"),
               "state": d["sent"], "here": d["state"]["here"], "needs": d["needs"],
               "questions_sha": hashlib.sha1(json.dumps(d["questions"], sort_keys=True).encode()).hexdigest()[:12],
               "raw": {k: t.get(k) for k in RAW_KEYS}}
        if self.args.log_questions:
            row["questions"] = d["questions"]
        self.log.write(json.dumps(row) + "\n")
        self.rows.append(row)
        if time.time() - self.last_stats > 1.0:
            self.last_stats = time.time()
            target = "-" if d["pick"] is None else "%s@%.0fu" % (cands[d["pick"]]["kind"],
                                                                 cands[d["pick"]]["path_units"])
            self.set_ground({"SystemOneLatencyMs": float(reply.get("latency_ms", 0)),
                             "ControlCommands": self.control_count,
                             "Controls": "%s -> %s (%d candidates)" % (it["mode"], target, len(cands))})
        return row

    def set_goal(self, goal):
        if goal in dg.GOALS and goal != self.goal:
            self.command("SET_GOAL", {"goal": goal})
            print(f"[pilot] goal {self.goal} -> {goal} (jev)", flush=True)
            self.goal = goal

    # ------------------------------------------------------------ manual: a person drives from the dashboard
    def manual_step(self):
        """No decision to take. The dashboard sends the person's keys as CONTROL commands through Yamcs, the
        same uplink the pilots use; this loop keeps up the rest of the ground segment (the telemetry
        subscription, the image product, the ground parameters) and logs what happened."""
        if time.time() - self.telemetry_time > 4.0:
            self.resubscribe()
            return
        t = self.telemetry
        now = time.time()
        if now - self.last_stats > 1.0:
            self.last_stats = now
            self.set_ground({"Controls": "MANUAL: a person is driving from the dashboard. "
                                         "uplinks=%s" % t.get("CMDS_RECEIVED", "-")})
            self.announce()
            self.log.write(json.dumps({"t": now, "kind": "manual", "episode": self.episode,
                                       "raw": {k: t.get(k) for k in RAW_KEYS}}) + "\n")
        if now - self.last_print > 5.0:
            self.last_print = now
            print(f"[pilot] manual: level {t.get('LEVEL')} hp={t.get('HEALTH')} kills={t.get('KILLS')} "
                  f"uplinks={t.get('CMDS_RECEIVED')} frames ok={self.frames.complete} lost={self.frames.incomplete}",
                  flush=True)

    # ------------------------------------------------------------ System Two: a bump when the walk stalls
    def check_stall(self):
        """Every --bump-every seconds, System Two looks at the map and the walk and pushes exploration somewhere;
        after --level-budget seconds without finishing the level, the game is reset (and, with a System Two, the episode
        reviewed). The budget holds with or without a System Two, never while a person drives."""
        t = self.telemetry
        if "EXPLORED_CELLS" not in t or self.manual:
            return
        now = time.time()
        self.progress.append((now, int(t.get("EXPLORED_CELLS", 0)), t.get("LEVEL")))
        self.progress = [p for p in self.progress if now - p[0] < 120.0]
        if self.args.level_budget and now - self.level_start_t > self.args.level_budget:
            print(f"[pilot] level {self.level} not finished within {self.args.level_budget:.0f} s: reset, review, try again (attempt {self.attempt + 1})", flush=True)
            self.episode_outcome = f"level not finished within the {self.args.level_budget:.0f} s budget (attempt {self.attempt})"
            self.command("RESET_GAME")
            self.level_start_t = now
            self.attempt += 1
            self.last_hint_t = now
            return
        if self.system_two is None:   # the bump is System Two's: without one the budget is all this does
            return
        if not self.args.bump_every or self.bump_busy or now - self.last_hint_t < self.args.bump_every:
            return
        self.last_hint_t = now
        self.bump_busy = True
        threading.Thread(target=self.stall_consult, daemon=True).start()

    def ascii_map(self, path, cols=72):
        """The navigator's map product as text Sonnet can read: # wall, . seen floor, o walked, F frontier, P player."""
        try:
            from PIL import Image
            im = Image.open(path).convert("RGB")
        except Exception:
            return None
        w, h = im.size
        step = max(1, w // cols)
        rows = []
        for yy in range(0, h, step):
            line = ""
            for xx in range(0, w, step):
                r, g, b = im.getpixel((min(xx + step // 2, w - 1), min(yy + step // 2, h - 1)))
                if r > 200 and g < 100 and b < 100:
                    line += "P"
                elif r > 200 and g > 150 and b < 100:
                    line += "F"
                elif r > 180 and g > 180 and b > 180:
                    line += "#"
                elif g > r + 30 and g > b + 30:
                    line += "o"
                elif r + g + b > 150:
                    line += "."
                else:
                    line += " "
            rows.append(line.rstrip())
        return "\n".join(rows)

    def stall_consult(self):
        try:
            from collections import Counter
            t = dict(self.telemetry)
            recent = [r for r in self.rows[-240:] if r.get("kind") == "control" and r.get("raw")]
            pts = [(round(r["raw"]["POS_X"] / 64) * 64, round(r["raw"]["POS_Y"] / 64) * 64) for r in recent if r["raw"].get("POS_X") is not None]
            path = [f"({x},{y}) x{n}" for (x, y), n in Counter(pts).most_common(8)]
            amap = self.ascii_map(self.frames.out_dir / "latest_map.png")
            old = [p for p in self.progress if time.time() - p[0] >= 50.0]
            gained = (self.progress[-1][1] - old[0][1]) if old and self.progress else 0
            prompt = ("Progress check. {:.0f} s into this level attempt (budget {:.0f} s), {} new map cells in the last minute. "
                      "Position ({:.0f}, {:.0f}), heading {:.0f} degrees "
                      "(0 = east, 90 = north). Level {}. Ahead: {}. Exit line seen at: {} units. Stuck: {}. Keys: {}.\n"
                      "Where the walk has been in the last two minutes (64-unit bins, most visited first): {}.\n"
                      "Map the navigator built (# wall, . seen floor, o walked, F unexplored edge, P player; north is up):\n{}\n\n"
                      "Pick a compass direction to push exploration toward unexplored space away from the well-trodden area "
                      "(the exit is somewhere unexplored), as a bearing in degrees (0 east, 90 north, 180 west, 270 south), how "
                      "many seconds to hold it, and the goal to set. One sentence of reasoning.").format(
                time.time() - self.level_start_t, self.args.level_budget, gained,
                t.get("POS_X", 0), t.get("POS_Y", 0), t.get("ANGLE", 0), t.get("LEVEL"), t.get("AHEAD_KIND"), t.get("EXIT_DIST"),
                t.get("STUCK"), t.get("KEYS"), path, amap or "(no map product yet)")
            schema = {"type": "object", "properties": {"bearing_deg": {"type": "integer"}, "hold_s": {"type": "integer"},
                                                       "goal": {"type": "string", "enum": ["Explore", "Scout"]},
                                                       "reason": {"type": "string", "maxLength": 300}},
                      "required": ["bearing_deg", "hold_s", "reason"], "additionalProperties": False}
            t0 = time.time()
            r = self.system_two.structured("You are the mission's System Two. The fast model plays; you only redirect exploration "
                                           "when it stalls. Answer from the map.", prompt, schema)
            rel = int(round((r["bearing_deg"] - float(t.get("ANGLE", 0)) + 180) % 360 - 180))
            ttl = int(max(20, min(120, r.get("hold_s", 60))))
            self.command("EXPLORE_HINT", {"bearing": rel, "ttl": ttl})
            if r.get("goal") == "Scout":
                self.set_goal("SCOUT")
            row = {"t": time.time(), "kind": "system_two_hint", "bearing_deg": r["bearing_deg"], "relative": rel, "ttl": ttl,
                   "goal": r.get("goal"), "reason": r.get("reason"), "latency_ms": int((time.time() - t0) * 1000),
                   "model": r.get("model"), "cost_usd": r.get("cost_usd"), "path": path}
            self.log.write(json.dumps(row) + "\n")
            self.rows.append(row)
            self.set_ground({"SystemTwoHint": f"push {r['bearing_deg']} deg for {ttl} s: {r.get('reason')}"[:900],
                             "SystemTwoLatencyMs": float(row["latency_ms"])})
            print(f"[pilot] System Two bump: bearing {r['bearing_deg']} deg (relative {rel:+d}) for {ttl} s, goal {r.get('goal')}: "
                  f"{r.get('reason')}", flush=True)
        except Exception as e:
            print(f"[pilot] stall consult failed: {e}", file=sys.stderr)
        finally:
            self.bump_busy = False

    # ------------------------------------------------------------ System Two: after-action review
    def episode_boundary(self):
        """Called when the telemetry episode number changes or the run ends."""
        rows = self.rows[self.episode_start_row:]
        outcome = self.episode_outcome or "run ended"
        self.episode_start_row = len(self.rows)
        self.episode_outcome = None
        if self.system_two is None or len(rows) < 20 or self.review_busy:
            return
        report = after_action.summarise(self.rows, rows, outcome, self.cfg)
        if report is None:
            return
        self.review_busy = True
        threading.Thread(target=self.review, args=(report,), daemon=True).start()

    def review(self, report):
        try:
            print(f"[pilot] after-action review ({report['outcome']}, {report['decisions']} decisions, graph v{self.cfg.get('version')})...", flush=True)
            new_cfg, rationale, issues, meta = after_action.review(self.system_two, report, self.cfg)
            changes = gc.diff(self.cfg, new_cfg)
            if self.args.auto_apply_graph:
                new_cfg = gc.save(new_cfg, rationale, issues, meta.get("model"))
                self.cfg = new_cfg
                where = "graph v%d (applied)" % new_cfg["version"]
            else:
                # One episode is one sample. The revision waits for tools/promote_graph.py, which replays
                # it against the states this run logged before it is allowed to fly.
                path = gc.save_candidate(new_cfg, rationale, issues, meta.get("model"), report)
                where = "candidate %s (not applied; promote with tools/promote_graph.py)" % path.name
                meta["candidate"] = str(path)
            row = {"t": time.time(), "kind": "after_action", "outcome": report["outcome"],
                   "graph_version": self.cfg["version"], "applied": bool(self.args.auto_apply_graph),
                   "rationale": rationale, "issues": issues, "changes": changes, "report": report, **meta}
            self.log.write(json.dumps(row) + "\n")
            self.rows.append(row)
            self.set_ground({"Plan": f"{where}: {rationale}"[:900], "SystemTwoLatencyMs": float(meta["latency_ms"])})
            print(f"[pilot] {where} ({meta.get('model')}, {meta['latency_ms']} ms): {rationale}", flush=True)
            for c in changes:
                print(f"         - {c}", flush=True)
        except Exception as e:
            print(f"[pilot] after-action review failed: {e}", file=sys.stderr)
            self.log.write(json.dumps({"t": time.time(), "kind": "after_action_error", "error": str(e)}) + "\n")
        finally:
            self.review_busy = False

    # ------------------------------------------------------------ main
    def run(self):
        self.subscribe()
        self.command("FRAME_RATE", {"hz": self.args.fps, "quality": self.args.quality})
        if self.manual:
            print("[pilot] MANUAL: no model flies. Open the dashboard (http://localhost:8070), click the picture "
                  "and drive; the keys go up as CONTROL commands through Yamcs.", flush=True)
            if self.args.reset:
                # A fresh game, and a fresh onboard executor: once a pilot has sent an INTENT the executor
                # drives and CONTROL is ignored until the episode restarts.
                self.command("RESET_GAME")
        else:
            key = getattr(self.system_one, "api_key", "")
            two = f"{self.system_two.name} ({getattr(self.system_two, 'model', '-')})" if self.system_two else "none"
            print(f"[pilot] System One = {self.system_one.name}{f' (key {key[:14]}...)' if key else ''} plays; "
                  f"System Two = {two}{' reviews after each episode' if self.system_two else ''}; "
                  f"graph v{self.cfg.get('version')}", flush=True)
            self.command("SET_GOAL", {"goal": self.goal})
        self.announce()
        deadline = time.time() + self.args.duration if self.args.duration else None
        n = 0
        while deadline is None or time.time() < deadline:
            t0 = time.time()
            ep = self.telemetry.get("EPISODE")
            if ep is not None and ep != self.episode:
                if self.episode is not None:
                    self.episode_boundary()
                self.episode = ep
                self.goal = "EXPLORE"
                self.nav_memory = dg.NavMemory(self.cfg)
                self.level_start_t = time.time()   # a new life is a new attempt: a fresh budget
            lv = self.telemetry.get("LEVEL")
            if lv is not None and lv != self.level:
                if self.level is not None:
                    print(f"[pilot] *** LEVEL {self.level} FINISHED -> level {lv} (attempt {self.attempt}, {time.time() - self.level_start_t:.0f} s) ***", flush=True)
                    self.log.write(json.dumps({"t": time.time(), "kind": "level", "finished": self.level, "started": lv, "controls": self.control_count,
                                               "attempt": self.attempt, "seconds": round(time.time() - self.level_start_t)}) + "\n")
                    self.set_ground({"Plan": f"LEVEL {self.level} FINISHED in {time.time() - self.level_start_t:.0f} s (attempt {self.attempt})"})
                self.level = lv
                self.level_start_t = time.time()
                self.attempt = 1
            try:
                row = self.manual_step() if self.manual else self.control_step(n)
            except Exception as e:
                row = None
                print(f"[pilot] {'manual step' if self.manual else 'System One'} failed: {e}", file=sys.stderr)
                time.sleep(1.0)
            n += 1
            if n % 20 == 0:
                self.check_stall()
                self.announce()     # a dashboard opened later still learns who is flying
            if row and n % 10 == 0:
                c = row['control']
                doing = (f"-> {c['mode']} {'target' if c.get('has_target') else 'no target'} "
                         f"{c['stance']} ttl={c['ttl_ms']}ms" if 'mode' in c
                         else f"-> {c['move']}/{c['turn']:.0f}")
                print(f"[pilot] #{n} {self.pilot_name} {row['latency_ms']} ms cmd {row['cmd_ms']} ms  hp={row['health']} "
                      f"{row['mode']} cand={row.get('candidates', '-')} pick={row['pick']} "
                      f"{' '.join(f'{k}={v}' for k, v in row['answers'].items())} {doing}  "
                      f"frames ok={self.frames.complete} lost={self.frames.incomplete}", flush=True)
            remaining = self.args.period - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)
        self.command("CONTROL", dict(dg.STOP))   # an explicit stop, not whatever a default-filled answer set means
        self.episode_boundary()
        while self.review_busy:
            time.sleep(1.0)
        print("[pilot] done", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yamcs", default="localhost:8090", help="Yamcs host:port for the client")
    p.add_argument("--yamcs-public", default="http://localhost:8090", help="Yamcs base URL as the browser reaches it (frame URLs)")
    p.add_argument("--instance", default="fprime-project")
    p.add_argument("--system-one", default="typesafe", choices=["typesafe", "openai", "code", "manual"],
                   help="typesafe: jev (needs TYPESAFE_API_KEY). code: the exact rules, no model, no key. "
                        "manual: a person drives from the dashboard, no model, no key")
    p.add_argument("--system-one-model", default=None)
    p.add_argument("--system-two", default="claude-cli", choices=["claude-cli", "anthropic", "openai", "none"])
    p.add_argument("--system-two-model", default=None, help="e.g. sonnet (CLI alias), claude-sonnet-5, gpt-4o")
    p.add_argument("--no-after-action", dest="after_action", action="store_false", help="never call System Two")
    p.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1"))
    p.add_argument("--env-files", nargs="*", default=[str(HERE / ".env"), str(HERE.parent / ".env")])
    p.add_argument("--period", type=float, default=0.25, help="seconds between control decisions (lower bound)")
    p.add_argument("--bump-every", type=float, default=60.0, help="seconds between System Two progress checks (0 = never)")
    p.add_argument("--level-budget", type=float, default=180.0, help="seconds per level attempt before a reset, and a review with a System Two (0 = none; not when a person drives)")
    p.add_argument("--auto-apply-graph", action="store_true",
                   help="make System Two's revision current immediately instead of saving it as a candidate "
                        "for tools/promote_graph.py (one episode is one sample; off by default)")
    p.add_argument("--log-questions", action="store_true", help="log the full question set on every row (large)")
    p.add_argument("--control", default="intent", choices=["intent", "legacy"],
                   help="intent: the charter's architecture -- score the candidate targets, send an INTENT "
                        "with a time to live, and let the onboard executor carry it out. legacy: the "
                        "pre-charter eight-sector graph sending CONTROL every tick.")
    p.add_argument("--fps", type=int, default=10)
    p.add_argument("--quality", type=int, default=45)
    p.add_argument("--publish-hz", type=float, default=None,
                   help="frames a second put in the Yamcs bucket for the displays (default 2; 10 when manual, "
                        "because a person steering needs to see where they are going)")
    p.add_argument("--no-reset", dest="reset", action="store_false",
                   help="manual: keep the game as it is instead of restarting the episode first")
    p.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (0 = run forever)")
    p.add_argument("--out-dir", type=Path, default=HERE.parent / "out")
    args = p.parse_args()
    if args.system_two == "none" or args.system_one == "manual":
        args.after_action = False   # nothing for System Two to review when a person is driving
    if args.publish_hz is None:
        args.publish_hz = 10.0 if args.system_one == "manual" else 2.0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    Pilot(args).run()


if __name__ == "__main__":
    main()
