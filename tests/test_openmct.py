"""The Open MCT displays (docs/OPENMCT.md) are generated, documented, current, and honest about what they read.

No network, no game, no browser: the displays and the operator reference are rebuilt in memory and compared
with what is committed, the custom views are checked against the reference, and the ground feed and the replay
builder are run on made-up rows.
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "ground"))

import build_openmct_displays as bod   # noqa: E402
import openmct_docs                     # noqa: E402
import ops_telemetry                    # noqa: E402
try:
    import set_yamcs_alarms             # noqa: E402  yamcs-client: ground/.venv
except ImportError:
    set_yamcs_alarms = None

WEB = ROOT / "ground" / "openmct"
LIVE_SCREENS = ("00 ", "10 ", "20 ", "30 ", "40 ", "60 ")
NOT_PANELS = {"conditionSet", "conditionWidget", "comps", "telemetry.correlator"}
BINS = {"DoomSat Displays", "Conditions", "Derived telemetry", "Parts (duplicate into My Items to build new screens)"}


class Displays(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b, cls.root, cls.params, cls.drift = bod.build_all("live")

    def test_committed_displays_are_current(self):
        committed = (WEB / "displays" / "doomsat-displays.json").read_text(encoding="utf-8")
        self.assertEqual(committed, bod.display_json(self.b, self.root),
                         "run: python tools/build_openmct_displays.py --doc")

    def test_plan_start_leaves_the_committed_file_alone(self):
        # a campaign build goes beside the committed file, git-ignored; that one keeps DEFAULT_PLAN_START
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp) / "ground" / "openmct"
            with mock.patch.multiple(bod, ROOT=Path(tmp), WEB=web), contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(sys, "argv", ["build_openmct_displays.py", "--plan-start", "1800000000000"]):
                bod.main()
            written = sorted(f.name for f in (web / "displays").iterdir())
            self.assertEqual(written, ["doomsat-displays.campaign.json"])
            self.assertIn("1800000000000", (web / "displays" / written[0]).read_text(encoding="utf-8"))

    def test_reference_doc_is_current(self):
        committed = (ROOT / "docs" / "OPENMCT.md").read_text(encoding="utf-8")
        self.assertEqual(committed, openmct_docs.render(self.b, self.root, self.params, self.drift),
                         "run: python tools/build_openmct_displays.py --doc")

    def test_every_panel_has_a_note(self):
        missing = sorted(o["name"] for o in self.b.objects.values()
                         if o["type"] not in NOT_PANELS and o["name"] not in BINS
                         and o["name"] not in openmct_docs.NOTES)
        self.assertEqual(missing, [], "add a line to NOTES in tools/openmct_docs.py")

    def test_every_parameter_has_a_meaning(self):
        used = set(self.b.used) | set(openmct_docs.SECTOR_RADAR_INPUTS) | set(openmct_docs.CANDIDATE_BOARD_INPUTS)
        missing = sorted(q for q in used if not openmct_docs.describe(q, self.params))
        self.assertEqual(missing, [], "add a meaning to GLOSSARY in tools/openmct_docs.py")

    def test_cell_size_is_the_payloads(self):
        # EXPLORED_CELLS and candidate novelty count the payload's own cells, not the charter's 128-unit ones
        grid = re.search(r"^GRID = (\d+)", (ROOT / "payload" / "mapclasses.py").read_text(encoding="utf-8"), re.M)[1]
        for q in (f"{openmct_docs.DOOM}/EXPLORED_CELLS", f"{openmct_docs.DOOM}/CAND0.novelty"):
            self.assertIn(f"{grid}-unit cells", openmct_docs.describe(q, self.params), q)
        self.assertIn(f"{grid}-unit cells", openmct_docs.NOTES["Cells seen this attempt"])

    def test_indicator_colours_follow_the_alarm_ranges(self):
        # "Colours mean the same thing everywhere": for a value of a parameter with alarm ranges, the colour the
        # indicator shows is the alarm level the value is in (ranges are the allowed values, inclusive)
        objs = self.b.objects
        colour_of_level = {"critical": bod.BAD, "warning": bod.WARN, "watch": bod.WATCH, None: bod.OK}
        compare = {"lessThan": lambda v, t: v < t, "lessThanOrEq": lambda v, t: v <= t,
                   "greaterThan": lambda v, t: v > t, "greaterThanOrEq": lambda v, t: v >= t}
        widgets = {o["configuration"]["objectStyles"]["conditionSetIdentifier"]["key"]: o for o in objs.values()
                   if o["type"] == "conditionWidget"}

        def level(alarms, v):
            for lvl in ("severe", "distress", "critical", "warning", "watch"):
                lo, hi = alarms.get(lvl, (None, None))
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    return lvl
            return None
        checked = set()
        for key, cs in objs.items():
            if cs["type"] != "conditionSet" or key not in widgets:
                continue
            colour = {s["conditionId"]: s["style"]["backgroundColor"]
                      for s in widgets[key]["configuration"]["objectStyles"]["styles"]}
            coll = cs["configuration"]["conditionCollection"]
            for q in {cr["telemetry"]["key"].replace("~", "/") for c in coll for cr in c["configuration"]["criteria"]}:
                alarms = self.params[q].get("alarms", {})
                if not alarms or "enum" in alarms:
                    continue
                bounds = {b for lo_hi in alarms.values() for b in lo_hi if b is not None}
                bounds |= {cr["input"][0] for c in coll for cr in c["configuration"]["criteria"]
                           if cr["telemetry"]["key"].replace("~", "/") == q}
                for v in sorted({0} | {b + d for b in bounds for d in (-1, -0.5, 0, 0.5, 1)}):
                    def hit(cr):
                        return (cr["telemetry"]["key"].replace("~", "/") == q and cr["operation"] in compare
                                and compare[cr["operation"]](v, cr["input"][0]))
                    first = next(c for c in coll if c.get("isDefault") or (any if c["configuration"]["trigger"]
                                 == "any" else all)(hit(cr) for cr in c["configuration"]["criteria"]))
                    self.assertEqual(colour[first["id"]], colour_of_level[level(alarms, v)],
                                     f"{cs['name']}: {q.rsplit('/', 1)[1]} = {v} shows {first['configuration']['output']}")
                checked.add(q.rsplit("/", 1)[1])
        self.assertTrue({"HEALTH", "ENEMY_COUNT", "DecisionAgeMs", "JevShare"} <= checked, checked)

    def test_every_reference_is_known(self):
        for o in self.b.objects.values():
            for c in o.get("composition", []):
                if c["namespace"] == "taxonomy" and not c["key"].startswith("yamcs."):
                    self.assertIn(c["key"].replace("~", "/"), self.params, o["name"])

    def test_custom_views_read_what_the_reference_says(self):
        # Every parameter plugin.js names, however it is written: a quoted Doom channel ('CLEAR_FWD'), a template
        # (`${DOOM}/POS_X`, `${GROUND}/PickSlot`), or a numbered slot (`${DOOM}/CAND${n}` with the members the
        # board reads, `${GROUND}/Score${n}`). Compared both ways with the inputs docs/OPENMCT.md lists.
        src = (WEB / "doomsat" / "plugin.js").read_text(encoding="utf-8")
        base = {"DOOM": openmct_docs.DOOM, "GROUND": openmct_docs.G}
        drawn = set(re.findall(r"\$\{b\}\.(\w+)", src))
        asked = set(re.findall(r"'(\w+)'", re.search(r"\[([^\]]*)\]\.forEach\(\(m\)", src)[1]))
        self.assertEqual(sorted(drawn - asked), [], "the board draws members it never asks for: always n/r")
        members = drawn | asked
        read = {f"{openmct_docs.DOOM}/{k}" for k in re.findall(r"'([A-Z][A-Z0-9_]{2,})'", src)}
        for ns, name, slot in re.findall(r"\$\{(DOOM|GROUND)\}/(\w+)(\$\{n\})?", src):
            if not slot:
                read.add(f"{base[ns]}/{name}")
            else:
                read |= {f"{base[ns]}/{name}{n}" + (f".{m}" if ns == "DOOM" else "")
                         for n in range(8) for m in (members if ns == "DOOM" else [""])}
        read &= set(self.params)   # commands (SET_GOAL) and labels ('JEV', 'HOLD') are not parameters
        documented = set(openmct_docs.SECTOR_RADAR_INPUTS + openmct_docs.CANDIDATE_BOARD_INPUTS)
        self.assertEqual(sorted(read - documented), [], "plugin.js reads parameters docs/OPENMCT.md does not list")
        self.assertEqual(sorted(documented - read), [], "docs/OPENMCT.md lists inputs plugin.js does not read")

    def test_nothing_from_the_wad_on_a_live_screen(self):
        objs = self.b.objects

        def subtree(key, seen):
            if key in seen or key not in objs:
                return seen
            seen.add(key)
            for c in objs[key].get("composition", []):
                if c["namespace"] == "":
                    subtree(c["key"], seen)
            return seen
        for c in objs[self.root]["composition"]:
            o = objs[c["key"]]
            if not o["name"].startswith(LIVE_SCREENS):
                continue
            text = json.dumps([objs[k] for k in subtree(c["key"], set())]).lower()
            self.assertNotIn("grader", text, o["name"])
            self.assertNotIn("image-view", text, o["name"])

    def test_commanding_is_off_unless_asked_for(self):
        self.assertIn("Boolean(options.commanding)", (WEB / "doomsat" / "plugin.js").read_text(encoding="utf-8"))
        self.assertIn("commanding = false", (WEB / "doomsat" / "common.js").read_text(encoding="utf-8"))
        self.assertIn("get('commanding') === 'on'", (WEB / "index.js").read_text(encoding="utf-8"))
        self.assertIn("commanding: false", (WEB / "replay.js").read_text(encoding="utf-8"))

    def test_time_strip_events_are_markers(self):
        # without the plugin the events lane of 40 TIME STRIP falls back to a table off the shared clock
        src = (WEB / "doomsat" / "common.js").read_text(encoding="utf-8")
        self.assertIn("EventTimestripPlugin(timeline.extendedLinesBus)", src)

    def test_no_display_sends_commands(self):
        # plugin.js keeps the command button for later; SET_GOAL HOLD stops nothing on board, so no display
        # carries it and the reference does not offer it
        self.assertEqual([o["name"] for o in self.b.objects.values() if o["type"] == "doomsat.command"], [])
        self.assertNotIn("commanding", openmct_docs.render(self.b, self.root, self.params, self.drift))


@unittest.skipIf(set_yamcs_alarms is None, "yamcs-client is not installed (run with ground/.venv/bin/python)")
class AlarmTool(unittest.TestCase):
    """tools/set_yamcs_alarms.py sends what alarm-ranges.json says, as the replay and the indicators read it."""
    RANGES = json.loads((ROOT / "ground" / "yamcs" / "alarm-ranges.json").read_text(encoding="utf-8"))

    @staticmethod
    def allowed(req):
        # Yamcs 5.12 reads the deprecated staticAlarmRange field on this request, not staticAlarmRanges
        out = {}
        for ar in req.defaultAlarm.staticAlarmRange:
            assert not (ar.HasField("minExclusive") or ar.HasField("maxExclusive")), ar
            out[set_yamcs_alarms.mdb_pb2.AlarmLevelType.Name(ar.level).lower()] = [
                ar.minInclusive if ar.HasField("minInclusive") else None,
                ar.maxInclusive if ar.HasField("maxInclusive") else None]
        return out

    def test_bounds_are_inclusive_and_zero_is_a_bound(self):
        # yamcs-client's set_default_alarm_ranges sends both ends exclusive and drops a 0: HEALTH 50 and one
        # enemy would be watch alarms live, and WATCHDOG_TRIPS would never alarm
        self.assertEqual(self.allowed(set_yamcs_alarms.alarm_request({"watch": [50, None], "warning": [None, 0]})),
                         {"watch": [50, None], "warning": [None, 0]})
        self.assertEqual(self.allowed(set_yamcs_alarms.alarm_request({"watch": [0, 10]})), {"watch": [0, 10]})
        for q, r in self.RANGES.items():
            if q.startswith("/") and "enum" not in r:
                self.assertEqual(self.allowed(set_yamcs_alarms.alarm_request(r)), r, q)

    def test_one_override_per_parameter(self):
        sent = []
        proc = types.SimpleNamespace(ctx=types.SimpleNamespace(patch_proto=lambda url, data: sent.append((url, data))))
        client = mock.Mock()
        client.return_value.get_processor.return_value = proc
        with mock.patch.object(set_yamcs_alarms, "YamcsClient", client), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(sys, "argv", ["set_yamcs_alarms.py"]):
            set_yamcs_alarms.main()
        client.return_value.get_processor.assert_called_once_with("fprime-project", "realtime")
        numeric = {q: r for q, r in self.RANGES.items() if q.startswith("/") and "enum" not in r}
        got = {url.split("/parameters", 1)[1]: set_yamcs_alarms.mdb_override_service_pb2.UpdateParameterRequest
               .FromString(data) for url, data in sent}
        self.assertEqual(sorted(got), sorted(numeric))
        self.assertTrue(all(url.startswith("/mdb-overrides/fprime-project/realtime/parameters/") for url, _ in sent))
        health = got[f"{openmct_docs.DOOM}/HEALTH"]
        self.assertEqual(health.action, health.SET_DEFAULT_ALARMS)
        self.assertEqual(self.allowed(health), numeric[f"{openmct_docs.DOOM}/HEALTH"])


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "runs scripts/start_openmct.sh under bash")
class StartScript(unittest.TestCase):
    def served(self, campaign_age_h):
        """start_openmct.sh in a made-up checkout, node and npx stubbed: the displays it would serve, and what it said."""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "scripts").mkdir()
            shutil.copy(ROOT / "scripts" / "start_openmct.sh", t / "scripts")
            web = t / "ground" / "openmct"
            for d in ("doomsat", "displays"):
                (web / d).mkdir(parents=True)
            for f in ("index.html", "index.js"):
                (web / f).write_text("")
            (t / "external" / "openmct-yamcs" / "example").mkdir(parents=True)
            (t / "docs" / "results").mkdir(parents=True)
            (t / "docs" / "results" / "e1m1-progress.html").write_text("")
            stubs = t / "bin"
            stubs.mkdir()
            for tool in ("node", "npx"):
                (stubs / tool).write_text("#!/bin/sh\nexit 0\n")
                (stubs / tool).chmod(0o755)
            now = time.time()
            for name, age_h in (("doomsat-displays.json", 72), ("doomsat-displays.campaign.json", campaign_age_h)):
                (web / "displays" / name).write_text(name)
                os.utime(web / "displays" / name, (now - age_h * 3600,) * 2)
            out = subprocess.run(["bash", str(t / "scripts" / "start_openmct.sh")], capture_output=True, text=True,
                                 env=dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}"), timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            return (t / "external" / "openmct-yamcs" / "example" / "displays" / "doomsat-displays.json").read_text(), out.stdout

    def test_a_campaign_build_is_served_for_a_day(self):
        self.assertEqual(self.served(1)[0], "doomsat-displays.campaign.json")
        shown, said = self.served(48)              # newer than the committed file, but a past campaign's
        self.assertEqual(shown, "doomsat-displays.json")
        self.assertIn("over a day old", said)


class FakeYamcs:
    """Stands in for the parameters:batchSet request, no network. Like Yamcs, it takes a batch whole or not at
    all: a name it lacks or a label it cannot convert refuses the lot, with Yamcs's own wording."""
    def __init__(self, lacks=(), bad_labels=()):
        self.lacks, self.bad_labels = set(lacks), set(bad_labels)
        self.values, self.bodies = {}, []

    def __call__(self, body):
        self.bodies.append(body)
        reqs = body["request"]
        unknown = [r["id"]["name"] for r in reqs if r["id"]["name"].rsplit("/", 1)[1] in self.lacks]
        if unknown:
            return 400, "InvalidIdentification: " + ", ".join(f'name: "{n}"\n' for n in unknown)
        for r in reqs:
            if r["value"].get("stringValue") in self.bad_labels:
                return 400, f"Cannot convert {r['value']['stringValue']} to intent_mode_t; it is not a valid label"
        for r in reqs:
            v = r["value"]
            self.values[r["id"]["name"]] = next(v[k] for k in v if k != "type")
        return 200, ""


