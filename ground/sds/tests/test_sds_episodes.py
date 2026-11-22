"""Episode closure and windows: doomsat_sds.episodes decides which episodes are finished and which stretch of
the archive belongs to each, and every product (L1, L2, the catalog id, the Airflow run id) inherits that answer.

There is no "episode over" message, and the events that stand in for one have holes (docs/plans/sds-airflow.md,
"What the code says", facts 1-2): a flight's first EpisodeStarted is never archived, a RESET_GAME ends an episode
with no PlayerDied or LevelFinished, F' re-announces the running episode when it restarts, and EPISODE starts again
at 1 when the payload restarts (in this repo's first flight, episode 1 was followed by another episode 1). A wrong
closure loses an episode or catalogs one twice; a wrong window mixes two episodes into one record. The last class
replays a real recording of the live archive (two deaths) and checks the window computed live for episode 2.
"""
import datetime as dt
import json
import os
import random
import sys
import unittest

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import archive, config  # noqa: E402
from doomsat_sds.episodes import (  # noqa: E402
    END_PAD_MS, INFER_AFTER_MS, MAX_SILENCE_MS, WAD_SWITCH_MS, ClosedEpisode, closed_episodes, episode_id, locate)

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
T0 = 1791074769000                      # 2026-10-04T00:46:09Z, the fixture's start


def ev(t, name, n, seq=None, **extra):
    """An archived F' event as archive.events() returns it: arguments in `extra`, as strings."""
    x = {"episode": str(n)}
    x.update({k: str(v) for k, v in extra.items()})
    return {"t": t, "type": config.EVENT_PREFIX + name, "extra": x, "seq": seq, "source": config.EVENT_SOURCE}


def died(n, t):
    return ClosedEpisode(n, t, "PlayerDied", "died", None)


def run(first_ms, last_ms, value, step=100):
    """Samples (generation ms, reception ms, value) every `step` ms over [first_ms, last_ms]."""
    return [(t, t - 950, value) for t in range(first_ms, last_ms + 1, step)]


def tics(first_ms, last_ms, tic0, step=100):
    return [(t, t - 950, tic0 + 3 * i) for i, t in enumerate(range(first_ms, last_ms + 1, step))]


