"""The payload's LOAD_WAD handling (request_wad, poll_wad) when the same LOAD_WAD comes twice: one switch, two answers.

On a lossy link the ground sends LOAD_WAD again when no answer comes (tools/wad_uplink_demo.py --tries, the
dashboard), and the answer may have been all that was lost. These tests run the payload's own methods with the game
and the probe child stood in for, so nothing is started and nothing is read. In the ground venv, which has neither
ViZDoom nor Pillow, the payload module is imported with empty stand-ins for those two (nothing here calls them), and
the stand-ins are taken out of sys.modules again so no other test sees them. The payload venv imports the real ones:

    ~/doom/payload-venv/bin/python -m unittest tests.test_payload_load_wad
"""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "payload"))


def _import_payload():
    try:
        import doom_payload  # noqa: E402  the payload venv: ViZDoom, numpy, Pillow
        return doom_payload
    except ImportError:
        pass
    stand_ins = [n for n in ("vizdoom", "PIL", "PIL.Image", "PIL.ImageDraw") if n not in sys.modules]
    sys.modules.update({n: mock.MagicMock() for n in stand_ins})
    try:
        import doom_payload  # noqa: E402
        return doom_payload
    except ImportError:  # pragma: no cover  (numpy missing too)
        return None
    finally:
        for n in stand_ins + ["doom_payload"]:
            sys.modules.pop(n, None)


dp = _import_payload()

if dp is not None:
    wu = dp.wu


