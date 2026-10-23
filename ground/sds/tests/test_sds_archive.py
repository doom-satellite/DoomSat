"""Reading parameters from Yamcs in chunks: doomsat_sds.archive.YamcsArchive.parameters.

Seen live on Yamcs 5.12.8: a 2.5-hour streamParameterValues lost its replay thread 30 s in, and the HTTP response
never ended, so the forward run's build_l1 waited for ever. Reads are now cut into CHUNK_MS replays, each request
has a read timeout, and a chunk that fails is asked for once more. What must hold: the chunked read returns
exactly what one replay of the whole window would (no sample lost or doubled on a chunk boundary), a single
failure costs nothing, a second one is raised rather than swallowed, and the timeout reaches every request.
"""
import datetime as dt
import os
import sys
import unittest
from types import SimpleNamespace

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import archive, config  # noqa: E402

T0 = 1_791_000_000_000          # 2026-10-03, a whole second
NAMES = ["TIC", "PLAYER_X"]


def at(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


class FakeYamcsArchive:
    """stream_parameter_values over samples every 500 ms; like a replay, both ends of the window are inclusive."""

    def __init__(self, start_ms, stop_ms, fail=()):
        self.samples = [(t, n) for t in range(start_ms, stop_ms + 1, 500) for n in NAMES]
        self.calls, self.fail = [], list(fail)

    def stream_parameter_values(self, names, start, stop):
        lo, hi = archive.to_ms(start), archive.to_ms(stop)
        self.calls.append((lo, hi))
        assert sorted(names) == sorted(config.qualified(n) for n in NAMES)
        if self.fail and self.fail.pop(0):
            raise TimeoutError("read timed out")
        for t, n in self.samples:
            if lo <= t <= hi:
                yield SimpleNamespace(parameters=[SimpleNamespace(
                    name=config.qualified(n), generation_time=at(t), reception_time=at(t + 40), eng_value=t % 997)])


def reader(fake):
    a = object.__new__(archive.YamcsArchive)      # no client, no network
    a.archive = fake
    return a


def one_replay(fake, start_ms, stop_ms):
    out = {n: [] for n in NAMES}
    for t, n in fake.samples:
        if start_ms <= t <= stop_ms:
            out[n].append((t, t + 40, t % 997))
    return out


class TestChunkedParameters(unittest.TestCase):
    def test_chunks_return_exactly_one_replay_of_the_window(self):
        start, stop = T0, T0 + 2 * archive.CHUNK_MS + 61_000        # three chunks, both inner bounds on a sample
        fake = FakeYamcsArchive(start - 5000, stop + 5000)
        got = reader(fake).parameters(NAMES, start, stop)
        self.assertEqual(got, one_replay(fake, start, stop))
        self.assertEqual(fake.calls, [(start, start + archive.CHUNK_MS),
                                      (start + archive.CHUNK_MS, start + 2 * archive.CHUNK_MS),
                                      (start + 2 * archive.CHUNK_MS, stop)])
        for n in NAMES:
            times = [s[0] for s in got[n]]
            self.assertEqual(len(times), len(set(times)), "a sample on a chunk boundary came back twice")
            self.assertEqual((times[0], times[-1]), (start, stop), "the outer bounds are Yamcs's, as before")

    def test_a_short_window_is_one_request(self):
        fake = FakeYamcsArchive(T0, T0 + 60_000)
        reader(fake).parameters(NAMES, T0, T0 + 30_000)
        self.assertEqual(fake.calls, [(T0, T0 + 30_000)])

    def test_an_empty_window_asks_nothing(self):
        fake = FakeYamcsArchive(T0, T0 + 60_000)
        self.assertEqual(reader(fake).parameters(NAMES, T0, T0), {n: [] for n in NAMES})
        self.assertEqual(fake.calls, [])

    def test_one_failed_chunk_is_asked_for_again(self):
        start, stop = T0, T0 + archive.CHUNK_MS + 30_000
        fake = FakeYamcsArchive(start, stop, fail=[False, True])     # the second chunk's first try fails
        got = reader(fake).parameters(NAMES, start, stop)
        self.assertEqual(got, one_replay(fake, start, stop))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[1], fake.calls[2])

    def test_a_chunk_failing_twice_is_raised_not_swallowed(self):
        fake = FakeYamcsArchive(T0, T0 + 60_000, fail=[True, True])
        with self.assertRaises(TimeoutError):
            reader(fake).parameters(NAMES, T0, T0 + 30_000)


class TestReadTimeout(unittest.TestCase):
    def test_every_request_gets_a_read_timeout_unless_it_names_one(self):
        seen = []
        session = SimpleNamespace(request=lambda method, url, **kw: seen.append(kw.get("timeout")))
        archive.with_read_timeout(session)
        session.request("POST", "http://x/api/archive/i:streamParameterValues", stream=True)
        session.request("GET", "http://x/api", timeout=5)
        self.assertEqual(seen, [(10, archive.READ_TIMEOUT_S), 5])

    def test_an_archive_can_be_given_a_shorter_one(self):
        # The record wait's sensor reads with 15 s, so a stalled Yamcs fails one poke and not the whole wait.
        from unittest import mock
        seen = []

        class Client:
            def __init__(self, address):
                self.ctx = SimpleNamespace(session=SimpleNamespace(
                    request=lambda method, url, **kw: seen.append(kw.get("timeout"))))

            def get_archive(self, instance):
                return None

        fake = SimpleNamespace(YamcsClient=Client)
        with mock.patch.dict(sys.modules, {"yamcs": SimpleNamespace(), "yamcs.client": fake}):
            for kw, want in (({}, archive.READ_TIMEOUT_S), ({"read_s": 15}, 15)):
                a = archive.YamcsArchive("localhost:8090", **kw)
                a.client.ctx.session.request("GET", "http://x/api")
                self.assertEqual(seen.pop(), (10, want))


if __name__ == "__main__":
    unittest.main()
