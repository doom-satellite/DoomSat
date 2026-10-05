"""Flying without jev: `--system-one code` (the rules, no model) and `--system-one manual` (a person at the
dashboard). Both exist so the stack can be shown to someone with no TypeSafe key.

The code System One must stay the bench's code baseline, answer for answer, or a code flight on the full
stack stops being the same experiment as `research/runner.py --decider code`. The manual mode must decide
nothing and issue nothing: the person's keys are the only uplink. And the dashboard's keys must name the
CONTROL command's real arguments, or every keypress fails in Yamcs and nobody finds out until a friend
tries it.
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "research"))
sys.path.insert(0, os.path.join(ROOT, "ground"))

import runner                                   # noqa: E402  (this sets DOOMSAT_ROLE=pilot)
import decision_graph as dg                     # noqa: E402
import graph_config as gc                       # noqa: E402
import targeting as tg                          # noqa: E402
from providers import CodeSystemOne, make_system_one   # noqa: E402

try:
    import pilot                                # needs yamcs-client: run the suite with ground/.venv
except ImportError:                             # pragma: no cover
    pilot = None

RULES = {"monsters": {"Zombieman": {"danger": 2}, "DoomImp": {"danger": 3}},
         "behaviour": {"health_critical": 30, "health_low": 50, "health_comfortable": 80,
                       "armor_low": 25, "ammo_low": {"shells": 6, "bullets": 20}}}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def cand(kind, path, novelty=200, tries=0, colour="", threat=255, count=0, bearing=0.0):
    return {"kind": kind, "x": 100.0 + path, "y": 200.0, "bearing": bearing, "path_units": path,
            "novelty": novelty, "colour": colour, "tries": tries, "threat_class": threat, "threat_count": count}


class TestTheCodePilotIsTheBenchBaseline(unittest.TestCase):
    def setUp(self):
        self.cfg = gc.load()
        self.ours, self.bench = CodeSystemOne(self.cfg), runner.CodeDecider(self.cfg)

    def same(self, state, qs):
        a, b = self.ours.ask(state, qs), self.bench.ask(state, qs)
        self.assertEqual(a["answers"], b["answers"])
        # engage and weapon go unanswered by both, on purpose: targeting.decide has a backstop for each
        self.assertLessEqual(set(a["answers"]), set(qs))
        self.assertTrue(a["answers"])
        self.assertEqual((a["model"], a["latency_ms"]), ("code", 0))

    def test_same_answers_on_the_targeting_heads(self):
        cases = [
            ({"HEALTH": 100, "ARMOR": 0, "SHELLS": 8, "BULLETS": 50, "KEYS": 0},
             [cand("frontier", 300.0), cand("door", 800.0), cand("frontier", 1500.0, novelty=40)]),
            ({"HEALTH": 22, "ARMOR": 0, "SHELLS": 0, "BULLETS": 4, "KEYS": 1, "ENEMY_COUNT": 2, "ENEMY_DIST": 150},
             [cand("item", 200.0), cand("door", 400.0, colour="red"), cand("exit", 900.0),
              cand("enemy", 150.0, threat=1, count=2)]),
            ({"HEALTH": 70, "ARMOR": 40, "SHELLS": 20, "BULLETS": 90, "KEYS": 0},
             [cand("key", 600.0, colour="blue"), cand("switch", 250.0, tries=2)]),
        ]
        for t, cands in cases:
            for ask_need in (False, True):
                state = tg.build_state(t, cands, tg.needs_from(t, RULES), [], rules=RULES)
                qs = tg.questions(state, self.cfg, ask_need=ask_need)
                self.assertTrue(qs)
                self.same(dg.state_for(state, qs), qs)

    def test_same_answers_on_the_sector_heads(self):
        open_ground = {"space": "long", "ground": "never explored", "door": "none", "exit_here": "no",
                       "key_here": "no", "item_here": "none", "hint_here": "no"}
        dead_end = dict(open_ground, space="blocked", ground="walked before")
        state = {"sectors": {"ahead": open_ground, "left": dead_end},
                 "player": {"health": "low"}, "combat": {"enemy_distance": "close"}}
        self.same(state, {"s_ahead": {}, "s_left": {}, "danger": {}, "goal": {}})

    def test_it_needs_no_key(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            one = make_system_one("code", mock.Mock(env_files=[]), self.cfg)
        self.assertEqual(one.name, "code")


@unittest.skipIf(pilot is None, "yamcs-client is not installed here; run with ground/.venv")
class TestThePilotModes(unittest.TestCase):
    def parse(self, *argv):
        seen = {}

        class Stub:                              # stands in for the Pilot, which would connect to Yamcs
            def __init__(self, args):
                seen["args"] = args

            def run(self):
                pass

        with tempfile.TemporaryDirectory() as out, mock.patch.object(pilot, "Pilot", Stub), \
                mock.patch.object(sys, "argv", ["pilot.py", "--out-dir", out, *argv]):
            pilot.main()
        return seen["args"]

    def test_manual_turns_system_two_off_and_the_picture_up(self):
        a = self.parse("--system-one", "manual")
        self.assertFalse(a.after_action, "there is nothing for System Two to review when a person drives")
        self.assertEqual(a.publish_hz, 10.0)
        self.assertTrue(a.reset)

    def test_the_model_pilots_keep_their_old_defaults(self):
        for one in ("typesafe", "code"):
            a = self.parse("--system-one", one)
            self.assertTrue(a.after_action)
            self.assertEqual(a.publish_hz, 2.0, "the jev flights publish twice a second, as before")

    def manual_pilot(self):
        p = pilot.Pilot.__new__(pilot.Pilot)
        p.manual, p.pilot_name, p.announce_ok = True, "manual", True
        p.telemetry = {"CMDS_RECEIVED": 7, "HEALTH": 100, "LEVEL": 1}
        p.telemetry_time, p.last_stats, p.last_print, p.episode = time.time(), 0.0, time.time(), 1
        p.frames = mock.Mock(complete=0, incomplete=0)
        p.processor, p.pub_processor, p.log = mock.Mock(), mock.Mock(), io.StringIO()
        return p

    def test_the_manual_step_decides_nothing_and_issues_nothing(self):
        p = self.manual_pilot()
        p.manual_step()
        p.processor.issue_command.assert_not_called()
        written = {c.args[0].rsplit("/", 1)[-1]: c.args[1] for c in p.pub_processor.set_parameter_value.call_args_list}
        self.assertEqual(written["PilotMode"], "manual")
        self.assertIn("uplinks=7", written["Controls"])
        row = json.loads(p.log.getvalue().splitlines()[-1])
        self.assertEqual(row["kind"], "manual")
        self.assertEqual(row["raw"]["HEALTH"], 100)

    def test_a_yamcs_without_pilotmode_is_told_once(self):
        p = self.manual_pilot()
        p.pub_processor.set_parameter_value.side_effect = RuntimeError("no such parameter")
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            p.announce()
            p.announce()
        self.assertFalse(p.announce_ok)
        self.assertEqual(err.getvalue().count("PilotMode"), 1)

    # The level budget. It once lived behind the System Two check, so `--system-two none` (play.sh --autopilot,
    # night_flight.sh) never reset: one code-autopilot attempt ran for hours.
    def budget_pilot(self, budget, elapsed, system_two=None, manual=False):
        p = pilot.Pilot.__new__(pilot.Pilot)
        p.args = mock.Mock(level_budget=budget, bump_every=60.0)
        p.manual, p.system_two = manual, system_two
        p.telemetry = {"EXPLORED_CELLS": 40, "LEVEL": 1}
        p.progress, p.level, p.attempt, p.episode_outcome, p.bump_busy = [], 1, 1, None, False
        p.level_start_t = time.time() - elapsed
        p.last_hint_t = time.time() - 600.0          # a bump is long overdue
        p.processor = mock.Mock()
        return p

    def check_stall(self, p):
        with mock.patch("pilot.threading.Thread") as thread, mock.patch.object(sys, "stdout", io.StringIO()):
            p.check_stall()
        sent = [c.args[0].rsplit("/", 1)[-1] for c in p.processor.issue_command.call_args_list]
        return sent, thread

    def test_the_level_budget_holds_without_system_two(self):
        p = self.budget_pilot(180.0, 200.0)
        sent, thread = self.check_stall(p)
        self.assertEqual(sent, ["RESET_GAME"], "a code flight past its budget is reset, System Two or not")
        self.assertEqual(p.attempt, 2)
        self.assertIn("within the 180 s budget", p.episode_outcome)
        self.assertLess(time.time() - p.level_start_t, 5.0, "the next attempt starts a fresh budget")
        thread.assert_not_called()

    def test_the_level_budget_with_system_two_is_unchanged(self):
        p = self.budget_pilot(180.0, 200.0, system_two=mock.Mock())
        sent, thread = self.check_stall(p)
        self.assertEqual(sent, ["RESET_GAME"])
        self.assertEqual(p.attempt, 2)
        self.assertIn("within the 180 s budget", p.episode_outcome)
        thread.assert_not_called()                   # the reset is this check's whole turn: no bump with it

    def test_inside_the_budget_only_system_two_bumps(self):
        for two in (None, mock.Mock()):
            p = self.budget_pilot(180.0, 30.0, system_two=two)
            sent, thread = self.check_stall(p)
            self.assertEqual(sent, [])
            self.assertEqual((p.attempt, p.episode_outcome), (1, None))
            self.assertEqual(thread.call_count, 0 if two is None else 1)
            self.assertEqual(thread.return_value.start.call_count, 0 if two is None else 1)   # and it runs

    def test_no_budget_and_a_person_driving_never_reset(self):
        for p in (self.budget_pilot(0.0, 3600.0),                 # --level-budget 0 keeps the old open-ended flight
                  self.budget_pilot(180.0, 3600.0, manual=True)):  # a person's drive is theirs to end
            sent, thread = self.check_stall(p)
            self.assertEqual(sent, [])
            self.assertEqual((p.attempt, p.episode_outcome), (1, None))
            thread.assert_not_called()


class TestTheDashboardSpeaksTheCommandsLanguage(unittest.TestCase):
    def test_the_drive_keys_fill_exactly_the_control_arguments(self):
        fpp = read("flight/Components/Doom/Doom.fpp")
        block = re.search(r"async command CONTROL\((.*?)\)\s*opcode", fpp, re.S).group(1)
        wanted = {m.lstrip("$") for m in re.findall(r"^\s*(\$?\w+)\s*:", block, re.M)}
        page = read("ground/dashboard/index.html")
        body = re.search(r"function controlArgs\(\)\s*\{(.*?)\n\}", page, re.S).group(1)
        sent = set(re.findall(r"\b(\w+):", body.split("return", 1)[1]))
        self.assertEqual(sent, wanted)

    def test_weapon_names_are_the_enum_labels(self):
        fpp = read("flight/Components/Doom/Doom.fpp")
        labels = set(re.findall(r"^\s*(\w+) = \d", re.search(r"enum Weapon : U8 \{(.*?)\}", fpp, re.S).group(1), re.M))
        page = read("ground/dashboard/index.html")
        used = set(re.findall(r'"(\w+)"', re.search(r"const WEAPON_KEYS = \{(.*?)\}", page).group(1))) | {"FIST"}
        self.assertLessEqual(used, labels)

    def test_every_ground_parameter_the_pilot_writes_is_in_the_database(self):
        xtce = read("ground/yamcs/mdb/doom-ground.xtce.xml")
        defined = set(re.findall(r'<Parameter name="(\w+)"', xtce))
        src = read("ground/pilot.py")
        written = set(re.findall(r'set_ground\(\{\s*"(\w+)"', src))
        written |= set(re.findall(r'\{GROUND\}/(\w+)"', src))
        for d in re.findall(r"set_ground\((\{.*?\})\)", src, re.S):
            written |= set(re.findall(r'"(\w+)":', d))
        self.assertIn("PilotMode", written)
        self.assertLessEqual(written, defined, "the pilot writes a ground parameter Yamcs does not define")


if __name__ == "__main__":
    unittest.main()