def feed(yamcs=None, **kw):
    return ops_telemetry.OpsTelemetry(None, every_s=0, post=yamcs if yamcs is not None else FakeYamcs(), **kw)


def row(mode="EXPLORE", tx=1.0, pick=0, model="jev-1.13.0", **select):
    return {"t": 1.0, "kind": "control", "mode": mode, "pick": pick, "select": {"gap": 0.5, "confidence": 0.7, **select},
            "answers": {"g_t0": "4.00", "g_t1": "2.00"}, "control": {"mode": mode, "target_x": tx, "target_y": 0.0},
            "model": model, "latency_ms": 450, "tel_age_ms": 60, "cmd_ms": 40, "graph_version": 3, "episode": 1}


class GroundFeed(unittest.TestCase):
    def test_decision_sources(self):
        src = ops_telemetry.decision_source
        self.assertEqual(src(row()), "JEV")
        self.assertEqual(src(row(fallback="unsure gap")), "UNSURE_BAND")
        self.assertEqual(src(row(held=True)), "HELD")
        self.assertEqual(src(dict(row(), cached=True)), "CACHED")
        self.assertEqual(src(dict(row(), answers={})), "UNAVAILABLE")
        self.assertEqual(src(row(gave_up=True)), "RULE")
        # decision_reasons' order: the band and a hold before a give-up or the cache
        self.assertEqual(src(dict(row(fallback="unsure gap"), cached=True)), "UNSURE_BAND")
        self.assertEqual(src(row(held=True, gave_up=True)), "HELD")
        self.assertEqual(src(row(fallback="no answers")), "UNAVAILABLE")

    def test_a_rule_answer_is_never_jev(self):
        src = ops_telemetry.decision_source
        # jev timed out: targeting.decide logs the rules' answers under model "code (fallback)"
        self.assertEqual(src(row(model="code (fallback)")), "UNAVAILABLE")
        self.assertEqual(src(dict(row(), unavailable="ReadTimeout: read timed out")), "UNAVAILABLE")
        # --system-one code (scripts/play.sh --autopilot): the rules answer every head, cached or not
        self.assertEqual(src(row(model="code")), "RULE")
        self.assertEqual(src(dict(row(model="code"), cached=True)), "RULE")

    def test_a_jev_outage_and_the_code_autopilot_read_zero_jev_share(self):
        for model, label in (("code (fallback)", "UNAVAILABLE"), ("code", "RULE")):
            yamcs = FakeYamcs()
            ops = feed(yamcs)
            for i in range(6):
                ops.update(dict(row(pick=i % 2, model=model), latency_ms=0), [{"kind": "frontier"}, {"kind": "door"}])
            self.assertEqual(yamcs.values["/DoomGround/DecisionSource"], label, model)
            self.assertEqual(yamcs.values["/DoomGround/JevShare"], 0.0, model)
            self.assertEqual(yamcs.values["/DoomGround/FallbackRate"], 1.0, model)

    def test_jev_share_counts_intent_changes_not_decisions(self):
        yamcs = FakeYamcs()
        ops = feed(yamcs)
        cands = [{"kind": "frontier"}, {"kind": "door"}]
        for _ in range(5):
            ops.update(row(pick=0), cands)                   # one intent: not a change until it changes
        self.assertNotIn("/DoomGround/JevShare", yamcs.values)
        ops.update(row(pick=1), cands)                       # a change, jev's
        ops.update(row(pick=0, fallback="unsure gap"), cands)  # a change, the band's
        self.assertAlmostEqual(yamcs.values["/DoomGround/JevShare"], 0.5)
        self.assertAlmostEqual(yamcs.values["/DoomGround/FallbackRate"], 1 / 7)
        self.assertEqual(yamcs.values["/DoomGround/PickKind"], "FRONTIER")
        self.assertEqual(yamcs.values["/DoomGround/DecisionAgeMs"], 550.0)

    def test_jev_share_is_the_charter_metric_less_rule_answers(self):
        sys.path.insert(0, str(ROOT / "research"))
        import frozen_metrics as fm
        flags = [{}, {"held": True}, {"fallback": "unsure gap"}, {"gave_up": True}, {}, {}]
        rows = [dict(row(mode=("EXPLORE", "FIGHT")[i % 7 == 3], pick=(i * 5) % 3, **flags[i % 6]), cached=i % 4 == 1)
                for i in range(60)]
        whole = ops_telemetry.Rolling(window=1000)
        for r in rows:
            whole.add(r)
        self.assertAlmostEqual(whole.jev_share(), fm.jev_share(rows))
        rows[10] = dict(rows[10], model="code (fallback)")   # a change jev did not make: charter 7 still credits it
        whole = ops_telemetry.Rolling(window=1000)
        for r in rows:
            whole.add(r)
        self.assertLess(whole.jev_share(), fm.jev_share(rows))

    def test_the_sector_pilot_word_is_not_a_slot(self):
        yamcs = FakeYamcs()
        feed(yamcs).update(row(pick="ahead"), [])
        self.assertEqual(yamcs.values["/DoomGround/PickSlot"], 255)
        self.assertEqual(yamcs.values["/DoomGround/PickKind"], "NONE")

    def test_everything_published_is_in_the_ground_xtce(self):
        yamcs = FakeYamcs()
        ops = feed(yamcs)
        ops.update(row(pick=1), [])
        ops.update(dict(row(), answers={"g_t0": "1", "engage": "x"}), [{"kind": "exit"}])
        params, _ = bod.doomdict.load()
        self.assertIn("/DoomGround/JevShare", yamcs.values)
        self.assertEqual(sorted(n for n in yamcs.values if n not in params), [])

    def test_one_request_per_publish(self):
        yamcs = FakeYamcs()
        ops = ops_telemetry.OpsTelemetry(None, post=yamcs)          # every_s = 1: once a second at most
        ops.update(dict(row(), answers={"g_t0": "1", "engage": "x"}), [{"kind": "exit"}])
        ops.update(row(pick=1), [])
        self.assertEqual(len(yamcs.bodies), 1)
        reqs = yamcs.bodies[0]["request"]
        self.assertGreaterEqual(len(reqs), 19)
        self.assertTrue(all(r["expiresIn"] == 3000 for r in reqs))

    def test_an_older_ground_database_is_reported_once(self):
        block = ("DecisionAgeMs", "IntentMode", "DecisionSource", "PickSlot", "PickKind", "PickGap", "PickConfidence",
                 "JevShare", "FallbackRate", "GraphVersion", "Attempt", "EngageAnswer") + tuple(f"Score{n}" for n in range(8))
        for lacks in (block, ("EngageAnswer",)):           # main's ground database; one name short
            yamcs, err = FakeYamcs(lacks=lacks), io.StringIO()
            ops = feed(yamcs)
            with contextlib.redirect_stderr(err):
                for i in range(12):
                    ops.update(dict(row(pick=i), answers={"g_t0": "1", "engage": "x"}), [])
                    if i == 1:                   # JevShare first comes up at the first intent change
                        settled = len(yamcs.bodies)
            said = err.getvalue()
            self.assertTrue(all(said.count(n) == 1 for n in lacks), said)
            self.assertIn("restart Yamcs", said)
            self.assertLessEqual(len(said.splitlines()), 2, said)
            late = yamcs.bodies[settled:]               # then one batch a publish, without what it lacks
            self.assertEqual(len(late), 0 if lacks == block else 10, lacks)
            self.assertTrue(all(len(b["request"]) == 19 for b in late))
        self.assertEqual(yamcs.values["/DoomGround/DecisionSource"], "JEV")

    def test_a_label_yamcs_cannot_take_is_left_out_while_it_lasts(self):
        yamcs, err = FakeYamcs(bad_labels={"DONE"}), io.StringIO()   # --control legacy at a level change
        ops = feed(yamcs)
        with contextlib.redirect_stderr(err):
            ops.update(row(mode="DONE"), [])
            self.assertEqual(yamcs.values["/DoomGround/DecisionSource"], "JEV")
            self.assertNotIn("/DoomGround/IntentMode", yamcs.values)
            n = len(yamcs.bodies)
            ops.update(row(mode="DONE"), [])
            self.assertEqual(len(yamcs.bodies), n + 1)
            ops.update(row(mode="EXPLORE"), [])
        self.assertEqual(yamcs.values["/DoomGround/IntentMode"], "EXPLORE")
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())

    def test_no_answer_is_reported_once_and_backed_off(self):
        calls = []

        def down(body):
            calls.append(body)
            raise TimeoutError("read timed out (0.5 s)")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ops = feed(down)                                         # retry_s = 10
            for _ in range(5):
                ops.update(row(), [])
            self.assertEqual(len(calls), 1)
            ops = feed(down, retry_s=0)
            for _ in range(5):
                ops.update(row(), [])
            self.assertEqual(len(calls), 6)
        self.assertEqual(len(err.getvalue().splitlines()), 2, err.getvalue())   # once per feed

    def test_a_restarted_yamcs_is_asked_again_for_what_the_last_one_lacked(self):
        old, new, err = FakeYamcs(lacks=("EngageAnswer",)), FakeYamcs(), io.StringIO()
        ops = feed(old, retry_s=0)
        engage = dict(row(), answers={"g_t0": "1", "engage": "x"})

        def down(body):
            raise ConnectionError("connection refused")
        with contextlib.redirect_stderr(err):
            ops.update(engage, [])
            ops.post = down                                # Yamcs restarting, with the current ground database
            ops.update(engage, [])
            ops.post = new
            ops.update(engage, [])
        self.assertNotIn("/DoomGround/EngageAnswer", old.values)
        self.assertEqual(new.values["/DoomGround/EngageAnswer"], "x")
        self.assertEqual(len(new.bodies), 1)
        self.assertEqual(err.getvalue().count("EngageAnswer"), 1, err.getvalue())

    def test_the_feed_reads_and_never_raises(self):
        r, cands = row(pick=1), [{"kind": "frontier"}, {"kind": "door"}]
        before = copy.deepcopy((r, cands))
        feed().update(r, cands)
        self.assertEqual((r, cands), before)
        with contextlib.redirect_stderr(io.StringIO()):
            feed().update(dict(row(), answers={"g_t0": "high"}), None)     # malformed: skipped, not raised

    def test_the_request_is_a_batch_set_with_a_timeout(self):
        sent = []

        class Response:
            def __init__(self, status, body):
                self.status_code, self.body, self.text = status, body, json.dumps(body)

            def json(self):
                return self.body

        class Session:
            def post(self, url, json=None, timeout=None):
                sent.append((url, json, timeout))
                return Response(200, {}) if len(sent) == 1 else Response(400, {"msg": "Cannot convert DONE"})

        class Processor:     # what yamcs-client 2.1.0's ProcessorClient carries
            ctx = types.SimpleNamespace(api_root="http://localhost:8090/api", session=Session(), credentials=None,
                                        auth_root="http://localhost:8090/auth")
            _instance, _processor = "fprime-project", "realtime"
        post = ops_telemetry.batch_set(Processor(), 0.5)
        body = {"request": [{"id": {"name": "/DoomGround/PickGap"}, "value": ops_telemetry.json_value(0.5)}]}
        self.assertEqual(post(body), (200, ""))
        self.assertEqual(sent[0], ("http://localhost:8090/api/processors/fprime-project/realtime/parameters:batchSet",
                                   body, 0.5))
        self.assertEqual(post(body), (400, "Cannot convert DONE"))
        self.assertEqual([ops_telemetry.json_value(v) for v in (True, 7, 2.5, "JEV")],
                         [{"type": "BOOLEAN", "booleanValue": True}, {"type": "SINT32", "sint32Value": 7},
                          {"type": "DOUBLE", "doubleValue": 2.5}, {"type": "STRING", "stringValue": "JEV"}])


