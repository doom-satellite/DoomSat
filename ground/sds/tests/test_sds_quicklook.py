"""The quicklook: what a person on console reads at a glance every minute (doomsat_sds.quicklook).

Frames are never archived, so the capture service's per-minute counts and the JPEGs it keeps are the only account
of how the frame link is doing, and ql_health is where that account is judged. A wrong verdict costs either way: a
false GO hides a payload that has stopped talking, a false NO-GO sends someone chasing a healthy link. Protected here:

- last_minutes sums only the N closed minutes before the current one (the current minute is still being counted);
  a row the capture service wrote while shutting down (partial_minute) covers an unfinished minute and stays out; a
  torn line from a crash mid-write is skipped, not fatal; completeness is complete/(complete+incomplete+missing), and
  None (not 0, not a ZeroDivisionError) when no frame was expected; the window crosses midnight and the year end.
- health: payload GO needs PAYLOAD_LINK True and fresher than 10 s (a True that stopped updating is NO-GO); capture GO
  needs a chunk within 10 s; downlink and uplink are the Yamcs link status "OK"; Yamcs is NO-GO when no realtime value
  came back; go means every verdict is GO; the yield is whole images received over FRAMES_SENT's rise, None without one.
- latest_frames: the newest n JPEGs of the last two hour directories, oldest first, found where the capture service
  writes them and by the names it gives them.
- contact_sheet (Pillow, which only the SDS venv has): a PNG cols*160 wide with one 120-pixel row per cols frames under
  the banner, each thumbnail where its index puts it, an undecodable frame left blank, the banner's boxes coloured by
  the verdicts, and a sheet that still renders when everything is down, which is when it is needed most.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import archive, capture, config, pipeline  # noqa: E402
from doomsat_sds import quicklook as ql  # noqa: E402
from doomsat_sds.frames import MAP_BIT  # noqa: E402

try:
    import PIL.Image
    HAVE_PIL = True
except ImportError:          # ground/.venv has no Pillow; the SDS venv does
    HAVE_PIL = False

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
EXAMPLE_MS = 1791075697283                     # 2026-10-04T01:01:37.283Z
EXAMPLE_NAME = "20261004T010137.283Z-00008275.jpg"
WINDOW = "last_%d_min" % ql.WINDOW_MIN
SENT = "frames_sent_on_board_last_%d_min" % ql.WINDOW_MIN
YIELD = "frame_yield_last_%d_min" % ql.WINDOW_MIN
THUMB_W, THUMB_H = 160, 120
BACKGROUND = (16, 20, 24)


def stats_row(minute, partial=False, **counts):
    """One line of capture/stats.jsonl as Capture._close_minute writes it."""
    row = dict(counts, minute=minute, subscriptions=1)
    if partial:
        row["partial_minute"] = True
    return row


def minutes_from(first, count):
    """yyyymmddThhmm keys, one a minute, starting at `first`."""
    import datetime as dt
    t = dt.datetime.strptime(first, "%Y%m%dT%H%M")
    return [(t + dt.timedelta(minutes=i)).strftime("%Y%m%dT%H%M") for i in range(count)]


def empty_window():
    w = {k: 0 for k in ql.COUNT_KEYS}
    w.update(frame_completeness=None, minutes=[])
    return w


def good_inputs():
    """Everything healthy: the shape pipeline.quicklook hands health()."""
    window = empty_window()
    window.update(frames_complete=290, frames_incomplete=4, frames_missing=6, maps_complete=10,
                  frame_completeness=0.9667,
                  minutes=minutes_from("20261004T0056", 5))
    status = {"seconds_since_last_chunk": 1.2, "subscriptions": 1, "in_flight": 0}
    realtime = {"PAYLOAD_LINK": (True, 0.8), "EPISODE": (3, 0.8), "HEALTH": (100, 0.8), "KILLS": (0, 0.8),
                "EXPLORED_CELLS": (20, 0.8), "TIC": (94, 0.8)}
    links = {"UDP_TM_IN": {"status": "OK", "in": 23599, "out": 0}, "UDP_TC_OUT": {"status": "OK", "in": 0, "out": 961}}
    return {"now_utc": "20261004T010137Z", "capture_status": status, "window": window, "realtime": realtime,
            "links": links, "frames_sent_window": 310}


def health(**changes):
    args = good_inputs()
    args.update(changes)
    return ql.health(**args)


def frames_sent_series(restart_s=None):
    """FRAMES_SENT as arch.parameters returns it, (generation_ms, reception_ms, value), one sample a second over the
    five closed minutes 00:56-01:00 and 5 s either side, ten images a second, F´ time a second ahead of the ground.
    With restart_s, F´ goes down that many seconds into the window, is back 10 s later, and counts again from 0."""
    w0 = 1791075360000                                   # 2026-10-04T00:56:00Z
    w1 = w0 + 5 * 60_000
    out = []
    for s in range(-5, 306):
        if restart_s is None or s < restart_s:
            value = 4970 + 10 * s
        elif s < restart_s + 10:
            continue                                     # F´ is down: no samples
        else:
            value = 10 * (s - restart_s - 10)
        out.append((w0 + s * 1000 + 1000, w0 + s * 1000, value))
    return w0, w1, out


def chunk(seq, index, count, data):
    """A FrameChunk value as Yamcs hands it over: `data` always 960 bytes, the first `length` real."""
    return {"seq": seq, "index": index, "count": count, "length": len(data), "data": data + b"\0" * (960 - len(data))}


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = config.Settings(home=self.tmp / "sds", doomsat_home=self.tmp / "doom")
        self.stats = self.settings.capture / "stats.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def write_stats(self, rows, tail=""):
        self.stats.parent.mkdir(parents=True, exist_ok=True)
        lines = [r if isinstance(r, str) else json.dumps(r, sort_keys=True) for r in rows]
        self.stats.write_text("".join(line + "\n" for line in lines) + tail, encoding="utf-8")


# ================================================================================================ last_minutes
class LastMinutes(TempHome):
    def test_sums_only_the_closed_minutes_before_now(self):
        # Powers of two, so the sum says exactly which minutes were counted.
        keys = minutes_from("20261004T0054", 8)             # 00:54 .. 01:01
        self.write_stats([stats_row(m, frames_complete=2 ** j) for j, m in enumerate(keys)])
        w = ql.last_minutes(self.stats, "20261004T0101")
        self.assertEqual(w["minutes"], keys[2:7])           # 00:56 .. 01:00; not 01:01, still being counted
        self.assertEqual(w["frames_complete"], 4 + 8 + 16 + 32 + 64)
        self.assertEqual(ql.last_minutes(self.stats, "20261004T0101", minutes=2)["frames_complete"], 32 + 64)

    def test_a_minute_the_service_was_down_for_part_of_is_left_out(self):
        # The service stopped at 00:59:20 (a partial row) and came back at 00:59:40. Neither row covers the whole
        # minute, so 00:59 is not counted at all: counting it would compare 40 s of frames with 60 s sent on board.
        self.write_stats([stats_row("20261004T0058", partial=True, frames_complete=100),
                          stats_row("20261004T0059", partial=True, frames_complete=7),
                          stats_row("20261004T0059", frames_complete=3),
                          stats_row("20261004T0100", frames_complete=5)])
        w = ql.last_minutes(self.stats, "20261004T0101")
        self.assertEqual(w["frames_complete"], 5)
        self.assertEqual(w["minutes"], ["20261004T0100"])

    def test_torn_and_garbled_lines_are_skipped(self):
        self.write_stats(["", "not json at all", stats_row("20261004T0059", frames_complete=4, chunks=40),
                          '{"minute": "20261004T0100"}{"minute": "20261004T0100"}'],
                         tail='{"chunks": 9, "frames_complete": 9, "minute": "20261004T01')    # crash mid-write
        w = ql.last_minutes(self.stats, "20261004T0101")
        self.assertEqual((w["frames_complete"], w["chunks"], w["minutes"]), (4, 40, ["20261004T0059"]))

    def test_every_count_is_summed_and_unknown_fields_ignored(self):
        first = {k: i + 1 for i, k in enumerate(ql.COUNT_KEYS)}
        second = {k: 100 * (i + 1) for i, k in enumerate(ql.COUNT_KEYS)}
        self.write_stats([stats_row("20261004T0059", frame_seq_restarts=3, **first),
                          stats_row("20261004T0100", **second)])
        w = ql.last_minutes(self.stats, "20261004T0101")
        for i, k in enumerate(ql.COUNT_KEYS):
            self.assertEqual(w[k], 101 * (i + 1), k)
        self.assertNotIn("frame_seq_restarts", w)
        self.assertNotIn("subscriptions", w)

    def test_completeness_is_complete_over_complete_incomplete_and_missing(self):
        # Cut frames are the ground's doing (it subscribed mid-image) and maps are counted apart: neither is in it.
        self.write_stats([stats_row("20261004T0100", frames_complete=6, frames_incomplete=1, frames_missing=1,
                                    frames_cut=5, maps_complete=1, maps_incomplete=9, maps_missing=9)])
        self.assertEqual(ql.last_minutes(self.stats, "20261004T0101")["frame_completeness"], 0.75)
        self.write_stats([stats_row("20261004T0100", frames_complete=2, frames_incomplete=1)])
        self.assertEqual(ql.last_minutes(self.stats, "20261004T0101")["frame_completeness"], 0.6667)
        self.write_stats([stats_row("20261004T0100", frames_missing=3)])
        self.assertEqual(ql.last_minutes(self.stats, "20261004T0101")["frame_completeness"], 0.0)

    def test_completeness_is_none_when_no_frame_was_expected(self):
        self.write_stats([stats_row("20261004T0100", frames_cut=2, maps_complete=4, chunks=12)])
        w = ql.last_minutes(self.stats, "20261004T0101")
        self.assertIsNone(w["frame_completeness"])
        self.assertEqual(w["minutes"], ["20261004T0100"])
        nothing = ql.last_minutes(self.tmp / "no" / "stats.jsonl", "20261004T0101")    # the service never ran
        self.assertIsNone(nothing["frame_completeness"])
        self.assertEqual(nothing["minutes"], [])
        self.assertEqual({k: nothing[k] for k in ql.COUNT_KEYS}, {k: 0 for k in ql.COUNT_KEYS})

    def test_the_window_crosses_midnight_and_the_year_end(self):
        keys = minutes_from("20261231T2356", 7)             # 23:56 .. 00:02 the next year
        self.assertEqual(keys[-1], "20270101T0002")
        self.write_stats([stats_row(m, frames_complete=2 ** j) for j, m in enumerate(keys)])
        w = ql.last_minutes(self.stats, "20270101T0002")
        self.assertEqual(w["minutes"], keys[1:6])
        self.assertEqual(w["frames_complete"], 2 + 4 + 8 + 16 + 32)

    def test_a_long_history_before_the_window_does_not_hide_it(self):
        keys = minutes_from("20261003T2200", 181)           # three hours of rows, the last at 01:00
        self.write_stats([stats_row(m, frames_complete=1, chunks=10) for m in keys])
        w = ql.last_minutes(self.stats, "20261004T0101")
        self.assertEqual((w["frames_complete"], w["chunks"]), (ql.WINDOW_MIN, 10 * ql.WINDOW_MIN))
        self.assertEqual(w["minutes"], keys[-ql.WINDOW_MIN:])


# ================================================================================================ capture -> quicklook
class WhatTheCaptureServiceWritesIsWhatTheQuicklookReads(TempHome):
    """The contract between capture.py and quicklook.py: stats keys, file names and the status file."""

    def setUp(self):
        super().setUp()
        self.cap = capture.Capture(self.settings)
        self.cap.first_minute = False          # as if it had been running before 01:00
        a = self.cap.assembler
        self.cap.minute = "20261004T0100"
        a.subscribed(100.0)
        sent = [(5, 1, 2, b"c" * 500, 100.5),                        # began before we subscribed: cut
                (6, 0, 1, b"\xff\xd8 frame six \xff\xd9", 100.6),     # whole
                (9, 0, 2, b"n" * 960, 100.7),                        # 7 and 8 never seen; 9 never finished
                (MAP_BIT | 1, 0, 1, b"\x89PNG map one", 100.8),       # a whole map
                (MAP_BIT | 3, 0, 2, b"m" * 300, 100.9)]               # map 2 never seen; map 3 never finished
        for seq, index, count, data, now in sent:
            a.add(chunk(seq, index, count, data), EXAMPLE_MS + int((now - 100.6) * 1000), now)
        a.expire(110.0)
        a.add(chunk(9, 1, 2, b"late"), EXAMPLE_MS + 9500, 110.1)            # after 9 was given up: late
        a.add(chunk(6, 0, 1, b"\xff\xd8 frame six \xff\xd9"), EXAMPLE_MS + 9600, 110.2)   # again: duplicate
        self.bytes = sum(len(s[3]) for s in sent) + len(b"late") + len(b"\xff\xd8 frame six \xff\xd9")
        self.cap._close_minute()
        # 01:01: one more frame, then the service stops part-way through the minute.
        self.cap.minute = "20261004T0101"
        a.add(chunk(10, 0, 1, b"\xff\xd8 ten \xff\xd9"), EXAMPLE_MS + 30000, 111.0)
        self.cap.last_chunk_mono = time.monotonic()      # what on_data records with each chunk
        self.cap.shutdown()

    def test_every_count_the_quicklook_sums_is_one_the_service_writes(self):
        w = ql.last_minutes(self.stats, "20261004T0102")
        self.assertEqual(w["minutes"], ["20261004T0100"])                    # 01:01 was cut short
        expected = {"frames_complete": 1, "frames_incomplete": 1, "frames_missing": 2, "frames_cut": 1,
                    "frame_duplicate_chunks": 1, "frame_late_chunks": 1, "maps_complete": 1, "maps_incomplete": 1,
                    "maps_missing": 1, "chunks": 7, "bytes": self.bytes}
        self.assertEqual(set(expected), set(ql.COUNT_KEYS))
        self.assertEqual({k: w[k] for k in ql.COUNT_KEYS}, expected)
        self.assertEqual(w["frame_completeness"], 0.25)

    def test_latest_frames_finds_the_jpegs_by_the_names_the_service_gives_them(self):
        found = ql.latest_frames(self.settings.capture)
        self.assertEqual([f.name for f in found], ["20261004T010137.283Z-00000006.jpg",
                                                   "20261004T010207.283Z-00000010.jpg"])
        self.assertEqual(found[0].parent, self.settings.capture / "frames" / "20261004" / "01")
        self.assertEqual(found[0].read_bytes(), b"\xff\xd8 frame six \xff\xd9")
        self.assertTrue(list((self.settings.capture / "maps").rglob("*.png")), "the map went to maps/")

    def test_a_service_that_has_received_nothing_says_so(self):
        fresh = capture.Capture(config.Settings(home=self.tmp / "fresh", doomsat_home=self.tmp))
        fresh._write_status(EXAMPLE_MS)
        status = json.loads((fresh.root / "status.json").read_text())
        self.assertIsNone(status["seconds_since_last_chunk"])    # not 0: nothing has ever arrived
        self.assertEqual(health(capture_status=status)["verdicts"]["capture"], "NO-GO")

    def test_the_minute_the_service_started_in_is_not_a_whole_minute(self):
        fresh = capture.Capture(config.Settings(home=self.tmp / "fresh", doomsat_home=self.tmp))
        fresh.minute = "20261004T0105"
        fresh._close_minute()
        fresh.minute = "20261004T0106"
        fresh._close_minute()
        w = ql.last_minutes(fresh.root / "stats.jsonl", "20261004T0107")
        self.assertEqual(w["minutes"], ["20261004T0106"])

    def test_the_status_file_feeds_the_capture_verdict(self):
        status = json.loads((self.settings.capture / "status.json").read_text())
        h = health(capture_status=status)
        self.assertLess(status["seconds_since_last_chunk"], 10)
        self.assertEqual(h["verdicts"]["capture"], "GO")
        self.assertEqual(h["capture"]["subscriptions"], status["subscriptions"])
        self.assertEqual(h["capture"]["in_flight"], 0)


# ================================================================================================ health
class Health(unittest.TestCase):
    def test_all_go(self):
        h = health()
        self.assertEqual(h["verdicts"], {"yamcs": "GO", "payload": "GO", "downlink": "GO", "uplink": "GO",
                                         "capture": "GO"})
        self.assertIs(h["go"], True)
        self.assertEqual(h[YIELD], round(300 / 310, 4))
        self.assertEqual(h["product"], {"type": "ql_health", "level": "QL"})

    def test_payload_needs_link_true_and_fresh(self):
        cases = [((True, 9.9), "GO"), ((True, 10.0), "NO-GO"), ((True, 600.0), "NO-GO"), ((False, 0.5), "NO-GO"),
                 ((True, None), "NO-GO"), (("False", 0.5), "NO-GO")]       # a truthy string is not True
        for value, verdict in cases:
            realtime = dict(good_inputs()["realtime"], PAYLOAD_LINK=value)
            h = health(realtime=realtime)
            self.assertEqual(h["verdicts"]["payload"], verdict, value)
            self.assertEqual(h["go"], verdict == "GO", value)
            self.assertEqual(h["verdicts"]["yamcs"], "GO")
        realtime = {k: v for k, v in good_inputs()["realtime"].items() if k != "PAYLOAD_LINK"}
        h = health(realtime=realtime)
        self.assertEqual(h["verdicts"]["payload"], "NO-GO")
        self.assertIsNone(h["telemetry_age_s"])

    def test_capture_needs_a_chunk_within_ten_seconds(self):
        since = lambda s: {"seconds_since_last_chunk": s}
        for status, verdict in [(since(9.9), "GO"), (since(10.0), "NO-GO"), (since(3600.0), "NO-GO"), ({}, "NO-GO"),
                                (None, "NO-GO")]:
            h = health(capture_status=status)
            self.assertEqual(h["verdicts"]["capture"], verdict, status)
            self.assertEqual(h["go"], verdict == "GO")
        h = health(capture_status=None)                     # the service never wrote status.json
        self.assertEqual(h["capture"], {"seconds_since_last_chunk": None, "subscriptions": None, "in_flight": None,
                                        "status_age_s": None})

    def test_a_status_file_left_behind_by_a_dead_service_is_no_go(self):
        status = {"seconds_since_last_chunk": 0.4}
        self.assertEqual(health(capture_status=status, status_age_s=10.0)["verdicts"]["capture"], "GO")
        h = health(capture_status=status, status_age_s=ql.STATUS_STALE_S + 1)
        self.assertEqual(h["verdicts"]["capture"], "NO-GO")
        self.assertEqual(h["capture"]["status_age_s"], ql.STATUS_STALE_S + 1)

    def test_downlink_and_uplink_are_the_link_status(self):
        links = good_inputs()["links"]
        h = health(links=dict(links, UDP_TM_IN=dict(links["UDP_TM_IN"], status="UNAVAIL")))
        self.assertEqual((h["verdicts"]["downlink"], h["verdicts"]["uplink"]), ("NO-GO", "GO"))
        h = health(links=dict(links, UDP_TC_OUT=dict(links["UDP_TC_OUT"], status="DISABLED")))
        self.assertEqual((h["verdicts"]["downlink"], h["verdicts"]["uplink"]), ("GO", "NO-GO"))
        h = health(links={})
        self.assertEqual((h["verdicts"]["downlink"], h["verdicts"]["uplink"]), ("NO-GO", "NO-GO"))
        self.assertFalse(h["go"])

    def test_yamcs_is_no_go_when_no_realtime_value_came_back(self):
        h = health(realtime={})
        self.assertEqual(h["verdicts"]["yamcs"], "NO-GO")
        self.assertEqual(h["verdicts"]["payload"], "NO-GO")
        self.assertEqual(h["payload"], dict.fromkeys(("EPISODE", "HEALTH", "KILLS", "EXPLORED_CELLS", "TIC",
                                                      "PAYLOAD_LINK")))
        self.assertFalse(h["go"])

    def test_the_yield_is_whole_images_over_frames_sent(self):
        window = dict(good_inputs()["window"], frames_complete=270, maps_complete=30)
        self.assertEqual(health(window=window, frames_sent_window=300)[YIELD], 1.0)      # frames and maps both count
        self.assertEqual(health(window=window, frames_sent_window=400)[YIELD], 0.75)
        self.assertIsNone(health(frames_sent_window=None)[YIELD])                         # no FRAMES_SENT window
        self.assertIsNone(health(frames_sent_window=0)[YIELD])

    def test_the_yield_from_a_steady_on_board_counter(self):
        # The way pipeline.quicklook gets frames_sent_window: FRAMES_SENT samples read across the five closed minutes.
        w0, w1, series = frames_sent_series()
        sent = pipeline.frames_sent_between(series, w0, w1)
        self.assertEqual(sent, 3000)
        window = dict(good_inputs()["window"], frames_complete=2880, maps_complete=60)
        h = health(window=window, frames_sent_window=sent)
        self.assertEqual((h[SENT], h[YIELD]), (3000, 0.98))

    # Regression test for a bug the tests found (now fixed): pipeline.py:262 (frames_sent_between returns b - a with no check) feeding quicklook.py:75-77 (health
    # divides by any non-zero frames_sent_window and reports it as frames sent). FRAMES_SENT is a U32 counter in the
    # F´ Doom component (Doom.cpp: m_framesSent(0) in the constructor, ++ per image) that restarts at 0 with the
    # binary. `scripts/flight.sh start` restarts F´ while the SDS keeps running and the Yamcs archive keeps the old
    # samples, so for the five minutes after a flight restart the window straddles the restart and ql_health reports
    # "frames sent on board -3270" and a yield of -0.0917. A counter that went backwards is not a rise: the number
    # should be None (or the rise summed across the restart), never negative. Either fix makes this pass:
    # frames_sent_between declining a window where the counter stepped back, or health treating a negative window
    # as no window (for both numbers it reports).
    def test_a_flight_restart_inside_the_window_never_gives_a_negative_count_or_yield(self):
        w0, w1, series = frames_sent_series(restart_s=120)
        h = health(frames_sent_window=pipeline.frames_sent_between(series, w0, w1))
        self.assertTrue(h[SENT] is None or h[SENT] >= 0, "frames sent on board: %s" % h[SENT])
        self.assertTrue(h[YIELD] is None or h[YIELD] >= 0, "yield: %s" % h[YIELD])

    def test_the_numbers_behind_the_verdicts_are_carried(self):
        args = good_inputs()
        h = ql.health(**args)
        self.assertEqual(h["utc"], args["now_utc"])
        self.assertEqual(h[WINDOW], args["window"])
        self.assertEqual(h[SENT], 310)
        self.assertEqual(h["links"], args["links"])
        self.assertEqual(h["telemetry_age_s"], 0.8)
        self.assertEqual(h["payload"], {"EPISODE": 3, "HEALTH": 100, "KILLS": 0, "EXPLORED_CELLS": 20, "TIC": 94,
                                        "PAYLOAD_LINK": True})
        self.assertEqual(h["capture"]["seconds_since_last_chunk"], 1.2)
        json.dumps(h, allow_nan=False)                  # it is written with canonical_json

    def test_realtime_values_as_the_archive_gives_them(self):
        """PAYLOAD_LINK reaches Yamcs as the string "True" (an XTCE enumeration); archive.value() makes it a bool,
        which is what health's `is True` needs. Built here the way pipeline.quicklook builds it, from the recording."""
        rec = archive.RecordedArchive(FIXTURE)
        names = ["PAYLOAD_LINK", "EPISODE", "HEALTH", "KILLS", "EXPLORED_CELLS", "TIC"]
        series = rec.parameters(names, rec.data["start_ms"], rec.data["stop_ms"])
        last = {n: series[n][-1] for n in names}
        self.assertIs(last["PAYLOAD_LINK"][2], True)
        self.assertIs(archive.value("True"), True)

        def realtime(asked_ms):
            return {n: (v, round((asked_ms - r) / 1000, 1)) for n, (_, r, v) in last.items()}

        fresh = max(r for _, r, _ in last.values()) + 1500
        h = health(realtime=realtime(fresh))
        self.assertEqual(h["verdicts"]["payload"], "GO")
        self.assertEqual(h["telemetry_age_s"], 1.5)
        self.assertEqual(h["payload"]["EPISODE"], 3)          # the episode that began at 00:46:49.483Z
        self.assertEqual(h["payload"]["TIC"], last["TIC"][2])
        stale = health(realtime=realtime(rec.data["stop_ms"] + 60_000))       # the link froze a minute ago
        self.assertEqual(stale["verdicts"]["payload"], "NO-GO")
        self.assertEqual(stale["verdicts"]["yamcs"], "GO")


