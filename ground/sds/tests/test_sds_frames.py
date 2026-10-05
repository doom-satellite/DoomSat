"""Frame reassembly and link accounting: doomsat_sds.frames.FrameAssembler.

Frames are never archived (the flight launcher marks FRAME_CHUNK realtime-only), so the capture service's live
reassembly is the only copy of what the payload saw, and its counts are the only measure of how the frame link
performed (the quicklook's completeness figure is built from them). The rules that matter: an image is the first
`length` bytes of each 960-byte chunk joined in index order; the payload's map PNGs share the channel with the top
bit of `seq` set and are counted apart; an image missing chunks is counted on a timer, so a stall at the end of a
flight is still seen; a straggler after that is late, not a new image; a payload restart is not thousands of
missing frames; and an image cut by our own (re)subscription is not blamed on the link.
"""
import json
import os
import sys
import unittest

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import frames, quicklook  # noqa: E402
from doomsat_sds.frames import MAP_BIT, RESTART_STEP, FrameAssembler  # noqa: E402

CHUNK = 960
TIMEOUT = 3.0


def image(seq, size):
    """Deterministic bytes standing in for a JPEG (or PNG)."""
    return bytes((i * 31 + seq * 7 + 1) % 256 for i in range(size))


def chunks(seq, data):
    """FrameChunk records as Yamcs hands them over: `data` always 960 bytes, the first `length` real."""
    parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    return [{"seq": seq, "index": i, "count": len(parts), "length": len(p), "data": p + b"\xee" * (CHUNK - len(p))}
            for i, p in enumerate(parts)]


class AssemblerCase(unittest.TestCase):
    def setUp(self):
        self.images = []
        self.fa = FrameAssembler(lambda *args: self.images.append(args), timeout_s=TIMEOUT)

    def feed(self, cs, now, t_ms=1000, step_s=0.01, step_ms=10):
        """Add chunks in the given order, each a little later than the one before."""
        for i, c in enumerate(cs):
            self.fa.add(c, t_ms + i * step_ms, now + i * step_s)

    def send(self, seq, now, size=100):
        """One whole single-chunk image."""
        self.feed(chunks(seq, image(seq, size)), now)


class ReassemblyTest(AssemblerCase):
    def test_in_order(self):
        data = image(7, 2500)                       # three chunks: 960 + 960 + 580
        cs = chunks(7, data)
        self.assertEqual(len(cs), 3)
        self.feed(cs, now=10.0, t_ms=5000)
        self.assertEqual(self.images, [("frame", 7, data, 5000)])
        counts = self.fa.take_counts()
        self.assertEqual(counts["frames_complete"], 1)
        self.assertEqual(counts["chunks"], 3)
        self.assertEqual(counts["bytes"], len(data))
        self.assertEqual(self.fa.partial, {})

    def test_out_of_order(self):
        data = image(8, 2500)
        cs = chunks(8, data)
        self.feed([cs[2], cs[0]], now=10.0)
        self.assertEqual(self.images, [])
        self.fa.add(cs[1], 1100, 10.1)
        self.assertEqual(len(self.images), 1)
        self.assertEqual(self.images[0][2], data)
        self.assertEqual(self.fa.take_counts()["frames_complete"], 1)

    def test_single_chunk_image(self):
        data = image(9, 960)
        self.fa.add(chunks(9, data)[0], 4242, 1.0)
        self.assertEqual(self.images, [("frame", 9, data, 4242)])

    def test_callback_gets_the_first_chunks_time(self):
        # Capture names the file by the TM time of the image's first chunk.
        self.feed(chunks(3, image(3, 2000)), now=10.0, t_ms=7000, step_ms=250)
        kind, seq, _, t_ms = self.images[0]
        self.assertEqual((kind, seq, t_ms), ("frame", 3, 7000))

    def test_map_chunks_are_maps(self):
        png = image(4, 1500)
        self.feed(chunks(4 | MAP_BIT, png), now=10.0, t_ms=900)
        self.assertEqual(self.images, [("map", 4, png, 900)])
        counts = self.fa.take_counts()
        self.assertEqual(counts["maps_complete"], 1)
        self.assertNotIn("frames_complete", counts)
        self.assertEqual((frames.kind_of(4 | MAP_BIT), frames.kind_of(4)), ("map", "frame"))

    def test_a_map_and_a_frame_with_the_same_number_do_not_mix(self):
        jpg, png = image(5, 1500), image(500, 1500)          # two chunks each
        f, m = chunks(5, jpg), chunks(5 | MAP_BIT, png)
        self.feed([f[0], m[0], m[1], f[1]], now=10.0)
        self.assertEqual(sorted(self.images), sorted([("frame", 5, jpg, 1000), ("map", 5, png, 1010)]))
        counts = self.fa.take_counts()
        self.assertEqual((counts["frames_complete"], counts["maps_complete"]), (1, 1))
        self.assertEqual((counts.get("frames_missing", 0), counts.get("maps_missing", 0)), (0, 0))