class ClosedEpisodesTest(unittest.TestCase):
    def test_death_closes_the_episode_with_its_start(self):
        out = closed_episodes([ev(T0 + 1_000, "EpisodeStarted", 2), ev(T0 + 30_000, "PlayerDied", 2, tic=900)])
        self.assertEqual(out, [ClosedEpisode(2, T0 + 30_000, "PlayerDied", "died", T0 + 1_000, False)])

    def test_death_without_an_archived_start_has_no_start(self):
        # A flight's first EpisodeStarted fires before Yamcs listens, so episode 1's death arrives alone.
        self.assertEqual(closed_episodes([ev(T0, "PlayerDied", 1, tic=7788)]), [died(1, T0)])

    def test_level_finished(self):
        out = closed_episodes([ev(T0, "EpisodeStarted", 3), ev(T0 + 90_000, "LevelFinished", 3, tic=3000)])
        self.assertEqual(out, [ClosedEpisode(3, T0 + 90_000, "LevelFinished", "level_finished", T0)])

    def test_next_start_with_no_end_is_a_reset_at_the_start_time(self):
        # RESET_GAME (the pilot's level budget) ends an episode with no PlayerDied or LevelFinished.
        out = closed_episodes([ev(T0, "EpisodeStarted", 4), ev(T0 + 180_000, "EpisodeStarted", 5)])
        self.assertEqual(out, [ClosedEpisode(4, T0 + 180_000, "EpisodeStarted", "reset", T0)])

    def test_start_that_does_not_follow_on_is_interrupted(self):
        # The payload restarted mid-episode: EPISODE goes back to 1. Only n -> n+1 is a reset.
        for n in (1, 7):
            with self.subTest(next=n):
                out = closed_episodes([ev(T0, "EpisodeStarted", 5), ev(T0 + 20_000, "EpisodeStarted", n)])
                self.assertEqual(out, [ClosedEpisode(5, T0 + 20_000, "EpisodeStarted", "interrupted", T0)])

    def test_reannounced_start_closes_nothing(self):
        # F' restarted mid-episode and logged EpisodeStarted(3) again: still one episode, which began at the first.
        events = [ev(T0, "EpisodeStarted", 3), ev(T0 + 40_000, "EpisodeStarted", 3)]
        self.assertEqual(closed_episodes(events), [])
        out = closed_episodes(events + [ev(T0 + 60_000, "PlayerDied", 3, tic=2000)])
        self.assertEqual(out, [ClosedEpisode(3, T0 + 60_000, "PlayerDied", "died", T0)])

    def test_repeated_death_is_counted_once(self):
        with self.subTest("with its start"):
            out = closed_episodes([ev(T0, "EpisodeStarted", 2), ev(T0 + 9_000, "PlayerDied", 2, tic=1),
                                   ev(T0 + 9_500, "PlayerDied", 2, tic=1)])
            self.assertEqual(out, [ClosedEpisode(2, T0 + 9_000, "PlayerDied", "died", T0)])
        with self.subTest("without its start"):
            out = closed_episodes([ev(T0, "PlayerDied", 1), ev(T0 + 700, "PlayerDied", 1)])
            self.assertEqual(out, [died(1, T0)])

    def test_death_after_a_flight_restart_has_no_start(self):
        # Flight A was stopped in the 2 s pause after episode 3 died, and flight B's EpisodeStarted(1) was never
        # archived. Flight B's first death still closes its episode 1 (it is no repeat of episode 3's death), and
        # flight A's EpisodeStarted(3) is not its start.
        events = [ev(T0, "EpisodeStarted", 3), ev(T0 + 30_000, "PlayerDied", 3, tic=1000),
                  ev(T0 + 600_000, "PlayerDied", 1, tic=2000)]
        self.assertEqual(closed_episodes(events), [ClosedEpisode(3, T0 + 30_000, "PlayerDied", "died", T0),
                                                   died(1, T0 + 600_000)])
        with self.subTest("flight A stopped mid-episode"):
            out = closed_episodes([ev(T0, "EpisodeStarted", 3), ev(T0 + 600_000, "PlayerDied", 1, tic=2000)])
            self.assertEqual([e for e in out if e.number == 1], [died(1, T0 + 600_000)])

    def test_a_start_after_the_episode_closed_opens_a_new_one(self):
        # F' and the payload were restarted together just after episode 1 died, with Yamcs still listening, so
        # the new episode 1's start is archived. That is a new episode (episode 1 followed by episode 1, as in
        # the first flight), not a re-announcement, and its death is no repeat of the first one.
        events = [ev(T0, "PlayerDied", 1, tic=7788), ev(T0 + 20_000, "EpisodeStarted", 1),
                  ev(T0 + 50_000, "PlayerDied", 1, tic=1500)]
        out = closed_episodes(events)
        self.assertEqual(out, [died(1, T0), ClosedEpisode(1, T0 + 50_000, "PlayerDied", "died", T0 + 20_000)])
        self.assertNotEqual(out[0].episode_id, out[1].episode_id)

    def test_death_then_next_start_is_not_a_reset(self):
        # The normal cycle: death, the payload's 2 s pause, the next episode. Each episode closes exactly once.
        events = [ev(T0 + 2_233, "PlayerDied", 1, tic=7788), ev(T0 + 4_533, "EpisodeStarted", 2),
                  ev(T0 + 38_184, "PlayerDied", 2, tic=1180), ev(T0 + 40_483, "EpisodeStarted", 3)]
        self.assertEqual(closed_episodes(events), [
            ClosedEpisode(1, T0 + 2_233, "PlayerDied", "died", None),
            ClosedEpisode(2, T0 + 38_184, "PlayerDied", "died", T0 + 4_533)])

    def test_unsorted_input(self):
        events = [ev(T0, "EpisodeStarted", 1, seq=1), ev(T0 + 5_000, "PlayerDied", 1, seq=2),
                  ev(T0 + 7_000, "EpisodeStarted", 2, seq=3), ev(T0 + 200_000, "EpisodeStarted", 3, seq=4),
                  ev(T0 + 250_000, "LevelFinished", 3, seq=5), ev(T0 + 252_000, "EpisodeStarted", 4, seq=6),
                  ev(T0 + 260_000, "EpisodeStarted", 1, seq=7)]
        expected = closed_episodes(events)
        self.assertEqual([(e.number, e.outcome) for e in expected],
                         [(1, "died"), (2, "reset"), (3, "level_finished"), (4, "interrupted")])
        for k in range(5):
            shuffled = list(events)
            random.Random(k).shuffle(shuffled)
            self.assertEqual(closed_episodes(shuffled), expected)
        self.assertEqual(closed_episodes(list(reversed(events))), expected)

    def test_events_with_one_time_tag_keep_their_sequence_order(self):
        # Sorted on (time, seq): with the order reversed this would close episode 2 twice (reset, then died).
        events = [ev(T0, "EpisodeStarted", 2, seq=10), ev(T0 + 9_000, "EpisodeStarted", 3, seq=12),
                  ev(T0 + 9_000, "PlayerDied", 2, seq=11)]
        self.assertEqual(closed_episodes(events), [ClosedEpisode(2, T0 + 9_000, "PlayerDied", "died", T0)])

    def test_events_without_an_episode_number_are_ignored(self):
        core = [ev(T0, "EpisodeStarted", 2), ev(T0 + 9_000, "PlayerDied", 2)]
        noise = [{"t": T0 + 100, "type": config.EVENT_PREFIX + "IntentSet", "seq": 5,
                  "extra": {"intentId": "787", "mode": "EXPLORE", "ttlMs": "1500"}},
                 {"t": T0 + 200, "type": config.EVENT_PREFIX + "PlayerDied", "seq": 6, "extra": {"episode": "?"}},
                 {"t": T0 + 300, "type": config.EVENT_PREFIX + "PlayerDied", "seq": 7, "extra": None}]
        self.assertEqual(closed_episodes(core + noise), closed_episodes(core))


