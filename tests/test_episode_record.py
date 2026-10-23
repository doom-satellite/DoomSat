"""The payload's episode record (payload/episode_record.py), which the ground's data system downlinks.

It must change nothing unless asked: the bench builds the payload from its own argparse.Namespace, which has no
`records` option, and the flight default is off. When it is on, the file it writes is what the ground compares
against the archive, so its name (episode and last tic, short enough for F' command strings), its counts, its
positions (rounded exactly as an F32 channel rounds them) and its context (the WAD flown, which LOAD_WAD can
switch) are pinned here. A failure to write must never escape into the game loop.
"""
import argparse
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "payload"))
import episode_record as er  # noqa: E402


def obs(episode, tic, x=10.123456789, y=-20.5, health=100, kills=0, explored=1, dead=0, done=0):
    return {"episode": episode, "tic": tic, "x": x, "y": y, "health": health, "kills": kills, "explored": explored,
            "keys": 0, "level": 1, "dead": dead, "level_done": done}


class FakePayload:
    wad = "/root/doom/wads/freedoom1.wad"
    pwad = None
    map = "E1M1"
    level = 1
    geometry = "off"
    oracle = "off"


class TestOffUnlessAsked(unittest.TestCase):
    def test_default_and_bench_namespaces_get_no_recorder(self):
        self.assertIsNone(er.from_args(argparse.Namespace(records="off"), FakePayload()))
        bench = argparse.Namespace(port=0, wad="doom1.wad", map="E1M1", skill=3, seed=1, fps=0, quality=45,
                                   status_every=3, map_png=None, geometry="off", oracle="off")
        self.assertIsNone(er.from_args(bench, FakePayload()))

    def test_on_writes_under_doomsat_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("DOOMSAT_HOME")
            os.environ["DOOMSAT_HOME"] = tmp
            try:
                rec = er.from_args(argparse.Namespace(records="on", skill=3, seed=7), FakePayload())
            finally:
                if old is None:
                    del os.environ["DOOMSAT_HOME"]
                else:
                    os.environ["DOOMSAT_HOME"] = old
            self.assertEqual(rec.path, os.path.join(tmp, "run", "rec"))
            self.assertEqual(rec.context(), {"wad": "freedoom1.wad", "pwad": None, "map": "E1M1", "level": 1,
                                             "skill": 3, "seed": 7, "geometry": "off", "oracle": "off"})

    def test_the_context_is_the_wad_flown_when_each_episode_begins(self):
        # The payload's switch_wad sets wad, pwad and map to the switched game's (paths, pinned under the uplink
        # directory with their file names kept) and starts a new episode; the context is read at each start.
        payload = FakePayload()
        rec = er.from_args(argparse.Namespace(records="on", skill=3, seed=7), payload)
        with tempfile.TemporaryDirectory() as tmp:
            rec.path = tmp
            rec.status(obs(1, 3))
            payload.wad = "/root/doom/wads/uplink/.pinned/1/freedoom2.wad"
            payload.pwad = "/root/doom/wads/uplink/.pinned/1/basic.wad"
            payload.map = "MAP01"
            rec.status(obs(2, 3))                              # LOAD_WAD: the game rebuilt, a new episode number
            with open(os.path.join(tmp, "1-3.json")) as f:
                closed = json.load(f)
        self.assertEqual(closed["outcome"], "reset")           # the ground calls this one wad_switch
        self.assertEqual((closed["context"]["wad"], closed["context"]["pwad"]), ("freedoom1.wad", None))
        self.assertEqual((rec.cur["context"]["wad"], rec.cur["context"]["pwad"], rec.cur["context"]["map"]),
                         ("freedoom2.wad", "basic.wad", "MAP01"))


class TestTheRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = er.EpisodeRecorder(self.tmp.name, lambda: {"wad": "freedoom1.wad", "map": "E1M1"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_episode_that_ends_in_death(self):
        for tic in range(4, 200, 3):
            self.rec.status(obs(2, tic, x=float(tic), kills=tic // 100, health=100 - tic // 4))
        self.rec.status(obs(2, 200, x=200.0, kills=2, health=40, dead=1))
        path = self.rec.close("died")
        self.assertEqual(os.path.basename(path), "2-200.json")
        with open(path) as f:
            r = json.load(f)
        self.assertEqual(r["record"], {"format": "doomsat-episode-record", "version": 1})
        self.assertEqual((r["episode"], r["outcome"], r["first_tic"], r["last_tic"]), (2, "died", 4, 200))
        self.assertEqual(r["statuses_sent"], len(range(4, 200, 3)) + 1)
        self.assertEqual(r["final"]["kills"], 2)
        self.assertEqual(r["final"]["dead"], 1)
        self.assertEqual(r["health_min"], 40)
        self.assertEqual(r["context"], {"wad": "freedoom1.wad", "map": "E1M1"})
        tics = [p[0] for p in r["path"]]
        self.assertEqual(tics[0], 4)
        self.assertEqual(tics[-1], 200)                       # the final position is always in the path
        self.assertTrue(all(b - a >= er.PATH_EVERY_TICS for a, b in zip(tics[:-2], tics[1:-1])))

    def test_a_final_status_that_repeats_the_last_one_is_not_counted_twice(self):
        # Review finding: when the last tic was also a periodic send, the death path sends that same status again;
        # counting it made a perfect downlink read as 99.997% (the ground counts distinct tics).
        for tic in (3, 6, 9):
            self.rec.status(obs(1, tic))
        self.rec.status(obs(1, 9, dead=1))               # the final status, same tic as the last periodic one
        self.rec.close("died")
        with open(os.path.join(self.tmp.name, "1-9.json")) as f:
            r = json.load(f)
        self.assertEqual(r["statuses_sent"], 3)
        self.assertEqual(r["final"]["dead"], 1)          # but its values are the final ones

    def test_positions_are_rounded_as_an_F32_channel_rounds_them(self):
        self.rec.status(obs(1, 3, x=10.123456789, y=0.1))
        self.rec.close("died")
        with open(os.path.join(self.tmp.name, "1-3.json")) as f:
            r = json.load(f)
        self.assertEqual(r["final"]["x"], 10.1234570)          # float32(10.123456789) to 9 significant digits
        self.assertEqual(r["final"]["y"], er.f32(0.1))
        self.assertNotEqual(r["final"]["y"], 0.1)

    def test_a_reset_is_closed_when_the_next_episode_appears(self):
        self.rec.status(obs(3, 10))
        self.rec.status(obs(3, 13))
        self.rec.status(obs(4, 1))                             # RESET_GAME: no death, no exit, a new number
        with open(os.path.join(self.tmp.name, "3-13.json")) as f:
            self.assertEqual(json.load(f)["outcome"], "reset")
        self.assertEqual(self.rec.cur["episode"], 4)

    def test_written_once_and_never_without_a_status(self):
        self.assertIsNone(self.rec.close("died"))
        self.rec.status(obs(5, 7))
        self.assertIsNotNone(self.rec.close("died"))
        self.assertIsNone(self.rec.close("died"))
        self.rec.status(obs(6, 1))                             # the already-written episode is not closed again
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["5-7.json"])

    def test_names_fit_a_flight_command_string(self):
        self.assertLessEqual(len("/root/doom/run/rec/" + "65535-4294967295.json"), 40)

    def test_a_failed_write_does_not_escape(self):
        blocker = os.path.join(self.tmp.name, "rec")
        open(blocker, "w").close()                              # a file where the directory should be
        rec = er.EpisodeRecorder(blocker, lambda: {})
        rec.status(obs(1, 3))
        self.assertIsNone(rec.close("died"))


if __name__ == "__main__":
    unittest.main()