VIEW_HARNESS = r"""
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
const settle = (n = 60) => new Promise((r) => setTimeout(r, n));
const X = `${DOOM}/POS_X`; const Y = `${DOOM}/POS_Y`; const A = `${DOOM}/ANGLE`;
// Open MCT as the views see it: a conductor the test moves, and a telemetry source that stamps its data with ISO
// strings, as openmct-yamcs does. slow: an answer for bounds starting there comes late.
function fake(data, mode, bounds, slow = {}) {
  const on = {}; const subs = new Map();
  const iso = (t) => new Date(t).toISOString();
  const time = {
    mode, bounds,
    on: (e, f) => { (on[e] = on[e] || []).push(f); },
    off: (e, f) => { on[e] = (on[e] || []).filter((g) => g !== f); },
    getBounds: () => ({ ...time.bounds }),
    isRealTime: () => time.mode === 'realtime',
    setBounds(b, tick = false) { time.bounds = b; (on.boundsChanged || []).forEach((f) => f({ ...b }, tick)); },
    setMode(m) { time.mode = m; (on.modeChanged || []).forEach((f) => f(m)); }   // the mode menu: no bounds
  };
  return {
    time,
    subscribers: () => [...subs.values()].reduce((n, s) => n + s.length, 0),
    live(q, t, value) { (subs.get(q) || []).forEach((cb) => cb({ timestamp: iso(t), value })); },
    objects: { get: async (i) => ({ identifier: i, q: i.key.replace(/~/g, '/') }) },
    telemetry: {
      isTelemetryObject: () => true,
      subscribe(o, cb) {
        subs.set(o.q, [...(subs.get(o.q) || []), cb]);
        return () => subs.set(o.q, subs.get(o.q).filter((c) => c !== cb));
      },
      async request(o, { start, end, strategy }) {
        if (slow[start]) await settle(slow[start]);
        const r = (data[o.q] || []).filter(([t]) => t >= start && t <= end).map(([t, value]) => ({ timestamp: iso(t), value }));
        return strategy === 'latest' ? r.slice(-1) : r;
      }
    }
  };
}
let seen;
const watch = (openmct) => new Latest(openmct, [A, X, Y], (v, s) => { seen = JSON.parse(JSON.stringify({ v, s })); },
  { history: [X, Y] });
const report = (o) => console.log(JSON.stringify(o));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class CustomViews(unittest.TestCase):
    """plugin.js as it is, under node, against a fake Open MCT whose time conductor the test moves."""

    def run_view(self, scenario):
        src = (WEB / "doomsat" / "plugin.js").read_text(encoding="utf-8")
        out = subprocess.run(["node", "--input-type=module", "-e", src + VIEW_HARNESS + scenario],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_the_traverse_pairs_x_and_y_by_time(self):
        # the replay drops a value that repeats, so POS_X and POS_Y need not have the same number of samples
        html = self.run_view("report(renderCandidates({}, {[X]: [[0, 0], [20, 100]], [Y]: [[0, 0], [10, 50], [20, 60]]}));")
        pts = [tuple(map(float, p.split(","))) for p in re.search(r'<polyline points="([^"]*)"', html).group(1).split()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        plan = [(round((x - min(xs)) / (max(xs) - min(xs)), 3), round((max(ys) - y) / (max(ys) - min(ys)), 3))
                for x, y in pts]
        self.assertEqual(plan, [(0, 0), (0, round(50 / 60, 3)), (1, 1)])   # x held at 0 while y went to 50

    def test_a_fixed_review_is_not_overwritten_by_live_data(self):
        got = self.run_view("""