class DuplicateAndLateTest(AssemblerCase):
    def test_duplicate_chunk_does_not_break_completion(self):
        data = image(11, 2500)
        cs = chunks(11, data)
        self.feed([cs[0], cs[0], cs[1], cs[2]], now=10.0)
        self.assertEqual([i[2] for i in self.images], [data])
        counts = self.fa.take_counts()
        self.assertEqual(counts["frame_duplicate_chunks"], 1)
        self.assertEqual(counts["frames_complete"], 1)
        self.assertEqual(counts["chunks"], 4)

    def test_duplicate_after_completion(self):
        cs = chunks(12, image(12, 2500))
        self.feed(cs, now=10.0)
        self.fa.add(cs[1], 2000, 11.0)
        self.assertEqual(len(self.images), 1)
        self.assertEqual(self.fa.partial, {})
        counts = self.fa.take_counts()
        self.assertEqual(counts["frame_duplicate_chunks"], 1)
        self.assertEqual(counts["frames_complete"], 1)
        self.fa.expire(12.0 + TIMEOUT)
        self.assertEqual(self.fa.take_counts(), {})

    def test_map_duplicates_counted_as_maps(self):
        cs = chunks(2 | MAP_BIT, image(2, 1000))
        self.feed(cs + cs[:1], now=10.0)
        counts = self.fa.take_counts()
        self.assertEqual(counts["map_duplicate_chunks"], 1)
        self.assertNotIn("frame_duplicate_chunks", counts)

    def test_late_chunk_for_an_expired_image(self):
        cs = chunks(13, image(13, 2500))
        self.feed(cs[:2], now=10.0)
        self.fa.expire(10.0 + TIMEOUT + 0.5)
        self.fa.add(cs[2], 9000, 10.0 + TIMEOUT + 1.0)
        self.assertEqual(self.images, [])
        self.assertEqual(self.fa.partial, {})       # not a new image
        self.fa.expire(10.0 + 3 * TIMEOUT)          # and so never counted incomplete a second time
        counts = self.fa.take_counts()
        self.assertEqual(counts["frames_incomplete"], 1)
        self.assertEqual(counts["frame_late_chunks"], 1)
        self.assertNotIn("frames_complete", counts)

    def test_stragglers_are_recognised_across_timer_ticks(self):
        # Capture calls expire() once a second. A chunk turning up a few seconds after its image completed, or
        # after it was given up on, must still be a duplicate or a late chunk, not the start of a phantom image
        # that the timer then counts incomplete.
        done, dead = chunks(14, image(14, 2500)), chunks(15, image(15, 2500))
        self.feed(done, now=10.0)
        self.feed(dead[:2], now=10.0)
        self.fa.expire(10.0 + TIMEOUT + 0.5)        # 15 given up on
        self.assertEqual(self.fa.take_counts()["frames_incomplete"], 1)
        for k in range(1, 4):
            self.fa.expire(10.0 + TIMEOUT + 0.5 + k)
        self.fa.add(done[0], 3000, 17.0)
        self.fa.add(dead[2], 3010, 17.0)
        self.fa.expire(17.0 + 2 * TIMEOUT)
        self.assertEqual(self.fa.take_counts(), {"chunks": 2, "bytes": CHUNK + 580,
                                                 "frame_duplicate_chunks": 1, "frame_late_chunks": 1})
        self.assertEqual(len(self.images), 1)


