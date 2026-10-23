"""The context an episode was flown in (doomsat_sds.context): WAD, map, skill, seed, pilot mode, dev or test set.

Before main none of it was in telemetry (docs/plans/sds-airflow.md, fact 12): it existed only in the running
payload's and pilot's command lines and in payload.log, so the forward run reads it from the live process table once
and the catalog keeps it for good. On main the payload can switch WAD in flight (LOAD_WAD), so its --wad can be
wrong, and the WAD comes from the WAD_IWAD, WAD_PWAD and WAD_LOADS channels instead: the value in effect when the
episode began, never the one a switch at its end wrote. Three mistakes would poison every product built on it.
Crediting an episode to a process that started after the episode began (a payload restarted with another WAD, a
pilot restarted in another mode) records a guess as a fact, so such values must come back unknown, with a source
that says why. Confusing the dev set with the locked test level (charter 2.5) would put tuning runs into the score,
so the dev/test label is read from the harness's own research/levels.yaml, which these tests read and never write.
And a switched or patched WAD is a demonstration, never dev or test, whatever its file is called.
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import config, context  # noqa: E402

REPO = Path(SDS).parents[1]
LEVELS = REPO / "research" / "levels.yaml"
START_MS = 1791074773533                  # episode 2 of the recorded flight began here (TM time)
BEFORE = START_MS / 1000 - 600            # a process start time (wall clock, s) well before the episode
AFTER = START_MS / 1000 + 60              # and one after it
PAYLOAD_PY = "/root/doom/payload-venv/bin/python"
PAYLOAD_SCRIPT = "/home/user/DoomSat/payload/doom_payload.py"
# As scripts/wsl_run_flight.sh starts it (abridged).
PAYLOAD_ARGV = [PAYLOAD_PY, PAYLOAD_SCRIPT, "--fps", "10", "--skill", "4", "--wad", "freedoom1.wad", "--map", "E1M1",
                "--geometry", "on", "--oracle", "off", "--seed=11"]
# As scripts/start_pilot.sh execs it.
PILOT_ARGV = ["/home/user/DoomSat/ground/.venv/bin/python", "pilot.py", "--duration", "120",
              "--system-one", "code", "--system-two", "none"]
# A LOAD_WAD check, as doom_payload.py request_wad starts it (its forked watchdog has the same argv).
PROBE_ARGV = [PAYLOAD_PY, PAYLOAD_SCRIPT, "--probe", "--wad", "/root/doom/wads/uplink/.pin-1-freedoom2.wad",
              "--map", "MAP01", "--skill", "4", "--seed", "11", "--geometry", "on"]
LOG = "\n".join([
    "[payload] listening on port 4242",
    "[payload] episode 1 started on E1M1 (level 1)",
    "[payload] episode 2 started on E1M2 (level 2)",
    "[payload] episode 3 started on E1M3 (level 3)",
])


def proc(pid, argv, start_s):
    return {"pid": pid, "argv": list(argv), "start_s": start_s}


class TestOptions(unittest.TestCase):
    def test_space_separated_values(self):
        self.assertEqual(context._options(["python", "x.py", "--wad", "freedoom1.wad", "--skill", "3"]),
                         {"--wad": "freedoom1.wad", "--skill": "3"})

    def test_equals_values(self):
        self.assertEqual(context._options(["x.py", "--seed=11", "--map-png=/tmp/a=b.png"]),
                         {"--seed": "11", "--map-png": "/tmp/a=b.png"})

    def test_a_flag_at_the_end_reads_true(self):
        self.assertEqual(context._options(["pilot.py", "--duration", "120", "--log-questions"]),
                         {"--duration": "120", "--log-questions": "true"})

    # Regression test for a bug the tests found (now fixed): context._options (context.py:66-67) reads the token after a flag with next(it) and, when that
    # token is another option, records the flag as "true" but drops the option it consumed: "--geometry --seed 9"
    # loses --seed (and "9" is then skipped as a positional).
    def test_a_flag_followed_by_another_option_keeps_both(self):
        self.assertEqual(context._options(["x.py", "--geometry", "--seed", "9", "--oracle"]),
                         {"--geometry": "true", "--seed": "9", "--oracle": "true"})

    def test_positionals_and_short_options_are_ignored(self):
        self.assertEqual(context._options(["python3", "-u", "x.py", "extra", "--wad", "a.wad", "-v"]),
                         {"--wad": "a.wad"})


class TestFind(unittest.TestCase):
    def test_the_interpreter_wins_over_a_bash_wrapper_that_mentions_the_script(self):
        # setsid -f bash -c "... doom_payload.py" starts first, and its argv ends with the script's path.
        wrapper = proc(10, ["bash", "-c", "exec %s %s" % (PAYLOAD_PY, PAYLOAD_SCRIPT)], BEFORE - 1)
        wrapper2 = proc(11, ["/bin/bash", PAYLOAD_SCRIPT], BEFORE - 2)
        python = proc(12, PAYLOAD_ARGV, BEFORE)
        self.assertEqual(context._find([wrapper, wrapper2, python], "doom_payload.py")["pid"], 12)

    def test_the_oldest_interpreter_wins(self):
        procs = [proc(21, PAYLOAD_ARGV, BEFORE + 30), proc(22, PAYLOAD_ARGV, BEFORE), proc(23, PAYLOAD_ARGV, BEFORE + 9)]
        self.assertEqual(context._find(procs, "doom_payload.py")["pid"], 22)

    def test_a_bare_script_name_matches(self):
        self.assertEqual(context._find([proc(31, PILOT_ARGV, BEFORE)], "pilot.py")["pid"], 31)

    def test_only_the_whole_file_name_matches(self):
        procs = [proc(41, ["python", "ground/autopilot.py"], BEFORE), proc(42, ["python", "my_doom_payload.py"], BEFORE),
                 proc(43, ["python", "payload/doom_payload.py.orig"], BEFORE)]
        self.assertIsNone(context._find(procs, "doom_payload.py"))
        self.assertIsNone(context._find(procs, "pilot.py"))

    def test_none_when_nothing_runs_it(self):
        self.assertIsNone(context._find([], "pilot.py"))

    def test_a_load_wad_check_and_its_watchdog_are_not_the_payload(self):
        # Oldest wins, so the probe and its watchdog get the oldest start times: the filter keeps them out, not age.
        probe, watchdog = proc(61, PROBE_ARGV, BEFORE - 100), proc(62, PROBE_ARGV, BEFORE - 100)
        self.assertEqual(context._find([probe, watchdog, proc(63, PAYLOAD_ARGV, BEFORE)], "doom_payload.py")["pid"], 63)
        self.assertIsNone(context._find([probe, watchdog], "doom_payload.py"))


class TestMapFromLog(unittest.TestCase):
    def test_map_and_level_for_the_episode(self):
        self.assertEqual(context.map_from_log(LOG, 3), ("E1M3", 3))
        self.assertEqual(context.map_from_log(LOG, 1), ("E1M1", 1))
        self.assertIsInstance(context.map_from_log(LOG, 2)[1], int)

    def test_none_when_the_episode_is_not_in_the_log(self):
        self.assertIsNone(context.map_from_log(LOG, 4))
        self.assertIsNone(context.map_from_log("", 1))

    def test_the_number_must_match_exactly(self):
        self.assertIsNone(context.map_from_log("[payload] episode 13 started on E1M3 (level 3)", 1))

    def test_only_payload_lines_count(self):
        text = "[pilot] episode 2 started on E1M9 (level 9)\n  [payload] episode 2 started on E1M2 (level 2)  "
        self.assertEqual(context.map_from_log(text, 2), ("E1M2", 2))

    def test_the_latest_line_for_a_number_wins(self):
        # The log of one payload process that restarted the game counts from 1 again; the latest is this flight.
        text = LOG + "\n[payload] episode 1 started on E1M4 (level 4)"
        self.assertEqual(context.map_from_log(text, 1), ("E1M4", 4))


class TestLevelSet(unittest.TestCase):
    """Against the real research/levels.yaml, which belongs to the harness and is only read."""

    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo = config.Settings(home=Path(tmp) / "sds", doomsat_home=Path(tmp) / "doom").repo
        self.before = hashlib.sha256(LEVELS.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(LEVELS.read_bytes()).hexdigest(), self.before, "levels.yaml was written")

    def test_settings_point_at_this_repo(self):
        self.assertEqual(Path(self.repo).resolve(), REPO.resolve())

    def test_dev_test_and_other(self):
        self.assertEqual(context.level_set(self.repo, "freedoom1.wad", "E1M1"), "dev")
        self.assertEqual(context.level_set(self.repo, "doom1.wad", "E1M1"), "test")
        self.assertEqual(context.level_set(self.repo, "freedoom1.wad", "E1M7"), "other")
        self.assertEqual(context.level_set(self.repo, "freedoom2.wad", "MAP01"), "other")

    def test_a_wad_given_as_a_path_counts_by_its_name(self):
        self.assertEqual(context.level_set(self.repo, "/root/doom/wads/freedoom1.wad", "E1M2"), "dev")

    def test_unknown_without_both_or_without_the_file(self):
        self.assertEqual(context.level_set(self.repo, None, "E1M1"), "unknown")
        self.assertEqual(context.level_set(self.repo, "doom1.wad", None), "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(context.level_set(Path(tmp), "doom1.wad", "E1M1"), "unknown")

    def test_a_switched_or_patched_wad_is_never_dev_or_test(self):
        # README.md: flights on an uplinked WAD are demonstrations, never benched or graded. An uplinked file can
        # carry an installed one's name (LOAD_WAD looks in the uplink directory first), so the name proves nothing.
        for wad, map_name in (("freedoom1.wad", "E1M1"), ("doom1.wad", "E1M1")):
            self.assertEqual(context.level_set(self.repo, wad, map_name, wad_loads=1), "other")
            self.assertEqual(context.level_set(self.repo, wad, map_name, pwad="basic.wad"), "other")
            self.assertEqual(context.level_set(self.repo, wad, map_name, pwad=None, wad_loads=0),
                             {"freedoom1.wad": "dev", "doom1.wad": "test"}[wad])
        self.assertEqual(context.level_set(self.repo, "doom1.wad", None, wad_loads=2), "other")   # no map needed
        self.assertEqual(context.level_set(self.repo, "doom1.wad", "E1M1", wad_loads=None), "test")


def hexname(name: str) -> str:
    """A WadName sample as archive.value() returns it: 40 bytes, zero-padded, as hex (live: WAD_IWAD)."""
    return name.encode("ascii").ljust(40, b"\0").hex()


class WadArchive:
    """WAD_IWAD / WAD_PWAD / WAD_LOADS samples (generation ms, reception ms, value), served as the archive does:
    start inclusive, stop exclusive. `fail` is raised by every read instead."""

    def __init__(self, samples=(), fail=None):
        self.samples, self.fail, self.asked = list(samples), fail, []

    def parameters(self, names, start_ms, stop_ms):
        self.asked.append((list(names), start_ms, stop_ms))
        if self.fail is not None:
            raise self.fail
        out = {n: [] for n in names}
        for t, base, patch, loads in self.samples:
            if start_ms <= t < stop_ms:
                for n, v in zip(config.CONTEXT_TLM, (hexname(base), hexname(patch), loads)):
                    if n in out:
                        out[n].append((t, t - 950, v))
        return out


class YamcsError(Exception):
    """Stands in for yamcs.client.core.exceptions.YamcsError / NotFound, which word every answer this way."""


class TestWadName(unittest.TestCase):
    def test_the_live_hex_decodes_to_the_file_name(self):
        live = "66726565646f6f6d312e776164" + "00" * 27          # WAD_IWAD on the CFDP stack, 4 October 2026
        self.assertEqual(context.wad_name(live), "freedoom1.wad")
        self.assertEqual(bytes.fromhex(live).rstrip(b"\0").decode("ascii", "replace"), "freedoom1.wad")

    def test_all_zeros_is_no_patch(self):
        self.assertIsNone(context.wad_name("00" * 40))
        self.assertIsNone(context.wad_name(""))
        self.assertIsNone(context.wad_name(None))

    def test_bytes_and_text_are_taken_as_they_come(self):
        self.assertEqual(context.wad_name(b"basic.wad\0\0\0"), "basic.wad")
        self.assertEqual(context.wad_name("freedoom2.wad"), "freedoom2.wad")     # not hex: already a name


class TestFlownWad(unittest.TestCase):
    """The WAD in effect when the episode began: what was flown in it, whatever a switch at its end wrote."""
    W = (START_MS, START_MS + 30_000)                    # an episode's window [first, last] from locate
    END = START_MS + 32_000                              # its closing EpisodeStarted

    def flown(self, samples, window=W, end=END):
        return context.flown_wad(WadArchive(samples), list(window), end)

    def test_a_stale_sample_before_the_window_loses_to_one_inside_it(self):
        # The WAD cannot change inside a window, so a sample in it is right; one before it may be F''s repeat of the
        # WAD a switch or a payload restart has just replaced.
        samples = [(START_MS - 1_900, "freedoom1.wad", "", 0), (START_MS - 900, "freedoom1.wad", "", 0),
                   (START_MS + 100, "freedoom2.wad", "basic.wad", 1),
                   (START_MS + 1_100, "freedoom2.wad", "basic.wad", 1)]
        f = self.flown(samples)
        self.assertEqual((f["wad"], f["pwad"], f["wad_loads"], f["t_ms"]),
                         ("freedoom2.wad", "basic.wad", 1, START_MS + 100))
        self.assertIn("during the episode", f["how"])
        with self.subTest("a sample at the window's first status is inside it"):
            f = self.flown([(START_MS - 900, "freedoom1.wad", "", 0), (START_MS, "freedoom2.wad", "", 1)])
            self.assertEqual((f["wad"], f["t_ms"]), ("freedoom2.wad", START_MS))

    def test_the_lost_write_of_a_switch_does_not_give_the_old_wad(self):
        # A switch at T: F' writes the new WAD once (handleWad), and that sample is lost, as whole TM samples are on
        # this stack. F''s 1 Hz repeats say the old WAD up to T and the new one after it; the switched episode's
        # first status is about 90 ms after T.
        T = START_MS
        old = [(t, "freedoom1.wad", "", 0) for t in (T - 2_070, T - 1_070, T - 70)]
        new = [(t, "freedoom2.wad", "", 1) for t in (T + 930, T + 1_930)]
        f = self.flown(old + new, window=(T + 90, T + 60_000), end=T + 62_000)
        self.assertEqual((f["wad"], f["pwad"], f["wad_loads"], f["t_ms"]), ("freedoom2.wad", None, 1, T + 930))

    def test_the_wad_a_switch_wrote_after_the_window_is_not_the_old_episodes(self):
        # 1 Hz samples of the old WAD through the episode, then the switch: its sample lands after the window's
        # last status and before the EpisodeStarted that closes the episode.
        old = [(t, "freedoom1.wad", "", 0) for t in range(START_MS - 4_000, self.W[1] + 1, 1_000)]
        new = [(self.W[1] + 800, "freedoom2.wad", "basic.wad", 1)]
        f = self.flown(old + new)
        self.assertEqual((f["wad"], f["pwad"], f["wad_loads"]), ("freedoom1.wad", None, 0))
        with self.subTest("and for the episode the switch started, the new WAD"):
            nxt = (self.END + 50, self.END + 20_000)
            later = [(t, "freedoom2.wad", "basic.wad", 1) for t in range(self.W[1] + 1_800, nxt[1], 1_000)]
            f = self.flown(old + new + later, window=nxt, end=nxt[1] + 2_000)
            self.assertEqual((f["wad"], f["pwad"], f["wad_loads"], f["t_ms"]),
                             ("freedoom2.wad", "basic.wad", 1, self.W[1] + 2_800))     # F''s first repeat in it
            # with no sample in the window, the last one before it: the switch's own
            f = self.flown(old + new, window=nxt, end=nxt[1] + 2_000)
            self.assertEqual((f["wad"], f["t_ms"]), ("freedoom2.wad", self.W[1] + 800))
            self.assertIn("before the episode", f["how"])

    def test_without_one_before_the_first_one_in_the_window(self):
        # A flight's first episode: the archive starts after the payload's report on connect, and F' repeats it.
        samples = [(START_MS - 6_000, "doom1.wad", "", 0), (START_MS + 700, "freedoom1.wad", "", 0),
                   (START_MS + 1_700, "freedoom1.wad", "", 0)]
        f = self.flown(samples)
        self.assertEqual((f["wad"], f["t_ms"]), ("freedoom1.wad", START_MS + 700))      # older than 5 s: not used
        self.assertIn("during the episode", f["how"])

    def test_nothing_after_the_window_or_the_closure(self):
        self.assertIn("missing", self.flown([(self.W[1] + 1, "freedoom2.wad", "", 1)]))
        # A death's window may end up to END_PAD_MS after its event; the event bounds the read too.
        f = self.flown([(self.END - 10, "freedoom2.wad", "", 1)], window=(START_MS, self.END + 1_000), end=self.END)
        self.assertEqual(f["wad"], "freedoom2.wad")
        self.assertIn("missing", self.flown([(self.END + 10, "freedoom2.wad", "", 1)],
                                            window=(START_MS, self.END + 1_000), end=self.END))

    def test_it_reads_only_the_wad_channels_around_the_window(self):
        arch = WadArchive([(START_MS - 900, "freedoom1.wad", "", 0)])
        context.flown_wad(arch, list(self.W), self.END)
        self.assertEqual(arch.asked, [(list(config.CONTEXT_TLM), START_MS - context.WAD_LOOKBACK_MS, self.W[1] + 1)])

    def test_no_samples_is_a_flight_build_from_before_main(self):
        f = self.flown([])
        self.assertEqual(list(f), ["missing"])
        self.assertIn("before main", f["missing"])

    def test_names_the_archive_does_not_know_fall_back(self):
        # Yamcs answers 404 "No such parameter" (or 400) for a name its mission database lacks.
        refused = YamcsError("404 Client Error: No such parameter (missing namespace?)")
        f = context.flown_wad(WadArchive(fail=refused), list(self.W), self.END)
        self.assertEqual(list(f), ["missing"])
        self.assertIn("refused", f["missing"])
        self.assertIn("No such parameter", f["missing"])

    def test_a_yamcs_that_is_down_or_failing_is_raised_not_guessed(self):
        # The context is cataloged for good: a flicker must fail the task (it is retried), not record argv as fact.
        # Only 404 and 400 say the names are unknown; a refused login or a busy server says nothing about them.
        for fail in (ConnectionError("refused"), TimeoutError("read timed out"), YamcsError("500 Server Error: x"),
                     YamcsError("401 Client Error: Unauthorized"), YamcsError("403 Client Error: Forbidden"),
                     YamcsError("429 Client Error: Too Many Requests")):
            with self.subTest(fail=fail):
                with self.assertRaises(type(fail)):
                    context.flown_wad(WadArchive(fail=fail), list(self.W), self.END)


class CaptureCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = config.Settings(home=Path(tmp.name) / "sds", doomsat_home=Path(tmp.name) / "doom")
        for target, kw in (("doomsat_sds.context.repo_commit", {"return_value": "feedc0ffee12"}),
                           ("doomsat_sds.context.processes", {"side_effect": AssertionError("read /proc")})):
            p = mock.patch(target, **kw)
            setattr(self, target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)


class TestCapture(CaptureCase):
    def capture(self, procs, number=2, log_text=LOG):
        return context.capture(self.settings, number, START_MS, procs=procs, log_text=log_text)

    def test_a_payload_running_before_the_episode_gives_its_arguments(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE), proc(202, PILOT_ARGV, BEFORE)])
        self.assertEqual((ctx["wad"], ctx["skill"], ctx["seed"], ctx["geometry"], ctx["oracle"]),
                         ("freedoom1.wad", 4, 11, "on", "off"))
        self.assertEqual((ctx["map"], ctx["level"]), ("E1M2", 2))
        self.assertIn("pid 101", ctx["sources"]["payload"])
        self.assertTrue(ctx["sources"]["payload"].startswith("arguments of the running payload"))
        self.assertEqual(ctx["sources"]["map"], "payload.log")
        self.assertEqual(ctx["level_set"], "dev")
        self.assertEqual(ctx["repo_commit"], "feedc0ffee12")
        self.repo_commit.assert_called_once_with(self.settings.repo)

    def test_skill_and_seed_are_integers(self):
        for argv in (PAYLOAD_ARGV, [PAYLOAD_PY, PAYLOAD_SCRIPT]):
            ctx = self.capture([proc(101, argv, BEFORE)])
            for key in ("skill", "seed"):
                self.assertIs(type(ctx[key]), int, (key, argv))

    def test_unset_arguments_take_the_payload_defaults(self):
        ctx = self.capture([proc(101, [PAYLOAD_PY, PAYLOAD_SCRIPT], BEFORE)])
        self.assertEqual((ctx["wad"], ctx["skill"], ctx["seed"]), ("doom1.wad", 2, 7))
        self.assertEqual(ctx["level_set"], "test")          # doom1.wad E1M2, from the log

    # Regression test for a bug the tests found (now fixed): context.PAYLOAD_DEFAULTS (context.py:21) mirrors doom_payload.py's argparse defaults but leaves
    # out --geometry and --oracle (both default "off", doom_payload.py:1417-1420), so a payload known to have flown
    # the episode, started without those options, is recorded with geometry and oracle None: the value capture()
    # reserves for "no payload process was running", not the "off" that payload actually ran with.
    def test_unset_geometry_and_oracle_are_the_payload_defaults(self):
        ctx = self.capture([proc(101, [PAYLOAD_PY, PAYLOAD_SCRIPT, "--wad", "freedoom1.wad"], BEFORE)])
        self.assertEqual((ctx["geometry"], ctx["oracle"]), ("off", "off"))

    def test_a_payload_started_after_the_episode_did_not_fly_it(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, AFTER), proc(202, PILOT_ARGV, BEFORE)])
        for key in ("wad", "skill", "seed", "geometry", "oracle", "map"):
            self.assertIsNone(ctx[key], key)
        self.assertNotIn("level", ctx)
        self.assertTrue(ctx["sources"]["payload"].startswith("unknown"), ctx["sources"]["payload"])
        self.assertEqual(ctx["level_set"], "unknown")

    def test_a_payload_with_no_start_time_is_not_trusted(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, None)])
        self.assertIsNone(ctx["wad"])
        self.assertTrue(ctx["sources"]["payload"].startswith("unknown"))

    def test_the_tm_clock_running_ahead_is_allowed_for(self):
        # TM time runs about a second ahead of the process clock, so a payload whose start_s is a few seconds
        # past the episode's TM start was still there when it began.
        ctx = self.capture([proc(101, PAYLOAD_ARGV, START_MS / 1000 + 3)])
        self.assertEqual(ctx["wad"], "freedoom1.wad")

    def test_episode_1_without_a_log_line_takes_the_map_argument(self):
        argv = PAYLOAD_ARGV[:]
        argv[argv.index("--map") + 1] = "E1M3"
        ctx = self.capture([proc(101, argv, BEFORE)], number=1, log_text="")
        self.assertEqual(ctx["map"], "E1M3")
        self.assertEqual(ctx["sources"]["map"], "payload --map (episode 1)")
        self.assertEqual(ctx["level_set"], "dev")

    def test_a_later_episode_without_a_log_line_has_no_map(self):
        # The payload advances maps on its own; after episode 1 the --map argument says nothing.
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], number=5)
        self.assertIsNone(ctx["map"])
        self.assertEqual(ctx["sources"]["map"], "unknown")
        self.assertEqual(ctx["level_set"], "unknown")

    def test_payload_log_is_read_from_the_run_directory(self):
        self.settings.run_dir.mkdir(parents=True)
        (self.settings.run_dir / "payload.log").write_text(LOG + "\n", encoding="utf-8")
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], number=3, log_text=None)
        self.assertEqual((ctx["map"], ctx["level"], ctx["sources"]["map"]), ("E1M3", 3, "payload.log"))

    def test_a_missing_payload_log_is_not_an_error(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], number=1, log_text=None)
        self.assertEqual((ctx["map"], ctx["sources"]["map"]), ("E1M1", "payload --map (episode 1)"))

    def test_a_pilot_running_before_the_episode_gives_its_mode(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE), proc(202, PILOT_ARGV, BEFORE)])
        self.assertEqual(ctx["pilot_mode"], "system-one=code system-two=none")
        self.assertIn("pid 202", ctx["sources"]["pilot"])

    def test_pilot_defaults_and_equals_form(self):
        ctx = self.capture([proc(202, ["python", "ground/pilot.py"], BEFORE)])
        self.assertEqual(ctx["pilot_mode"], "system-one=typesafe system-two=claude-cli")
        ctx = self.capture([proc(202, ["python", "ground/pilot.py", "--system-two=anthropic"], BEFORE)])
        self.assertEqual(ctx["pilot_mode"], "system-one=typesafe system-two=anthropic")

    # Regression test for a bug the tests found (now fixed): the same context._options defect as seen in the catalog. pilot.py has flags that take
    # no value (--no-after-action, --auto-apply-graph, --log-questions, --no-reset; pilot.py's argument parser), and
    # "pilot.py --log-questions --system-two none" swallows --system-two, so the episode is cataloged as flown
    # with "system-two=claude-cli", a mode it did not have. scripts/play.sh documents such a call: "play.sh
    # --no-reset --system-two claude-cli" runs "pilot.py --system-one manual --system-two none --no-reset
    # --system-two claude-cli", which argparse reads as claude-cli (the last one wins) and capture() records as none.
    def test_a_pilot_flag_before_the_mode_does_not_hide_it(self):
        mode = lambda *args: self.capture([proc(202, ["python", "pilot.py", *args], BEFORE)])["pilot_mode"]
        start_pilot = mode("--log-questions", "--system-two", "none", "--auto-apply-graph", "--system-one", "code")
        play_sh = mode("--system-one", "manual", "--system-two", "none", "--no-reset", "--system-two", "claude-cli")
        self.assertEqual([start_pilot, play_sh],
                         ["system-one=code system-two=none", "system-one=manual system-two=claude-cli"])

    def test_a_pilot_started_after_the_episode_is_unknown(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE), proc(202, PILOT_ARGV, AFTER)])
        self.assertIsNone(ctx["pilot_mode"])
        self.assertTrue(ctx["sources"]["pilot"].startswith("unknown"), ctx["sources"]["pilot"])
        self.assertEqual(ctx["wad"], "freedoom1.wad")       # the payload's values do not depend on the pilot

    def test_no_pilot_running_is_unknown_not_none(self):
        # The context is captured a minute or so after the episode; a pilot run with --duration may have exited by
        # then. Its absence is not evidence that nobody flew, so it must not be recorded as "none".
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)])
        self.assertIsNone(ctx["pilot_mode"])
        self.assertTrue(ctx["sources"]["pilot"].startswith("unknown"), ctx["sources"]["pilot"])

    def test_a_pilot_wrapper_does_not_stand_in_for_the_pilot(self):
        wrapper = proc(201, ["bash", "-c", "exec python /home/user/DoomSat/ground/pilot.py"], BEFORE - 5)
        ctx = self.capture([wrapper, proc(202, PILOT_ARGV, AFTER)])
        self.assertIsNone(ctx["pilot_mode"])
        ctx = self.capture([wrapper])
        self.assertIsNone(ctx["pilot_mode"])

    def test_nothing_running(self):
        ctx = self.capture([])
        self.assertIsNone(ctx["wad"])
        self.assertIsNone(ctx["pilot_mode"])
        self.assertEqual(ctx["level_set"], "unknown")
        self.assertEqual(ctx["repo_commit"], "feedc0ffee12")


class TestCaptureOnMain(CaptureCase):
    """capture() with flown_wad()'s answer, as the forward run calls it on main."""
    TLM = {"wad": "freedoom2.wad", "pwad": None, "wad_loads": 1, "t_ms": START_MS + 400,
           "how": "the first sample during the episode"}
    PRE_MAIN = {"missing": "the archive has no WAD_IWAD sample from 5 s before the episode to its end, as on a "
                           "flight build from before main"}

    def capture(self, procs, number=2, log_text=LOG, flown=None):
        return context.capture(self.settings, number, START_MS, procs=procs, log_text=log_text, flown=flown)

    def test_the_wad_flown_is_the_telemetrys_not_the_launch_argument(self):
        # Launched on freedoom1.wad, switched to freedoom2.wad by LOAD_WAD before this episode began.
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], flown=self.TLM)
        self.assertEqual((ctx["wad"], ctx["pwad"], ctx["wad_loads"], ctx["wad_launch"]),
                         ("freedoom2.wad", None, 1, "freedoom1.wad"))
        self.assertTrue(ctx["sources"]["wad"].startswith("telemetry WAD_IWAD, WAD_PWAD, WAD_LOADS"), ctx["sources"])
        self.assertIn("2026-10-04T", ctx["sources"]["wad"])
        self.assertEqual((ctx["map"], ctx["sources"]["map"]), ("E1M2", "payload.log"))
        self.assertEqual(ctx["level_set"], "other")             # switched: a demonstration, whatever the names

    def test_the_telemetry_does_not_need_the_payload_process(self):
        ctx = self.capture([], flown=dict(self.TLM, wad="freedoom1.wad", pwad="basic.wad", wad_loads=0))
        self.assertEqual((ctx["wad"], ctx["pwad"], ctx["wad_loads"]), ("freedoom1.wad", "basic.wad", 0))
        self.assertIsNone(ctx["wad_launch"])
        self.assertIsNone(ctx["map"])
        self.assertTrue(ctx["sources"]["payload"].startswith("unknown"))
        self.assertEqual(ctx["level_set"], "other")             # a patch WAD: never dev or test

    def test_unswitched_telemetry_keeps_the_dev_and_test_labels(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], flown=dict(self.TLM, wad="freedoom1.wad", wad_loads=0))
        self.assertEqual(ctx["level_set"], "dev")

    def test_a_flight_build_from_before_main_falls_back_to_the_arguments_and_says_why(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], flown=self.PRE_MAIN)
        self.assertEqual((ctx["wad"], ctx["pwad"], ctx["wad_loads"], ctx["wad_launch"]),
                         ("freedoom1.wad", None, 0, "freedoom1.wad"))
        self.assertIn("pid 101", ctx["sources"]["wad"])
        self.assertIn("no WAD_IWAD sample", ctx["sources"]["wad"])
        self.assertEqual(ctx["level_set"], "dev")
        with self.subTest("and with nothing running, nothing is known"):
            ctx = self.capture([], flown=self.PRE_MAIN)
            self.assertEqual((ctx["wad"], ctx["pwad"], ctx["wad_loads"]), (None, None, None))
            self.assertTrue(ctx["sources"]["wad"].startswith("unknown"), ctx["sources"]["wad"])
            self.assertIn("no WAD_IWAD sample", ctx["sources"]["wad"])

    def test_a_refused_read_falls_back_the_same_way(self):
        refused = context.flown_wad(WadArchive(fail=YamcsError("400 Client Error: Invalid parameter name")),
                                    [START_MS, START_MS + 30_000], START_MS + 32_000)
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], flown=refused)
        self.assertEqual((ctx["wad"], ctx["wad_loads"]), ("freedoom1.wad", 0))
        self.assertIn("Invalid parameter name", ctx["sources"]["wad"])

    def test_the_fallback_reads_pwad_too(self):
        argv = PAYLOAD_ARGV + ["--pwad", "/root/doom/wads/uplink/basic.wad"]
        ctx = self.capture([proc(101, argv, BEFORE)], flown=self.PRE_MAIN)
        self.assertEqual((ctx["wad"], ctx["pwad"]), ("freedoom1.wad", "basic.wad"))
        self.assertEqual(ctx["level_set"], "other")

    def test_a_wad_given_as_a_path_is_kept_as_launched_and_named_by_its_file(self):
        argv = PAYLOAD_ARGV[:]
        argv[argv.index("--wad") + 1] = "/root/doom/wads/freedoom1.wad"
        ctx = self.capture([proc(101, argv, BEFORE)], flown=self.PRE_MAIN)
        self.assertEqual((ctx["wad"], ctx["wad_launch"]), ("freedoom1.wad", "/root/doom/wads/freedoom1.wad"))

    def test_after_a_switch_the_map_argument_says_nothing(self):
        argv = PAYLOAD_ARGV[:]
        argv[argv.index("--map") + 1] = "E1M3"
        ctx = self.capture([proc(101, argv, BEFORE)], number=1, log_text="", flown=self.TLM)
        self.assertIsNone(ctx["map"])
        self.assertEqual(ctx["sources"]["map"], "unknown")
        ctx = self.capture([proc(101, argv, BEFORE)], number=1, log_text="", flown=dict(self.TLM, wad_loads=0))
        self.assertEqual((ctx["map"], ctx["sources"]["map"]), ("E1M3", "payload --map (episode 1)"))

    def test_a_load_wad_check_running_now_is_not_taken_for_the_payload(self):
        ctx = self.capture([proc(61, PROBE_ARGV, BEFORE - 100), proc(101, PAYLOAD_ARGV, BEFORE)], flown=self.PRE_MAIN)
        self.assertIn("pid 101", ctx["sources"]["payload"])
        self.assertEqual(ctx["wad"], "freedoom1.wad")


if __name__ == "__main__":
    unittest.main()