class InferredResetTest(unittest.TestCase):
    """EpisodeStarted(n) with nothing before it: episode n-1 was reset, if the events reach far enough back to
    have shown its death (a death is followed by the next start about 2 s later, never a minute)."""
    S = T0 - 3_600_000                  # where the event query began

    def test_infers_the_predecessor_when_the_scan_reaches_back_far_enough(self):
        t = self.S + INFER_AFTER_MS
        out = closed_episodes([ev(t, "EpisodeStarted", 4)], scan_start_ms=self.S)
        self.assertEqual(out, [ClosedEpisode(3, t, "EpisodeStarted", "reset", None, inferred=True)])

    def test_not_when_the_start_is_too_close_to_the_scan_start(self):
        # Its predecessor's death may be just before the scan began.
        out = closed_episodes([ev(self.S + INFER_AFTER_MS - 1, "EpisodeStarted", 4)], scan_start_ms=self.S)
        self.assertEqual(out, [])

    def test_never_for_episode_one(self):
        out = closed_episodes([ev(self.S + 10 * INFER_AFTER_MS, "EpisodeStarted", 1)], scan_start_ms=self.S)
        self.assertEqual(out, [])

    def test_never_without_scan_start(self):
        self.assertEqual(closed_episodes([ev(self.S + 10 * INFER_AFTER_MS, "EpisodeStarted", 4)]), [])

    def test_not_when_the_predecessor_is_in_the_events(self):
        with self.subTest("its start is there"):
            out = closed_episodes([ev(self.S + 1_000, "EpisodeStarted", 3),
                                   ev(self.S + 2 * INFER_AFTER_MS, "EpisodeStarted", 4)], scan_start_ms=self.S)
            self.assertEqual(out, [ClosedEpisode(3, self.S + 2 * INFER_AFTER_MS, "EpisodeStarted", "reset",
                                                 self.S + 1_000, inferred=False)])
        with self.subTest("its death is there"):
            out = closed_episodes([ev(self.S + 2 * INFER_AFTER_MS, "PlayerDied", 3),
                                   ev(self.S + 2 * INFER_AFTER_MS + 2_300, "EpisodeStarted", 4)], scan_start_ms=self.S)
            self.assertEqual(out, [died(3, self.S + 2 * INFER_AFTER_MS)])

    def test_the_started_episode_then_closes_normally(self):
        t = self.S + INFER_AFTER_MS + 5_000
        out = closed_episodes([ev(t, "EpisodeStarted", 4), ev(t + 30_000, "PlayerDied", 4)], scan_start_ms=self.S)
        self.assertEqual(out, [ClosedEpisode(3, t, "EpisodeStarted", "reset", None, inferred=True),
                               ClosedEpisode(4, t + 30_000, "PlayerDied", "died", t)])


class EpisodeIdTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(episode_id(1791074807184, 2), "20261004T004647Z-e0002")
        self.assertEqual(died(2, 1791074807184).episode_id, "20261004T004647Z-e0002")
        doc_example = archive.to_ms(dt.datetime(2026, 10, 4, 0, 15, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(episode_id(doc_example, 3), "20261004T001512Z-e0003")

    def test_legal_as_a_run_id_and_an_object_name(self):
        self.assertRegex(episode_id(T0, 17), r"^\d{8}T\d{6}Z-e\d{4}$")

    def test_unique_and_sortable_across_payload_restarts(self):
        # EPISODE restarts at 1 with the payload, so the number alone is no identity; the end time makes one.
        ends = [T0, T0 + 45_000, T0 + 3_600_000, T0 + 86_400_000]
        ids = [episode_id(t, 1) for t in ends]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(sorted(ids), ids)
        self.assertNotEqual(episode_id(T0, 1), episode_id(T0, 2))


class ConfTest(unittest.TestCase):
    """as_conf() is what sds_episode_watch hands each sds_forward run, and from_conf() what the run rebuilds."""
    CASES = [ClosedEpisode(2, 1791074807184, "PlayerDied", "died", 1791074773533),
             ClosedEpisode(4, T0 + 180_000, "EpisodeStarted", "reset", None),
             ClosedEpisode(3, T0 + 61_000, "EpisodeStarted", "reset", None, inferred=True),
             ClosedEpisode(5, T0 + 20_000, "EpisodeStarted", "interrupted", T0),
             ClosedEpisode(6, T0 + 40_000, "EpisodeStarted", "wad_switch", T0 + 21_000),
             ClosedEpisode(1, T0 + 90_000, "LevelFinished", "level_finished", T0 + 1)]

    def test_round_trip(self):
        for ep in self.CASES:
            with self.subTest(ep=ep):
                self.assertEqual(ClosedEpisode.from_conf(ep.as_conf()), ep)
                self.assertEqual(ClosedEpisode.from_conf(json.loads(json.dumps(ep.as_conf()))), ep)

    def test_conf_carries_id_and_utc_end(self):
        conf = self.CASES[0].as_conf()
        self.assertEqual(conf["episode_id"], "20261004T004647Z-e0002")
        self.assertEqual(conf["end_utc"], "2026-10-04T00:46:47.184Z")
        self.assertIs(self.CASES[2].as_conf()["inferred"], True)
        self.assertIsNone(self.CASES[1].as_conf()["start_event_ms"])

    def test_minimal_conf(self):
        ep = ClosedEpisode.from_conf({"number": "3", "end_ms": str(T0), "closing": "PlayerDied", "outcome": "died"})
        self.assertEqual(ep, ClosedEpisode(3, T0, "PlayerDied", "died", None, False))


class LocateTest(unittest.TestCase):
    # Three episodes as the archive holds them: status every 100 ms, TIC from the start each episode, the
    # payload's 2.3 s pause between a death and the next start.
    A = (T0, T0 + 20_000)
    B = (T0 + 22_300, T0 + 50_000)
    C = (T0 + 52_300, T0 + 60_000)

    def setUp(self):
        self.episode = run(*self.A, 1) + run(*self.B, 2) + run(*self.C, 3)
        self.tic = tics(*self.A, 7000) + tics(*self.B, 4) + tics(*self.C, 4)

    def test_window_of_a_death(self):
        self.assertEqual(locate(self.episode, self.tic, died(2, self.B[1])), self.B)
        self.assertEqual(locate(self.episode, self.tic, died(1, self.A[1])), self.A)

    def test_reset_closure_finds_the_run_that_already_ended(self):
        # A reset's closing event is the next episode's start, after the run's last sample.
        ep = ClosedEpisode(2, self.C[0], "EpisodeStarted", "reset", self.B[0])
        self.assertEqual(locate(self.episode, self.tic, ep), self.B)

    def test_final_status_may_trail_the_event_by_the_pad(self):
        self.assertEqual(locate(self.episode, self.tic, died(2, self.B[1] - END_PAD_MS)), self.B)
        # A run still going well after the closing event is not the episode that closed.
        self.assertIsNone(locate(self.episode, self.tic, died(2, self.B[1] - END_PAD_MS - 100)))

    def test_none_when_the_number_never_appears(self):
        self.assertIsNone(locate(self.episode, self.tic, died(7, self.C[1])))
        self.assertIsNone(locate([], [], died(1, T0)))

    def test_a_run_that_starts_after_the_closing_event_is_not_chosen(self):
        self.assertIsNone(locate(self.episode, self.tic, died(3, self.B[1])))
        # Not even one that also ends within the pad (EPISODE 1 again, TIC from the start: a new run).
        after = (self.A[1] + 500, self.A[1] + 1_000)
        episode = run(*self.A, 1) + run(*after, 1)
        tic = tics(*self.A, 7000) + tics(*after, 4)
        self.assertEqual(locate(episode, tic, died(1, self.A[1])), self.A)

    def test_last_matching_run_is_chosen(self):
        # The payload restarted after a long silence and EPISODE began again at 1.
        later = (self.C[1] + MAX_SILENCE_MS + 1_000, self.C[1] + MAX_SILENCE_MS + 30_000)
        episode = self.episode + run(*later, 1)
        tic = self.tic + tics(*later, 4)
        self.assertEqual(locate(episode, tic, died(1, later[1])), later)
        self.assertEqual(locate(episode, tic, died(1, self.A[1])), self.A)

    def test_a_long_silence_splits_a_run(self):
        first = (T0, T0 + 10_000)
        with self.subTest("exactly MAX_SILENCE_MS keeps one run"):
            second = (first[1] + MAX_SILENCE_MS, first[1] + MAX_SILENCE_MS + 10_000)
            self.assertEqual(locate(run(*first, 1) + run(*second, 1), [], died(1, second[1])), (first[0], second[1]))
        with self.subTest("longer splits it"):
            second = (first[1] + MAX_SILENCE_MS + 1, first[1] + MAX_SILENCE_MS + 10_001)
            self.assertEqual(locate(run(*first, 1) + run(*second, 1), [], died(1, second[1])), second)

    def test_tic_going_backwards_splits_a_run_with_the_same_number(self):
        # This repo's first flight: the payload restarted within seconds and episode 1 was followed by
        # episode 1. Only TIC starting again tells the two apart.
        first, second = (T0, T0 + 20_000), (T0 + 25_000, T0 + 40_000)
        episode = run(*first, 1) + run(*second, 1)
        tic = tics(*first, 7000) + tics(*second, 4)
        self.assertEqual(locate(episode, tic, died(1, second[1])), second)
        self.assertEqual(locate(episode, tic, died(1, first[1])), first)

    def test_a_repeated_tic_does_not_split_a_run(self):
        # The final, dead STATUS repeats the last TIC (in the recording, 1180 at both 00:46:47.133 and .183).
        # Only TIC going backwards is a restart; otherwise a death's window would be its last sample alone.
        episode = run(*self.B, 2)
        tic = tics(*self.B, 4)
        end = self.B[1] + 50
        episode.append((end, end - 950, 2))
        tic.append((end, end - 950, tic[-1][2]))
        self.assertEqual(locate(episode, tic, died(2, end)), (self.B[0], end))

    def test_short_dropout_with_tic_still_rising_keeps_one_run(self):
        # A few seconds of lost telemetry inside an episode must not cut its window.
        first, second = (T0, T0 + 20_000), (T0 + 25_000, T0 + 40_000)
        tic = tics(*first, 4) + tics(*second, 4 + 3 * 250)
        self.assertEqual(locate(run(*first, 2) + run(*second, 2), tic, died(2, second[1])), (first[0], second[1]))


class RecordedFlightTest(unittest.TestCase):
    """Two deaths recorded from the live archive over [00:46:09Z, 00:46:52Z) on 2026-10-04."""

    @classmethod
    def setUpClass(cls):
        cls.arch = archive.RecordedArchive(FIXTURE)
        cls.start, cls.stop = 1791074769000, 1791074812000
        cls.events = cls.arch.events(cls.start, cls.stop, types=[config.EVENT_PREFIX + n for n in config.EPISODE_EVENTS])
        s = cls.arch.parameters(["EPISODE", "TIC"], cls.start, cls.stop)
        cls.episode, cls.tic = s["EPISODE"], s["TIC"]

    def test_two_deaths_close_two_episodes(self):
        # Episode 3 started at 00:46:49.483Z and is still running at the end: not closed.
        self.assertEqual(closed_episodes(self.events, scan_start_ms=self.start), [
            ClosedEpisode(1, 1791074771233, "PlayerDied", "died", None, False),
            ClosedEpisode(2, 1791074807184, "PlayerDied", "died", 1791074773533, False)])
        self.assertEqual([e.episode_id for e in closed_episodes(self.events)],
                         ["20261004T004611Z-e0001", "20261004T004647Z-e0002"])

    def test_other_events_change_nothing(self):
        # The archive also holds about 170 IntentSet events in this window.
        everything = self.arch.events(self.start, self.stop)
        self.assertGreater(len(everything), len(self.events))
        self.assertEqual(closed_episodes(everything, scan_start_ms=self.start), closed_episodes(self.events))

    def test_episode_two_window_matches_the_live_run(self):
        ep2 = closed_episodes(self.events)[1]
        window = locate(self.episode, self.tic, ep2)
        self.assertEqual(window, (1791074773533, 1791074807183))
        self.assertEqual(window[1] - window[0], 33_650)
        self.assertEqual(window[0], ep2.start_event_ms)
        # The window's last TIC is the tic the PlayerDied event reports.
        died_event = [e for e in self.events if e["t"] == ep2.end_ms][0]
        self.assertEqual(dict((t, v) for t, _, v in self.tic)[window[1]], int(died_event["extra"]["tic"]))

    def test_episode_one_is_truncated_at_the_recording_start(self):
        ep1 = closed_episodes(self.events)[0]
        window = locate(self.episode, self.tic, ep1)
        self.assertEqual(window, (self.episode[0][0], 1791074771233))
        self.assertGreaterEqual(window[0], self.start)
        self.assertEqual(dict((t, v) for t, _, v in self.tic)[window[1]], 7788)

    def test_window_survives_the_conf_round_trip(self):
        for ep in closed_episodes(self.events):
            self.assertEqual(locate(self.episode, self.tic, ClosedEpisode.from_conf(ep.as_conf())),
                             locate(self.episode, self.tic, ep))


if __name__ == "__main__":
    unittest.main()


class PayloadRestartTest(unittest.TestCase):
    """A payload restart (scripts/flight.sh payload) ends the episode in progress without a death or an exit, and
    when the new payload's first episode has the number the old one had (1 -> 1), F' logs no EpisodeStarted at all.
    Its PayloadConnected is then the only trace: without it the old episode was never cataloged, and the new
    episode's death was dropped as a repeat of the old one's."""

    def connected(self, t):
        return {"t": t, "type": config.EVENT_PREFIX + "PayloadConnected", "extra": {}, "seq": None,
                "source": config.EVENT_SOURCE}

    def test_a_restart_during_episode_one_closes_it_and_the_next_death_counts(self):
        out = closed_episodes([ev(1_000, "EpisodeStarted", 1), self.connected(300_000),
                               ev(360_000, "PlayerDied", 1, tic=2_000)])
        self.assertEqual([(e.number, e.closing, e.outcome, e.inferred) for e in out],
                         [(1, "PayloadConnected", "interrupted", True), (1, "PlayerDied", "died", False)])
        self.assertEqual(out[0].start_event_ms, 1_000)
        self.assertIsNone(out[1].start_event_ms)       # the old start does not belong to the new episode
        self.assertNotEqual(out[0].episode_id, out[1].episode_id)

    def test_a_restart_in_the_pause_after_a_death(self):
        out = closed_episodes([ev(1_000, "EpisodeStarted", 1), ev(60_000, "PlayerDied", 1, tic=900),
                               self.connected(61_000), ev(90_000, "PlayerDied", 1, tic=800)])
        self.assertEqual([(e.number, e.end_ms) for e in out], [(1, 60_000), (1, 90_000)])

    def test_a_reconnect_with_nothing_open_closes_nothing(self):
        self.assertEqual(closed_episodes([self.connected(5_000), ev(9_000, "EpisodeStarted", 1)]), [])

    def test_the_watch_asks_for_payload_connected(self):
        self.assertIn("PayloadConnected", config.EPISODE_EVENTS)


class WadSwitchTest(unittest.TestCase):
    """On main the payload can switch WAD in flight (LOAD_WAD). It rebuilds the game and starts the next episode, so
    the episode in progress ends with no PlayerDied or LevelFinished, and F' logs WadLoaded just before the next
    EpisodeStarted (Doom.cpp handleWad). Without WadLoaded the episode reads as a RESET_GAME. With it the outcome is
    "wad_switch", and nothing else about the closure moves: the id is the closing time, and an episode cataloged as
    a reset before this rule must not come back under another id and be processed twice."""

    def loaded(self, t, seq=None, name="freedoom2.wad", map_="MAP01"):
        return {"t": t, "type": config.EVENT_PREFIX + "WadLoaded", "extra": {"name": name, "map": map_}, "seq": seq,
                "source": config.EVENT_SOURCE}

    def test_a_switch_relabels_the_reset_and_keeps_its_id(self):
        plain = [ev(T0, "EpisodeStarted", 4), ev(T0 + 30_100, "EpisodeStarted", 5)]
        switched = plain + [self.loaded(T0 + 30_000)]
        [reset], [switch] = closed_episodes(plain), closed_episodes(switched)
        self.assertEqual(reset, ClosedEpisode(4, T0 + 30_100, "EpisodeStarted", "reset", T0))
        self.assertEqual(switch, ClosedEpisode(4, T0 + 30_100, "EpisodeStarted", "wad_switch", T0))
        self.assertEqual(switch.episode_id, reset.episode_id)
        self.assertEqual((switch.closing, switch.end_ms, switch.start_event_ms, switch.inferred),
                         (reset.closing, reset.end_ms, reset.start_event_ms, reset.inferred))

    def test_a_shared_time_tag_counts_in_either_order(self):
        # F' time tags are coarse: WadLoaded and the EpisodeStarted after it can carry the same one, and the
        # archive's sequence numbers then decide the order. The WadLoaded belongs to the switch that started episode 5,
        # so episode 5, reset 4 s later (inside WAD_SWITCH_MS of it), is not a switch.
        t = T0 + 30_000
        for seqs in ((1, 2), (2, 1)):
            with self.subTest(seqs=seqs):
                events = [ev(T0, "EpisodeStarted", 4, seq=0), self.loaded(t, seq=seqs[0]),
                          ev(t, "EpisodeStarted", 5, seq=seqs[1]), ev(t + 4_000, "EpisodeStarted", 6, seq=3)]
                self.assertEqual([(e.number, e.outcome) for e in closed_episodes(events)],
                                 [(4, "wad_switch"), (5, "reset")])

    def test_a_switch_long_before_the_next_start_is_not_its_cause(self):
        events = [ev(T0, "EpisodeStarted", 4), self.loaded(T0 + 10_000),
                  ev(T0 + 10_001 + WAD_SWITCH_MS, "EpisodeStarted", 5)]
        self.assertEqual(closed_episodes(events)[0].outcome, "reset")
        events[-1] = ev(T0 + 10_000 + WAD_SWITCH_MS, "EpisodeStarted", 5)
        self.assertEqual(closed_episodes(events)[0].outcome, "wad_switch")

    def test_the_switch_that_started_an_episode_does_not_end_it(self):
        # WadLoaded(t) then EpisodeStarted(5) at t: episode 5 began with the switch. RESET_GAME ends it 4 s later,
        # within WAD_SWITCH_MS of that WadLoaded, and it is still a reset.
        t = T0 + 30_000
        events = [ev(T0, "EpisodeStarted", 4), self.loaded(t - 100), ev(t, "EpisodeStarted", 5),
                  ev(t + 4_000, "EpisodeStarted", 6)]
        self.assertEqual([(e.number, e.outcome) for e in closed_episodes(events)], [(4, "wad_switch"), (5, "reset")])

    def test_a_flights_first_episode_ended_by_a_switch(self):
        # The first episode's start is never archived, so its closure is the inferred one; a demonstration's first
        # LOAD_WAD ends exactly that episode.
        S = T0 - 3_600_000
        t = S + INFER_AFTER_MS + 5_000
        out = closed_episodes([self.loaded(t - 200), ev(t, "EpisodeStarted", 2)], scan_start_ms=S)
        self.assertEqual(out, [ClosedEpisode(1, t, "EpisodeStarted", "wad_switch", None, inferred=True)])
        with self.subTest("a WadLoaded before a payload restart belongs to the old payload"):
            connected = {"t": t - 100, "type": config.EVENT_PREFIX + "PayloadConnected", "extra": {}, "seq": None,
                         "source": config.EVENT_SOURCE}
            out = closed_episodes([self.loaded(t - 200), connected, ev(t, "EpisodeStarted", 2)], scan_start_ms=S)
            self.assertEqual([e.outcome for e in out], ["reset"])

    def test_only_a_reset_becomes_a_switch(self):
        with self.subTest("a death before the switch keeps the death, once"):
            events = [ev(T0, "EpisodeStarted", 4), ev(T0 + 20_000, "PlayerDied", 4, tic=700),
                      self.loaded(T0 + 21_000), ev(T0 + 21_100, "EpisodeStarted", 5)]
            self.assertEqual(closed_episodes(events), [ClosedEpisode(4, T0 + 20_000, "PlayerDied", "died", T0)])
        with self.subTest("a number that does not follow on is still a restart"):
            events = [ev(T0, "EpisodeStarted", 4), self.loaded(T0 + 21_000), ev(T0 + 21_100, "EpisodeStarted", 1)]
            self.assertEqual(closed_episodes(events)[0].outcome, "interrupted")

    def test_the_watch_asks_for_wad_loaded(self):
        self.assertIn("WadLoaded", config.EPISODE_EVENTS)