class IncompleteTest(AssemblerCase):
    def test_counted_by_the_timer_after_the_timeout_not_before(self):
        cs = chunks(20, image(20, 2500))
        self.fa.add(cs[0], 1000, 10.0)
        self.fa.add(cs[1], 1010, 10.5)
        self.fa.take_counts()
        for now in (11.0, 12.9, 10.0 + TIMEOUT):    # within timeout_s of the first chunk
            self.fa.expire(now)
            self.assertEqual(self.fa.take_counts(), {}, now)
            self.assertIn(20, self.fa.partial)
        # No further chunk arrives (a stall at the end of a flight): the timer alone counts it.
        self.fa.expire(10.0 + TIMEOUT + 0.1)
        self.assertEqual(self.fa.take_counts(), {"frames_incomplete": 1})
        self.assertEqual(self.fa.partial, {})
        self.assertEqual(self.images, [])

    def test_maps_counted_apart(self):
        self.fa.add(chunks(1 | MAP_BIT, image(1, 2500))[0], 1000, 10.0)
        self.fa.take_counts()
        self.fa.expire(20.0)
        self.assertEqual(self.fa.take_counts(), {"maps_incomplete": 1})

    def test_expire_everything_flushes_all(self):
        # At shutdown nothing in flight may vanish from the counts, however young.
        self.fa.add(chunks(30, image(30, 2500))[0], 1000, 50.0)
        self.fa.add(chunks(31, image(31, 2500))[1], 1000, 50.0)
        self.fa.add(chunks(2 | MAP_BIT, image(2, 2500))[0], 1000, 50.0)
        self.fa.take_counts()
        self.fa.expire(50.0, everything=True)
        self.assertEqual(self.fa.partial, {})
        self.assertEqual(self.fa.take_counts(), {"frames_incomplete": 2, "maps_incomplete": 1})
        self.fa.add(chunks(30, image(30, 2500))[1], 1010, 50.1)
        self.assertEqual(self.fa.take_counts()["frame_late_chunks"], 1)
        self.assertEqual(self.fa.partial, {})

    def test_chunk_after_the_timeout_does_not_complete_the_image(self):
        # Regression test for a bug the tests found (now fixed): frames.py:61-74, add() never looks at a partial's age, so an image whose last chunk arrives
        # after timeout_s is counted complete whenever the caller's timer has not yet called expire() (capture.py
        # ticks once a second), against the documented rule "complete: every chunk arrived within timeout_s of the
        # first" (and "checked on a timer, not only when the next chunk happens to arrive"). With these same chunk
        # times, a tick at 12.9 gives frames_complete 1 and a tick at 13.2 gives frames_incomplete 1: the counts
        # depend on when the timer ran, not on the times the caller passed in.
        cs = chunks(21, image(21, 2500))
        self.fa.add(cs[0], 1000, 10.0)
        self.fa.add(cs[1], 1010, 10.1)
        self.fa.expire(12.9)                        # the last tick before the timeout
        self.fa.add(cs[2], 1020, 10.0 + TIMEOUT + 0.5)
        self.fa.expire(10.0 + TIMEOUT + 0.6)
        counts = self.fa.take_counts()
        self.assertNotIn("frames_complete", counts)
        self.assertEqual(counts.get("frames_incomplete"), 1)
        self.assertEqual(self.images, [])


class SequenceTest(AssemblerCase):
    def test_missing_is_whole_sequence_gaps(self):
        self.send(10, now=1.0)                      # the first number seen: nothing before it is missing
        self.send(11, now=1.1)
        self.assertEqual(self.fa.take_counts().get("frames_missing", 0), 0)
        self.send(15, now=1.2)                      # 12, 13 and 14 never seen
        self.assertEqual(self.fa.take_counts()["frames_missing"], 3)

    def test_chunks_lost_inside_an_image_are_not_missing(self):
        self.send(1, now=1.0)
        self.fa.add(chunks(2, image(2, 2500))[0], 1000, 1.1)
        self.send(3, now=1.2)
        self.fa.expire(10.0)
        counts = self.fa.take_counts()
        self.assertEqual(counts.get("frames_missing", 0), 0)
        self.assertEqual(counts["frames_incomplete"], 1)

    def test_maps_have_their_own_numbering(self):
        self.send(100, now=1.0)
        self.send(1 | MAP_BIT, now=1.1)
        self.send(101, now=1.2)
        self.send(4 | MAP_BIT, now=1.3)
        counts = self.fa.take_counts()
        self.assertEqual(counts["maps_missing"], 2)
        self.assertEqual(counts.get("frames_missing", 0), 0)

    def test_a_big_step_back_is_a_restart_not_gaps(self):
        # The payload restarted and numbers frames from 0 again.
        self.send(5000, now=1.0)
        self.send(5001, now=1.1)
        self.send(0, now=30.0)
        self.send(1, now=30.1)
        counts = self.fa.take_counts()
        self.assertEqual(counts["frame_seq_restarts"], 1)
        self.assertEqual(counts.get("frames_missing", 0), 0)
        self.assertEqual(counts["frames_complete"], 4)
        self.send(3, now=30.2)                      # gaps count again, in the new numbering
        self.assertEqual(self.fa.take_counts()["frames_missing"], 1)

    def test_a_small_step_back_is_neither(self):
        self.send(5000, now=1.0)
        self.send(5000 - RESTART_STEP, now=1.1)     # an old image arriving late, not a restart
        counts = self.fa.take_counts()
        self.assertNotIn("frame_seq_restarts", counts)
        self.assertEqual(counts.get("frames_missing", 0), 0)
        self.assertEqual(counts["frames_complete"], 2)
        self.send(5001, now=1.2)
        self.assertEqual(self.fa.take_counts().get("frames_missing", 0), 0)