@unittest.skipIf(dp is None, "needs numpy (both venvs have it)")
class TestALoadWadSentAgain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"DOOMSAT_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(wu.uplink_dir())
        self.iwad = os.path.join(wu.wads_dir(), "freedoom2.wad")
        self.level = os.path.join(wu.uplink_dir(), "basic.wad")
        for path in (self.iwad, self.level):
            self.write(path)
        # The payload as launched on freedoom2.wad MAP01, without its game or its link
        p = object.__new__(dp.Payload)
        p.wad, p.pwad, p.map = self.iwad, None, "MAP01"
        p.wad_loads, p.wad_job, p.outbox = 0, None, []
        p.wad_serial, p.wad_pinned = 0, None
        p.wad_files = wu.files_key(p.wad, p.pwad)   # as __init__ sets it
        p.oracle, p.geometry = "off", "off"
        p.args = types.SimpleNamespace(skill=3, seed=1)
        p.switch_wad = mock.Mock(side_effect=self.switch)   # stands for rebuilding the game on the new files
        self.p, self.children = p, []
        for target, kw in ((dp.subprocess, dict(Popen=mock.Mock(side_effect=self.child))),
                           (dp.os, dict(killpg=mock.Mock()))):
            patch = mock.patch.multiple(target, **kw)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        quiet = contextlib.redirect_stdout(io.StringIO())   # the payload says what it does on stdout
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    @staticmethod
    def write(path, fill=b"\0"):
        with open(path, "wb") as f:
            f.write(fill * 64)

    def child(self, cmd, **_kw):
        """The probe: it flew a second on the file and exited 0."""
        self.children.append(cmd)
        return mock.Mock(pid=4321, poll=mock.Mock(return_value=0), wait=mock.Mock(return_value=0))

    def switch(self, ipath, ppath, map_name):
        self.p.wad, self.p.pwad, self.p.map = ipath, ppath, map_name
        return None

    def load(self, iwad, pwad, map_name):
        self.p.request_wad(wu.encode_load_wad(iwad, pwad, map_name))

    def answers(self):
        said = [wu.decode_wad_report(body) for body in self.p.outbox]
        self.p.outbox.clear()
        return [(r["result"], r["loads"], r["name"], r["map"], r["reason"]) for r in said]

    def test_a_repeat_after_the_switch_changes_nothing(self):
        self.load("freedoom2.wad", "basic.wad", "map01")
        self.assertTrue(self.p.poll_wad())
        self.assertEqual(self.answers(), [(wu.LOADED, 1, "basic.wad over freedoom2.wad", "MAP01", "")])
        for _ in range(2):   # the answer was lost, so the ground asks again
            self.load("freedoom2.wad", "basic.wad", "MAP01")
            self.assertIsNone(self.p.wad_job, "no second proof")
            self.assertEqual(self.answers(), [(wu.ALREADY, 1, "basic.wad over freedoom2.wad", "MAP01", "")])
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.p.switch_wad.call_count, 1)
        self.assertEqual(self.p.wad_loads, 1)

    def test_a_repeat_while_it_is_being_proven_waits_for_that_answer(self):
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertEqual(self.answers(), [], "the proof is still running: no answer yet, and no refusal")
        self.assertEqual(len(self.children), 1)
        self.assertTrue(self.p.poll_wad())
        self.assertEqual(self.answers(), [(wu.LOADED, 1, "basic.wad over freedoom2.wad", "MAP01", "")])

    def test_another_load_while_one_is_being_proven_is_refused(self):
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.load("freedoom2.wad", "basic.wad", "MAP02")
        self.load("freedoom2.wad", "", "MAP01")
        self.assertEqual([a[0] for a in self.answers()], [wu.FAILED, wu.FAILED])
        self.assertEqual(len(self.children), 1)

    def test_a_new_uplink_of_the_same_name_is_loaded(self):
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertTrue(self.p.poll_wad())
        newer = self.level + ".123.part"
        self.write(newer, b"\1")
        os.replace(newer, self.level)   # the Doom component commits a new upload of basic.wad
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertIsNotNone(self.p.wad_job, "a new file: proven and switched to")
        self.assertTrue(self.p.poll_wad())
        self.assertEqual([a[:2] for a in self.answers()], [(wu.LOADED, 1), (wu.LOADED, 2)])

    def test_a_new_uplink_of_the_same_name_is_loaded_when_no_pin_could_be_made(self):
        # On another file system the payload flies the uplinked path itself, which a new upload then takes over: what
        # flies is still the file it loaded, not whatever the name holds now
        with mock.patch.object(wu, "pin", return_value=None):
            self.load("freedoom2.wad", "basic.wad", "MAP01")
            self.assertTrue(self.p.poll_wad())
            self.assertEqual(self.p.pwad, self.level, "no pin: the game flies the uplinked path")
            newer = self.level + ".123.part"
            self.write(newer, b"\1")
            os.replace(newer, self.level)
            self.load("freedoom2.wad", "basic.wad", "MAP01")
            self.assertIsNotNone(self.p.wad_job, "a new file under the same name: proven and switched to")
            self.assertTrue(self.p.poll_wad())
        self.assertEqual([a[:2] for a in self.answers()], [(wu.LOADED, 1), (wu.LOADED, 2)])

    def test_a_game_launched_under_a_linked_name_is_already_flying(self):
        alias = os.path.join(wu.wads_dir(), "alias.wad")
        os.symlink(self.iwad, alias)
        self.p.wad = alias
        self.p.wad_files = wu.files_key(self.p.wad, self.p.pwad)
        self.load("alias.wad", "", "MAP01")
        self.load("freedoom2.wad", "", "MAP01")
        self.assertEqual([a[0] for a in self.answers()], [wu.ALREADY, wu.ALREADY])
        self.assertEqual(self.children, [])

    def test_the_game_as_launched_is_already_flying(self):
        self.load("freedoom2.wad", "", "map01")
        self.assertEqual(self.answers(), [(wu.ALREADY, 0, "freedoom2.wad", "MAP01", "")])
        self.assertEqual(self.children, [])

    def test_another_map_or_a_level_moved_on_is_a_switch(self):
        self.load("freedoom2.wad", "", "MAP02")
        self.assertTrue(self.p.poll_wad())
        self.p.map = "MAP03"   # the level was finished since: MAP02 is no longer what flies
        self.load("freedoom2.wad", "", "MAP02")
        self.assertTrue(self.p.poll_wad())
        self.assertEqual([a[:2] for a in self.answers()], [(wu.LOADED, 1), (wu.LOADED, 2)])

    def test_a_refusal_is_not_remembered_as_flying(self):
        self.load("freedoom2.wad", "nothere.wad", "MAP01")
        self.load("freedoom2.wad", "nothere.wad", "MAP01")
        self.assertEqual([a[0] for a in self.answers()], [wu.FAILED, wu.FAILED])
        self.assertEqual(self.children, [])

    def test_a_failed_proof_is_not_remembered_as_flying(self):
        # The probe child dies (a damaged WAD kills ViZDoom), and the answer is lost: the repeat is proven again
        dp.subprocess.Popen.side_effect = lambda cmd, **kw: (self.children.append(cmd), mock.Mock(
            pid=4321, poll=mock.Mock(return_value=-11), wait=mock.Mock(return_value=-11)))[1]
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertFalse(self.p.poll_wad())
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertIsNotNone(self.p.wad_job, "proven again, not ALREADY")
        self.assertFalse(self.p.poll_wad())
        self.assertEqual([a[0] for a in self.answers()], [wu.FAILED, wu.FAILED])
        self.assertEqual(self.p.switch_wad.call_count, 0)

    def test_a_failed_switch_is_not_remembered_as_flying(self):
        # The proof passed but the game would not rebuild on the files, so the old game flies on
        self.p.switch_wad.side_effect = lambda *a: "the game would not start on it (RuntimeError: x)"
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertFalse(self.p.poll_wad())
        self.load("freedoom2.wad", "basic.wad", "MAP01")
        self.assertIsNotNone(self.p.wad_job, "proven again, not ALREADY")
        self.assertFalse(self.p.poll_wad())
        self.assertEqual([a[0] for a in self.answers()], [wu.FAILED, wu.FAILED])
        self.assertEqual(self.p.switch_wad.call_count, 2)


if __name__ == "__main__":
    unittest.main()