const m = fake({[A]: [[1500, 90]], [X]: [[1500, 5]], [Y]: [[1500, 7]]}, 'fixed', {start: 1000, end: 2000});
watch(m); await settle();
const fixed = m.subscribers();
m.live(A, 5000, 270); m.live(X, 5000, 99); await settle();
const after = seen;
m.time.setMode('realtime'); await settle();
const realtime = m.subscribers();
m.time.setMode('fixed'); await settle();
report({fixed, after, realtime, back: m.subscribers()});
""")
        self.assertEqual(got["fixed"], 0, "Fixed mode must not subscribe")
        self.assertEqual(got["after"]["v"][f"{openmct_docs.DOOM}/ANGLE"], 90)
        self.assertEqual([v for _, v in got["after"]["s"][f"{openmct_docs.DOOM}/POS_X"]], [5])
        self.assertGreater(got["realtime"], 0)
        self.assertEqual(got["back"], 0)

    def test_real_time_keeps_the_traverse_to_the_window(self):
        got = self.run_view("""
const m = fake({[X]: [[1000, 1], [2000, 2]], [Y]: [[1000, 1], [2000, 2]]}, 'realtime', {start: 0, end: 10000});
watch(m); await settle();
m.time.setBounds({start: 1500, end: 11500}, true); m.live(X, 11000, 3); m.live(Y, 11000, 4); await settle();
const first = seen.s;
m.time.setBounds({start: 5000, end: 15000}, true); await settle();
report({first, then: seen.s});
""")
        X, Y = f"{openmct_docs.DOOM}/POS_X", f"{openmct_docs.DOOM}/POS_Y"
        self.assertEqual(got["first"][X], [[2000, 2], [11000, 3]])    # ms, whatever the source's time format
        self.assertEqual(got["first"][Y], [[2000, 2], [11000, 4]])
        self.assertEqual(got["then"][X], [[11000, 3]])

    def test_a_bounds_change_clears_and_asks_again(self):
        # the answer for 3000 to 4000 is slow and has data; it comes in after the answer for 1400 to 1600
        got = self.run_view("""