class SubscriptionGraceTest(AssemblerCase):
    """yamcs-client never reconnects, so capture resubscribes; the image on the wire at that moment is cut by
    the ground, and must not lower the link's completeness."""

    def test_mid_image_start_within_a_second_is_cut(self):
        self.fa.subscribed(100.0)
        self.fa.add(chunks(40, image(40, 2500))[1], 1000, 100.5)
        self.fa.expire(100.5 + TIMEOUT + 0.1)
        self.assertEqual(self.fa.take_counts(), {"chunks": 1, "bytes": CHUNK, "frames_cut": 1})

    def test_map_cut_too(self):
        self.fa.subscribed(100.0)
        self.fa.add(chunks(3 | MAP_BIT, image(3, 2500))[2], 1000, 100.2)
        self.fa.expire(110.0)
        counts = self.fa.take_counts()
        self.assertEqual(counts["maps_cut"], 1)
        self.assertNotIn("maps_incomplete", counts)

    def test_chunk_zero_within_the_grace_is_incomplete(self):
        self.fa.subscribed(100.0)
        self.fa.add(chunks(41, image(41, 2500))[0], 1000, 100.5)
        self.fa.expire(110.0)
        counts = self.fa.take_counts()
        self.assertEqual(counts["frames_incomplete"], 1)
        self.assertNotIn("frames_cut", counts)

    def test_mid_image_start_after_the_grace_is_incomplete(self):
        self.fa.subscribed(100.0)
        self.fa.add(chunks(42, image(42, 2500))[1], 1000, 101.5)
        self.fa.expire(110.0)
        counts = self.fa.take_counts()
        self.assertEqual(counts["frames_incomplete"], 1)
        self.assertNotIn("frames_cut", counts)

    def test_never_subscribed_means_no_grace(self):
        self.fa.add(chunks(43, image(43, 2500))[1], 1000, 0.0)
        self.fa.expire(10.0)
        self.assertEqual(self.fa.take_counts()["frames_incomplete"], 1)

    def test_a_cut_image_whose_first_chunk_turns_up_is_complete(self):
        data = image(44, 2500)
        cs = chunks(44, data)
        self.fa.subscribed(100.0)
        self.feed([cs[1], cs[2], cs[0]], now=100.2)
        self.fa.expire(110.0)
        self.assertEqual([i[2] for i in self.images], [data])
        counts = self.fa.take_counts()
        self.assertEqual(counts["frames_complete"], 1)
        self.assertNotIn("frames_cut", counts)


class CountsTest(AssemblerCase):
    def test_take_counts_returns_and_resets(self):
        self.send(1, now=1.0, size=300)
        first = self.fa.take_counts()
        self.assertEqual(first, {"chunks": 1, "bytes": 300, "frames_complete": 1})
        self.assertEqual(json.loads(json.dumps(first)), first)     # capture writes it to stats.jsonl
        self.assertEqual(self.fa.take_counts(), {})
        self.send(2, now=1.1)                                      # the next minute's counting
        self.assertEqual(first, {"chunks": 1, "bytes": 300, "frames_complete": 1})

    def test_sequence_tracking_survives_take_counts(self):
        # Counts are per minute, but a gap across a minute boundary is still a gap.
        self.send(1, now=1.0)
        self.fa.take_counts()
        self.send(3, now=61.0)
        self.assertEqual(self.fa.take_counts()["frames_missing"], 1)

    def test_chunks_and_bytes_count_everything_received(self):
        cs = chunks(50, image(50, 2500))
        self.feed(cs + [cs[2]], now=1.0)                       # one duplicate after completion
        counts = self.fa.take_counts()
        self.assertEqual(counts["chunks"], 4)
        self.assertEqual(counts["bytes"], 2500 + 580)

    def test_keys_the_quicklook_reads(self):
        # quicklook.last_minutes() sums these names from stats.jsonl; a renamed counter would read as zero there.
        fa = self.fa
        self.send(1, now=1.0)                                   # frames_complete
        self.send(3, now=1.1)                                   # frames_missing
        cs = chunks(4, image(4, 2500))
        self.feed(cs[:1] + cs[:1], now=1.2)                     # frame_duplicate_chunks
        fa.expire(10.0)                                         # frames_incomplete
        fa.add(cs[1], 1000, 10.1)                               # frame_late_chunks
        fa.subscribed(20.0)
        fa.add(chunks(5, image(5, 2500))[1], 1000, 20.1)
        fa.expire(30.0)                                         # frames_cut
        self.send(1 | MAP_BIT, now=31.0)                        # maps_complete
        self.send(3 | MAP_BIT, now=31.1)                        # maps_missing
        fa.add(chunks(4 | MAP_BIT, image(4, 2500))[0], 1000, 32.0)
        fa.expire(40.0)                                         # maps_incomplete
        counts = fa.take_counts()
        self.assertEqual([k for k in quicklook.COUNT_KEYS if k not in counts], [])
        self.assertTrue(all(counts[k] > 0 for k in quicklook.COUNT_KEYS))


if __name__ == "__main__":
    unittest.main()