# ================================================================================================ latest_frames
class LatestFrames(TempHome):
    def put(self, *names, data=b"jpeg"):
        out = []
        for name in names:
            p = self.settings.capture / "frames" / name[:8] / name[9:11] / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            out.append(p)
        return out

    def test_the_newest_n_of_the_last_two_hours_in_time_order(self):
        old = self.put("20261004T000110.000Z-00000001.jpg", "20261004T005959.900Z-00000002.jpg")
        mid = self.put("20261004T010000.100Z-00000003.jpg", "20261004T010137.283Z-00008275.jpg",
                       "20261004T015959.000Z-00008276.jpg")
        new = self.put("20261004T020000.000Z-00008277.jpg", "20261004T020001.000Z-00008278.jpg")
        self.assertEqual(ql.latest_frames(self.settings.capture), mid + new)        # 00h is the third hour back
        self.assertEqual(ql.latest_frames(self.settings.capture, n=3), mid[2:] + new)
        self.assertEqual(ql.latest_frames(self.settings.capture, n=1), new[1:])
        self.assertTrue(all(p.exists() for p in old))

    def test_the_two_hours_may_straddle_midnight(self):
        late = self.put("20261004T235959.500Z-00000010.jpg")
        early = self.put("20261005T000000.500Z-00000011.jpg")
        self.assertEqual(ql.latest_frames(self.settings.capture), late + early)

    def test_time_order_survives_a_payload_restart(self):
        # The sequence number restarts with the payload; the name starts with the time, so time order wins.
        frames = self.put("20261004T010100.000Z-00009999.jpg", "20261004T010130.000Z-00000001.jpg")
        self.assertEqual(ql.latest_frames(self.settings.capture), frames)

    def test_only_finished_jpegs_count(self):
        frames = self.put("20261004T005959.000Z-00008274.jpg", "20261004T010137.283Z-00008275.jpg")
        hour = frames[1].parent
        (hour / ".20261004T010137.383Z-00008276.jpg.k3j4h5").write_bytes(b"half")    # write_atomic's temp file
        (hour / "20261004T010137.483Z-00008277.png").write_bytes(b"png")
        # Maps have hour directories of their own, newer here than any frame's: they must not count as one of the
        # last two hours, or the 00h frames would be crowded out.
        for h in ("01", "02"):
            maps = self.settings.capture / "maps" / "20261004" / h
            maps.mkdir(parents=True)
            (maps / ("20261004T%s0137.583Z-00000001.png" % h)).write_bytes(b"map")
            (maps / ("20261004T%s0137.683Z-00000002.jpg" % h)).write_bytes(b"not a frame")
        (self.settings.capture / "frames" / "20261004" / "notes").mkdir()
        (self.settings.capture / "frames" / "20261004" / "notes" / "x.jpg").write_bytes(b"x")
        self.assertEqual(ql.latest_frames(self.settings.capture), frames)

    def test_nothing_captured_yet(self):
        self.assertEqual(ql.latest_frames(self.settings.capture), [])
        (self.settings.capture / "frames" / "20261004" / "01").mkdir(parents=True)
        self.assertEqual(ql.latest_frames(self.settings.capture), [])