const m = fake({[A]: [[1500, 90], [3500, 80]], [X]: [[1500, 5], [3500, 6]], [Y]: [[3500, 8]]}, 'fixed',
               {start: 1000, end: 2000}, {3000: 80});
const view = watch(m); await settle();
const before = seen;
m.time.setBounds({start: 3000, end: 4000}); m.time.setBounds({start: 1400, end: 1600}); await settle(300);
const back = seen;
const held = JSON.parse(JSON.stringify({ v: view.values, s: view.series }));
m.time.setBounds({start: 6000, end: 7000}); await settle(300);
report({before, back, held, empty: seen});
""")
        A, X, Y = (f"{openmct_docs.DOOM}/{n}" for n in ("ANGLE", "POS_X", "POS_Y"))
        self.assertEqual(got["before"]["v"][A], 90)
        self.assertEqual(got["back"]["v"], {A: 90, X: 5}, "a slow answer for older bounds came in last and won")
        self.assertEqual(got["back"]["s"], {X: [[1500, 5]], Y: []})
        self.assertEqual(got["held"], got["back"], "the slow answer for older bounds changed what the view holds")
        self.assertEqual(got["empty"]["v"], {})
        self.assertEqual(got["empty"]["s"].get(X, []), [])


class Replay(unittest.TestCase):
    def build(self, flight, *args):
        """Three made-up rows as <flight>/decisions.jsonl, then the builder on --log alone."""
        flight.mkdir(parents=True, exist_ok=True)
        rows = []
        for i, (hp, mode) in enumerate([(100, "EXPLORE"), (40, "FIGHT"), (20, "FIGHT")]):
            r = row(mode=mode, tx=float(i), model="code (fallback)" if i == 2 else "jev-1.13.0")
            r.update(t=1790000000 + i, tic=400 + 35 * i, kills=i, goal="EXPLORE", candidates=1,
                     cand_xy=[{"kind": "frontier", "x": 1.0, "y": 2.0}],
                     raw={"HEALTH": hp, "STUCK": False, "POS_X": float(i), "POS_Y": 0.0, "WEAPON": "PISTOL"})
            rows.append(json.dumps(r))
        (flight / "decisions.jsonl").write_text("\n".join(rows))
        out = flight.parent / "pack"
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_openmct_replay.py"),
                        "--log", str(flight / "decisions.jsonl"), "--out", str(out), "--no-displays", *args],
                       check=True, capture_output=True)
        return out, json.loads((out / "pack.json").read_text())

    def test_a_pack_from_three_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, pack = self.build(Path(tmp) / "flight-x", "--name", "t")
            grader = (out / "grader").exists()
        meta = pack["meta"]
        self.assertEqual(meta["rows"], 3)
        self.assertFalse(set(meta["provenance"]["recorded"]) & set(meta["provenance"]["derived"]))
        self.assertIn("/DoomSat_DoomSat/DoomSat/doom/HEALTH", meta["provenance"]["recorded"])
        self.assertIn("/DoomGround/JevShare", meta["provenance"]["derived"])
        self.assertTrue(all(e[3].startswith("[derived]") for e in pack["events"]))
        self.assertTrue(any("HEALTH fell through 25" in e[3] for e in pack["events"]))
        self.assertEqual(len(pack["commands"]), 3)
        # the live feed's rules: jev made the one intent change, then jev was unreachable
        self.assertEqual([v for _, v in pack["series"]["/DoomGround/DecisionSource"]], ["JEV", "UNAVAILABLE"])
        self.assertEqual([v for _, v in pack["series"]["/DoomGround/JevShare"]], [1.0])
        # a log with no frames beside it gets none: not research/out/flight-32's camera, map or grader wall
        self.assertEqual((meta["name"], meta["frames"]), ("t", 0))
        self.assertEqual(pack["images"], {"/DoomGround/DoomFrame": []})
        self.assertFalse(grader)

    def test_a_log_brings_its_own_frames_map_and_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            flight = Path(tmp) / "flight-x"
            (flight / "frames").mkdir(parents=True)
            for n in (10, 11, 12):
                (flight / "frames" / f"frame-{n:06d}.jpg").write_bytes(b"frame %d" % n)
            (flight / "frames" / "latest_map.png").write_bytes(b"map")
            (flight / "overlay-E1M1-0.png").write_bytes(b"overlay")
            out, pack = self.build(flight, "--frame-stride", "1")
            frames = [(out / f).read_bytes() for _, f in pack["images"]["/DoomGround/DoomFrame"]]
            copied = (out / "map.png").read_bytes(), (out / "grader" / "overlay-E1M1-0.png").read_bytes()
        self.assertEqual((pack["meta"]["name"], pack["meta"]["frames"]), ("flight-x", 3))
        self.assertEqual(frames, [b"frame 10", b"frame 11", b"frame 12"])
        self.assertEqual(copied, (b"map", b"overlay"))
        self.assertIn("/DoomGround/DoomMap", pack["images"])

if __name__ == "__main__":
    unittest.main()