# ================================================================================================ contact_sheet
COLOURS = [(220, 30, 30), (30, 200, 40), (40, 60, 230), (230, 220, 30), (200, 40, 210), (30, 210, 210),
           (240, 140, 20), (128, 128, 128), (250, 250, 250)]


@unittest.skipUnless(HAVE_PIL, "Pillow is in the SDS venv, not in ground/.venv")
class ContactSheet(TempHome):
    def setUp(self):
        super().setUp()
        self.cap = capture.Capture(self.settings)
        for i, colour in enumerate(COLOURS):
            buf = io.BytesIO()
            PIL.Image.new("RGB", (320, 240), colour).save(buf, format="JPEG", quality=95)
            self.cap._save("frame", 8275 + i, buf.getvalue(), EXAMPLE_MS + 100 * i)
        self.frames = ql.latest_frames(self.settings.capture)
        self.h = health()

    @staticmethod
    def image(png):
        return PIL.Image.open(io.BytesIO(png)).convert("RGB")

    def near(self, got, want, tol=16):
        return all(abs(a - b) <= tol for a, b in zip(got, want))

    def test_frames_are_named_as_the_capture_service_names_them(self):
        self.assertEqual(len(self.frames), len(COLOURS))
        self.assertEqual(self.frames[0].name, EXAMPLE_NAME)
        self.assertEqual(self.frames[0].relative_to(self.settings.capture).parts[:3], ("frames", "20261004", "01"))

    def test_a_png_whose_height_follows_the_frame_count(self):
        png = ql.contact_sheet(self.frames[:5], self.h)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(PIL.Image.open(io.BytesIO(png)).format, "PNG")
        sizes = {k: PIL.Image.open(io.BytesIO(ql.contact_sheet(self.frames[:k], self.h))).size
                 for k in (0, 1, 4, 5, 8, 9)}
        banner = sizes[1][1] - THUMB_H
        self.assertGreater(banner, 0)
        for k, (w, h) in sizes.items():
            rows = max(1, -(-k // 4))
            self.assertEqual((w, h), (4 * THUMB_W, banner + rows * THUMB_H), k)
        self.assertEqual(sizes[0], sizes[1])                # an empty sheet still has its one row and its banner
        w, h = self.image(ql.contact_sheet(self.frames[:5], self.h, cols=3)).size
        self.assertEqual((w, h), (3 * THUMB_W, banner + 2 * THUMB_H))

    def test_each_thumbnail_lands_where_its_index_puts_it(self):
        sheet = self.image(ql.contact_sheet(self.frames, self.h))
        banner = sheet.size[1] - 3 * THUMB_H
        for i, colour in enumerate(COLOURS):
            cx, cy = (i % 4) * THUMB_W, banner + (i // 4) * THUMB_H
            centre = sheet.getpixel((cx + THUMB_W // 2, cy + THUMB_H // 2))
            self.assertTrue(self.near(centre, colour), "slot %d: %s, want %s" % (i, centre, colour))
        # The unused slots of the last row stay background, and frame 0 carries its time and number in yellow.
        self.assertEqual(sheet.getpixel((3 * THUMB_W + 80, banner + 2 * THUMB_H + 60)), BACKGROUND)
        label = [sheet.getpixel((x, y)) for x in range(3, 120) for y in range(banner + 2, banner + 16)]
        self.assertTrue(any(r > 200 and g > 200 and b < 90 for r, g, b in label))

    def test_a_frame_that_will_not_decode_leaves_its_slot_blank(self):
        hour = self.frames[0].parent
        broken = hour / "20261004T010137.300Z-00008280.jpg"
        broken.write_bytes(b"\xff\xd8 truncated")
        frames = [self.frames[0], broken, self.frames[2]]
        sheet = self.image(ql.contact_sheet(frames, self.h))
        banner = sheet.size[1] - THUMB_H
        centre = lambda i: sheet.getpixel((i * THUMB_W + THUMB_W // 2, banner + THUMB_H // 2))
        self.assertTrue(self.near(centre(0), COLOURS[0]))
        self.assertEqual(centre(1), BACKGROUND)
        self.assertTrue(self.near(centre(2), COLOURS[2]))

    def test_the_banner_boxes_show_the_verdicts(self):
        realtime = dict(good_inputs()["realtime"], PAYLOAD_LINK=(False, 0.5))
        links = dict(good_inputs()["links"], UDP_TC_OUT={"status": "UNAVAIL", "in": 0, "out": 0})
        h = health(realtime=realtime, links=links)
        self.assertEqual(list(h["verdicts"].values()), ["GO", "NO-GO", "GO", "NO-GO", "GO"])
        sheet = self.image(ql.contact_sheet(self.frames[:4], h))

        def kind(px):
            r, g, b = px
            return "GO" if g > 150 and r < 120 else "NO-GO" if r > 180 and g < 120 else None

        runs = []
        for x in range(sheet.size[0]):                      # row 7: inside the boxes, above their text
            k = kind(sheet.getpixel((x, 7)))
            if k and (not runs or runs[-1][0] != k or runs[-1][1] != x - 1):
                runs.append([k, x])
            elif k:
                runs[-1][1] = x
        self.assertEqual([k for k, _ in runs], list(h["verdicts"].values()))

    def test_it_renders_when_everything_is_down(self):
        h = ql.health("20261004T010137Z", None, empty_window(), {}, {}, None)
        self.assertFalse(h["go"])
        sheet = self.image(ql.contact_sheet([], h))
        self.assertEqual(sheet.size[0], 4 * THUMB_W)

    def test_the_same_inputs_give_the_same_bytes(self):
        self.assertEqual(ql.contact_sheet(self.frames, self.h), ql.contact_sheet(self.frames, self.h))


if __name__ == "__main__":
    unittest.main()
