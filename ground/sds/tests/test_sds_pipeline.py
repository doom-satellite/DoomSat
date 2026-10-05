"""The pipeline end to end: watch, locate, L1, L2, the L3 rollup and reprocessing, as the DAGs run them, but over a
recorded window of the live archive (two deaths) and a temporary product store, with no Airflow and no Yamcs.

What it protects is the promise the whole data system rests on (docs/plans/sds-airflow.md, "Plan"): every step
is idempotent (a retried Airflow task finds the same bytes and changes nothing), the catalog says which version of
each product is current, a new algorithm version is built beside the old one rather than over it (Phase C), and
reprocessing starts again from the archive and *says so* when the archive no longer gives back what it gave: a
silent rewrite of a cataloged product would make every checksum in the catalog meaningless. The forward run also
captures the flight's context once (from the live process table), so context.capture is replaced by a fixed
answer here, and the commit by "testcommit", so every byte is deterministic.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import archive, config, context, episodes, pipeline, products, store  # noqa: E402
from doomsat_sds.catalog import Catalog  # noqa: E402

REAL_CAPTURE = context.capture             # PipelineCase replaces it; the WAD tests below call it for real

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
FIXTURE_START_MS, FIXTURE_STOP_MS = 1791074769000, 1791074812000     # [00:46:09Z, 00:46:52Z)
AT_MS = FIXTURE_STOP_MS + 15_000         # "now" for watch: both deaths have settled
LOOKBACK_S = 120
EP1_END_MS = 1791074771233               # PlayerDied, episode 1 (its start is long before the fixture)
EP2_START_EVENT_MS = 1791074773533       # EpisodeStarted 2
EP2_END_MS = 1791074807184               # PlayerDied, episode 2
EP2_WINDOW = [1791074773533, 1791074807183]   # computed live, 33.65 s
EP1_ID = episodes.episode_id(EP1_END_MS, 1)
EP2_ID = episodes.episode_id(EP2_END_MS, 2)

# The recording predates main's WAD channels, so the forward run's own read of them finds none.
PRE_MAIN = {"missing": "the archive has no WAD_IWAD sample from 5 s before the episode to its end, as on a flight "
                       "build from before main"}
FIXED_CONTEXT = {
    "wad": "freedoom1.wad", "map": "E1M1", "level": 1, "skill": 3, "seed": 7, "geometry": None, "oracle": None,
    "pilot_mode": "system-one=code system-two=none", "repo_commit": "0123456789ab", "level_set": "dev",
    "sources": {"payload": "arguments of the running payload (pid 4242)", "map": "payload.log",
                "pilot": "arguments of the running pilot (pid 4343)"},
}


def bumped(version: str) -> str:
    """The version a Phase C change would give: minor incremented, patch reset."""
    major, minor, _ = (int(x) for x in version.split("."))
    return "%d.%d.0" % (major, minor + 1)


class PerturbedArchive:
    """The recorded archive, except that one archived sample now reads differently: what a reader bug, a lossy
    back-fill or a re-ingest would look like to reprocessing."""

    def __init__(self, inner, name, t_ms, new_value):
        self.inner, self.name, self.t_ms, self.new_value = inner, name, t_ms, new_value

    def parameters(self, names, start_ms, stop_ms):
        out = self.inner.parameters(names, start_ms, stop_ms)
        if self.name in out:
            out[self.name] = [(g, r, self.new_value if g == self.t_ms else v) for g, r, v in out[self.name]]
        return out

    def events(self, start_ms, stop_ms, types=None):
        return self.inner.events(start_ms, stop_ms, types)

    def commands(self, start_ms, stop_ms):
        return self.inner.commands(start_ms, stop_ms)

    def server_id(self):
        return self.inner.server_id()


class StubArchive:
    """A hand-built archive: a few events and EPISODE samples, nothing else."""

    def __init__(self, events, series):
        self._events, self._series = events, series

    def events(self, start_ms, stop_ms, types=None):
        return [e for e in self._events if start_ms <= e["t"] < stop_ms and (not types or e["type"] in types)]

    def parameters(self, names, start_ms, stop_ms):
        return {n: [s for s in self._series.get(n, []) if start_ms <= s[0] < stop_ms] for n in names}

    def commands(self, start_ms, stop_ms):
        return []

    def server_id(self):
        return "vm"


class PipelineCase(unittest.TestCase):
    """A fresh store and catalog per test, with context capture and the commit pinned."""

    @classmethod
    def setUpClass(cls):
        cls.archive = archive.RecordedArchive(FIXTURE)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.settings = config.Settings(home=self.tmp / "sds", doomsat_home=self.tmp / "doom")
        self.catalog = Catalog(self.settings.catalog)
        self.addCleanup(self.catalog.close)
        self.capture = self._patch(mock.patch("doomsat_sds.context.capture", return_value=copy.deepcopy(FIXED_CONTEXT)))
        self._patch(mock.patch("doomsat_sds.pipeline._commit", return_value="testcommit"))
        # Every product registration, with its status, as the lineage file should record it.
        self.registered = []
        real = pipeline._register

        def spy(*args, **kwargs):
            ref = real(*args, **kwargs)
            self.registered.append(ref)
            return ref
        self._patch(mock.patch.object(pipeline, "_register", spy))

    def _patch(self, patcher):
        thing = patcher.start()
        self.addCleanup(patcher.stop)
        return thing

    def watch(self, at_ms=AT_MS, lookback_s=LOOKBACK_S, arch=None):
        return pipeline.watch(self.settings, arch or self.archive, self.catalog, lookback_s=lookback_s, at_ms=at_ms)

    def forward(self, at_ms=AT_MS):
        """What sds_episode_watch and sds_forward do: every episode watch reports, located, L1, then the L2s."""
        out = {}
        for conf in self.watch(at_ms):
            located = pipeline.locate_episode(self.archive, conf)
            run_id = "fwd__" + conf["episode_id"]
            l1_ref = pipeline.l1(self.settings, self.archive, self.catalog, located, run_id=run_id)
            l2_refs = {t: pipeline.l2(self.settings, self.catalog, l1_ref, t, run_id=run_id) for t in products.L2_TYPES}
            out[conf["episode_id"]] = {"located": located, "l1": l1_ref, "l2": l2_refs}
        return out

    def located(self, episode_id):
        conf = next(c for c in self.watch() if c["episode_id"] == episode_id)
        return pipeline.locate_episode(self.archive, conf)

    def lineage_lines(self):
        path = self.settings.lineage / "openlineage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestWatch(PipelineCase):
    """Which episodes the minute-by-minute watch hands to sds_forward."""

    def test_both_closed_episodes_oldest_first(self):
        confs = self.watch()
        self.assertEqual([c["episode_id"] for c in confs], [EP1_ID, EP2_ID])
        self.assertEqual(EP2_ID, "20261004T004647Z-e0002")
        for c in confs:
            self.assertEqual(c["episode_id"], episodes.episode_id(c["end_ms"], c["number"]))
            self.assertEqual(c["end_utc"], archive.iso(c["end_ms"]))
            self.assertEqual((c["closing"], c["outcome"], c["inferred"]), ("PlayerDied", "died", False))
        self.assertEqual([(c["number"], c["end_ms"]) for c in confs], [(1, EP1_END_MS), (2, EP2_END_MS)])
        self.assertIsNone(confs[0]["start_event_ms"])        # episode 1 began before the fixture
        self.assertEqual(confs[1]["start_event_ms"], EP2_START_EVENT_MS)

    def test_an_episode_closed_less_than_settle_s_ago_waits(self):
        # Its last samples may not be archived yet; the next minute's watch picks it up.
        self.assertEqual([c["episode_id"] for c in self.watch(at_ms=FIXTURE_STOP_MS)], [EP1_ID])
        edge = EP2_END_MS + pipeline.SETTLE_S * 1000
        self.assertEqual([c["episode_id"] for c in self.watch(at_ms=edge - 1)], [EP1_ID])
        self.assertEqual([c["episode_id"] for c in self.watch(at_ms=edge)], [EP1_ID, EP2_ID])

    def test_an_episode_closed_before_the_lookback_is_left_out(self):
        # AT_MS is 55.8 s after episode 1 died and 19.8 s after episode 2 died.
        self.assertEqual([c["episode_id"] for c in self.watch(lookback_s=30)], [EP2_ID])

    def test_an_episode_with_an_l1_is_not_handed_out_again(self):
        self.forward(at_ms=FIXTURE_STOP_MS)                   # catalogs episode 1 only
        self.assertIsNotNone(self.catalog.current(EP1_ID, "l1_episode"))
        self.assertEqual([c["episode_id"] for c in self.watch()], [EP2_ID])
        self.forward()
        self.assertEqual(self.watch(), [])

    def test_an_episode_cataloged_without_an_l1_is_still_handed_out(self):
        # An sds_forward run that cataloged the episode and then failed before writing its L1 must be retried.
        located = self.located(EP1_ID)
        self.catalog.add_episode(pipeline._episode_row(located, FIXED_CONTEXT))
        self.assertEqual([c["episode_id"] for c in self.watch()], [EP1_ID, EP2_ID])

    def test_an_inferred_reset_is_handed_out_only_when_the_samples_confirm_it(self):
        # A flight's first episode has no start event and, when RESET_GAME ends it, no end event either: only
        # EpisodeStarted(3) shows that episode 2 ended. Watch acts on that only if EPISODE really was 2.
        t = AT_MS - 60_000
        start3 = {"t": t, "rt": t - 950, "seq": 1, "source": config.EVENT_SOURCE, "message": "",
                  "type": config.EVENT_PREFIX + "EpisodeStarted", "extra": {"episode": "3"}}
        samples = lambda v: {"EPISODE": [(t - 10_000 + i * 100, None, v) for i in range(100)]}
        confs = self.watch(arch=StubArchive([start3], samples(2)))
        self.assertEqual(len(confs), 1)
        self.assertEqual((confs[0]["number"], confs[0]["closing"], confs[0]["outcome"], confs[0]["inferred"]),
                         (2, "EpisodeStarted", "reset", True))
        self.assertEqual(confs[0]["episode_id"], episodes.episode_id(t, 2))
        self.assertEqual(self.watch(arch=StubArchive([start3], samples(1))), [])
        self.assertEqual(self.watch(arch=StubArchive([start3], {})), [])


class TestLocate(PipelineCase):
    def test_episode_2_window_is_the_one_computed_live(self):
        conf = next(c for c in self.watch() if c["episode_id"] == EP2_ID)
        located = pipeline.locate_episode(self.archive, conf)
        self.assertEqual(located["window"], EP2_WINDOW)
        self.assertEqual({k: v for k, v in located.items() if k != "window"}, conf)

    def test_episode_1_is_truncated_at_the_fixture_start(self):
        first = self.archive.parameters(["EPISODE"], FIXTURE_START_MS, FIXTURE_STOP_MS)["EPISODE"][0][0]
        self.assertEqual(self.located(EP1_ID)["window"], [first, EP1_END_MS])

    def test_an_episode_the_archive_never_shows_raises(self):
        conf = episodes.ClosedEpisode(9, EP2_END_MS, "PlayerDied", "died", None).as_conf()
        with self.assertRaises(LookupError):
            pipeline.locate_episode(self.archive, conf)

    @staticmethod
    def long_episode(minutes, step_ms):
        """Episode 4 dying at EP2_END_MS after `minutes` of samples, with episode 3 ending 2 s before it began."""
        end = EP2_END_MS
        first = end - minutes * 60_000
        ts = list(range(first - 300_000, first - 1_999, step_ms)) + list(range(first, end + 1, step_ms))
        series = {"EPISODE": [(t, None, 4 if t >= first else 3) for t in ts],
                  "TIC": [(t, None, 3 * i) for i, t in enumerate(ts)]}
        conf = episodes.ClosedEpisode(4, end, "PlayerDied", "died", None).as_conf()
        return StubArchive([], series), conf, [t for t in ts if t >= first]

    def test_an_episode_longer_than_the_first_look_is_located_whole(self):
        # The first look reaches 15 minutes back; the code autopilot has flown an 18-minute episode. A window cut
        # at the edge of the first look would silently drop the start of the flight from L1.
        arch, conf, ts = self.long_episode(minutes=20, step_ms=1_000)
        self.assertEqual(pipeline.locate_episode(arch, conf)["window"], [ts[0], ts[-1]])

    def test_an_episode_longer_than_four_hours_takes_what_the_archive_has(self):
        # Beyond four hours the search stops widening and keeps the last four hours rather than failing forever.
        arch, conf, ts = self.long_episode(minutes=300, step_ms=10_000)
        cap = EP2_END_MS - 4 * 3600 * 1000
        self.assertEqual(pipeline.locate_episode(arch, conf)["window"], [min(t for t in ts if t >= cap), ts[-1]])


class TestLevel1(PipelineCase):
    def test_l1_writes_registers_and_catalogs_the_episode(self):
        located = self.located(EP2_ID)
        ref = pipeline.l1(self.settings, self.archive, self.catalog, located, run_id="fwd__" + EP2_ID)
        ver = products.version("l1_episode")
        path = store.episode_product_path(self.settings, EP2_ID, "l1_episode", ver, "json")

        self.assertEqual(ref["status"], "new")
        self.assertEqual(ref["product_id"], "%s/l1_episode@%s" % (EP2_ID, ver))
        self.assertEqual(ref["path"], str(path))
        self.assertTrue(path.is_file())
        self.assertEqual(store.sha256_file(path), ref["sha256"])
        self.capture.assert_called_once_with(self.settings, 2, EP2_WINDOW[0], flown=PRE_MAIN)

        doc = store.read_json(path)
        self.assertEqual(doc["product"], {"type": "l1_episode", "level": "L1", "version": ver})
        self.assertEqual(doc["episode"]["id"], EP2_ID)
        self.assertEqual([doc["episode"]["start_ms"], doc["episode"]["end_ms"]], EP2_WINDOW)
        self.assertEqual(doc["context"], FIXED_CONTEXT)

        row = self.catalog.product(ref["product_id"])
        self.assertEqual((row["episode_id"], row["level"], row["product_type"], row["algorithm_version"]),
                         (EP2_ID, "L1", "l1_episode", ver))
        self.assertEqual((row["path"], row["sha256"], row["size_bytes"]), (str(path), ref["sha256"], path.stat().st_size))
        self.assertEqual(row["media_type"], products.ALGORITHMS["l1_episode"][3])
        self.assertEqual((row["code_commit"], row["airflow_run_id"]), ("testcommit", "fwd__" + EP2_ID))
        self.assertEqual(row["inputs"], doc["inputs"])
        self.assertTrue(row["is_current"])

        ep = self.catalog.episode(EP2_ID)
        self.assertEqual((ep["number"], ep["outcome"], ep["closing_event"], ep["inferred_close"]), (2, "died", "PlayerDied", 0))
        self.assertEqual((ep["closing_ms"], ep["start_event_ms"]), (EP2_END_MS, EP2_START_EVENT_MS))
        self.assertEqual([ep["start_ms"], ep["end_ms"]], EP2_WINDOW)
        self.assertEqual((ep["start_utc"], ep["end_utc"]), (archive.iso(EP2_WINDOW[0]), archive.iso(EP2_WINDOW[1])))
        self.assertEqual(ep["duration_s"], 33.65)
        for key in ("wad", "map", "skill", "seed", "pilot_mode", "repo_commit", "level_set"):
            self.assertEqual(ep[key], FIXED_CONTEXT[key], key)
        self.assertEqual(ep["context"], FIXED_CONTEXT)

    def test_l1_again_is_unchanged_and_keeps_the_flight_context(self):
        # A retried task, or one run hours later when another payload is running: the context belongs to the
        # flight, so it comes from the catalog and the bytes are the same.
        located = self.located(EP2_ID)
        first = pipeline.l1(self.settings, self.archive, self.catalog, located)
        before = Path(first["path"]).read_bytes()
        self.capture.return_value = dict(FIXED_CONTEXT, wad="doom1.wad", level_set="test")
        again = pipeline.l1(self.settings, self.archive, self.catalog, located)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual((again["product_id"], again["sha256"], again["path"]),
                         (first["product_id"], first["sha256"], first["path"]))
        self.assertEqual(Path(again["path"]).read_bytes(), before)
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.catalog.episode(EP2_ID)["context"], FIXED_CONTEXT)
        self.assertEqual(len(self.catalog.products(EP2_ID, "l1_episode")), 1)
        self.assertEqual(self.catalog.findings(), [])


class WadArchive:
    """The recorded archive plus main's WAD channels: `wad` is [(t_ms, base, patch, loads)], or an exception every
    read of those channels raises (a mission database without them, or a Yamcs that went away)."""

    def __init__(self, inner, wad):
        self.inner, self.wad, self.wad_reads = inner, wad, []

    def parameters(self, names, start_ms, stop_ms):
        names = list(names)
        if not set(names) & set(config.CONTEXT_TLM):
            return self.inner.parameters(names, start_ms, stop_ms)
        self.wad_reads.append(names)
        if isinstance(self.wad, Exception):
            raise self.wad
        hexname = lambda v: v.encode("ascii").ljust(40, b"\0").hex() if isinstance(v, str) else v
        out = {n: [] for n in names}
        for t, base, patch, loads in self.wad:
            if start_ms <= t < stop_ms:
                for n, v in zip(config.CONTEXT_TLM, (base, patch, loads)):
                    out[n].append((t, t - 950, hexname(v)))
        return out

    def events(self, start_ms, stop_ms, types=None):
        return self.inner.events(start_ms, stop_ms, types)

    def commands(self, start_ms, stop_ms):
        return self.inner.commands(start_ms, stop_ms)

    def server_id(self):
        return self.inner.server_id()


class TestWadContext(PipelineCase):
    """The forward run on main: the WAD in the context comes from its own read of the WAD channels, and L1's read of
    the archive (its inputs, and so its bytes apart from the context) is what it always was."""

    def setUp(self):
        super().setUp()
        # The real capture, with no process running: everything below comes from telemetry or nowhere.
        self.capture.side_effect = lambda *a, **kw: REAL_CAPTURE(*a, procs=[], log_text="", **kw)

    def test_the_wad_comes_from_telemetry_and_l1_reads_what_it_always_read(self):
        before = EP2_WINDOW[0] - 700
        arch = WadArchive(self.archive, [(before, "freedoom2.wad", "basic.wad", 1),
                                         (EP2_WINDOW[1] + 500, "doom1.wad", "", 2)])   # a later switch: not this one
        ref = pipeline.l1(self.settings, arch, self.catalog, self.located(EP2_ID))
        self.assertEqual(arch.wad_reads, [list(config.CONTEXT_TLM)])      # one read of its own, nothing mixed in
        ep = self.catalog.episode(EP2_ID)
        self.assertEqual((ep["wad"], ep["level_set"]), ("freedoom2.wad", "other"))
        self.assertEqual((ep["context"]["pwad"], ep["context"]["wad_loads"]), ("basic.wad", 1))
        self.assertTrue(ep["context"]["sources"]["wad"].startswith("telemetry"), ep["context"]["sources"])
        doc = store.read_json(ref["path"])
        self.assertEqual(doc["context"], ep["context"])
        names = [i for i in doc["inputs"] if i["kind"] == "parameters"][0]["names"]
        self.assertFalse(set(names) & set(config.CONTEXT_TLM))
        self.assertEqual([i["types"] for i in doc["inputs"] if i["kind"] == "events"], [list(pipeline.L1_EVENTS)])
        summary = products.build_summary(doc)
        self.assertEqual((summary["wad"], summary["pwad"], summary["wad_loads"]), ("freedoom2.wad", "basic.wad", 1))

    def test_a_mission_database_without_the_channels_still_gets_its_l1(self):
        refused = Exception("404 Client Error: No such parameter (missing namespace?)")
        pipeline.l1(self.settings, WadArchive(self.archive, refused), self.catalog, self.located(EP2_ID))
        ep = self.catalog.episode(EP2_ID)
        self.assertIsNone(ep["wad"])                              # no payload process either: unknown, not guessed
        self.assertIn("refused", ep["context"]["sources"]["wad"])
        self.assertIsNotNone(self.catalog.current(EP2_ID, "l1_episode"))

    def test_a_yamcs_that_goes_away_catalogs_nothing(self):
        with self.assertRaises(ConnectionError):
            pipeline.l1(self.settings, WadArchive(self.archive, ConnectionError("refused")), self.catalog,
                        self.located(EP2_ID))
        self.assertIsNone(self.catalog.episode(EP2_ID))

    def test_a_cataloged_episode_is_not_read_again(self):
        located = self.located(EP2_ID)
        pipeline.l1(self.settings, self.archive, self.catalog, located)
        arch = WadArchive(self.archive, [(EP2_WINDOW[0] - 700, "freedoom2.wad", "", 1)])
        again = pipeline.l1(self.settings, arch, self.catalog, located)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(arch.wad_reads, [])                      # the context belongs to the flight


class TestLevel2(PipelineCase):
    def test_each_l2_type_is_built_from_the_l1_and_points_at_it(self):
        l1_ref = pipeline.l1(self.settings, self.archive, self.catalog, self.located(EP2_ID))
        for t in products.L2_TYPES:
            with self.subTest(product_type=t):
                level, ver, ext, media = products.ALGORITHMS[t]
                ref = pipeline.l2(self.settings, self.catalog, l1_ref, t, run_id="fwd__" + EP2_ID)
                path = store.episode_product_path(self.settings, EP2_ID, t, ver, ext)
                self.assertEqual(ref["status"], "new")
                self.assertEqual(ref["product_id"], "%s/%s@%s" % (EP2_ID, t, ver))
                self.assertEqual(ref["path"], str(path))
                self.assertEqual(store.sha256_file(path), ref["sha256"])
                row = self.catalog.product(ref["product_id"])
                self.assertEqual(row["inputs"], [{"product_id": l1_ref["product_id"], "sha256": l1_ref["sha256"]}])
                self.assertEqual((row["level"], row["algorithm_version"], row["media_type"]), (level, ver, media))
                self.assertTrue(row["is_current"])
                if ext == "png":
                    self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                else:
                    doc = store.read_json(path)
                    self.assertEqual(doc["episode_id"], EP2_ID)
                    self.assertEqual(doc["product"]["version"], ver)

    def test_l2_again_is_unchanged(self):
        l1_ref = pipeline.l1(self.settings, self.archive, self.catalog, self.located(EP2_ID))
        first = {t: pipeline.l2(self.settings, self.catalog, l1_ref, t) for t in products.L2_TYPES}
        for t in products.L2_TYPES:
            again = pipeline.l2(self.settings, self.catalog, l1_ref, t)
            self.assertEqual((again["status"], again["sha256"]), ("unchanged", first[t]["sha256"]), t)
        self.assertEqual(self.catalog.findings(), [])


class TestRollup(PipelineCase):
    def test_rollup_is_over_the_current_summaries_and_is_current(self):
        self.forward()
        ref = pipeline.rollup(self.settings, self.catalog, run_id="rollup1")
        summaries = self.catalog.products(product_type="l2_summary", current_only=True)
        self.assertEqual(sorted(s["episode_id"] for s in summaries), [EP1_ID, EP2_ID])
        ids, shas = [s["product_id"] for s in summaries], [s["sha256"] for s in summaries]
        ver = products.version("l3_rollup")
        h = products.rollup_inputs_hash(ids, shas)

        self.assertEqual(ref["status"], "new")
        self.assertEqual(ref["product_id"], "l3/l3_rollup@%s/%s" % (ver, h[:12]))
        self.assertEqual(ref["path"], str(store.l3_product_path(self.settings, "l3_rollup", ver, h)))
        row = self.catalog.product(ref["product_id"])
        self.assertEqual(row["inputs"], [{"product_id": i, "sha256": s} for i, s in zip(ids, shas)])
        self.assertIsNone(row["episode_id"])
        self.assertEqual((row["level"], row["algorithm_version"]), ("L3", ver))
        self.assertTrue(row["is_current"])
        self.assertEqual(self.catalog.current(None, "l3_rollup")["product_id"], ref["product_id"])
        doc = store.read_json(ref["path"])
        self.assertEqual(doc["episodes"], 2)
        self.assertEqual(doc["inputs"], sorted(ids))
        self.assertEqual(doc["outcomes"], {"died": 2})

        self.assertEqual(pipeline.rollup(self.settings, self.catalog)["status"], "unchanged")

    def test_a_new_summary_makes_a_new_rollup_current(self):
        self.forward(at_ms=FIXTURE_STOP_MS)                 # episode 1 only
        r1 = pipeline.rollup(self.settings, self.catalog)
        self.assertEqual(store.read_json(r1["path"])["episodes"], 1)
        self.forward()                                       # then episode 2
        r2 = pipeline.rollup(self.settings, self.catalog)
        self.assertEqual(r2["status"], "new")
        self.assertNotEqual(r2["product_id"], r1["product_id"])
        self.assertEqual(store.read_json(r2["path"])["episodes"], 2)
        self.assertFalse(self.catalog.product(r1["product_id"])["is_current"])
        self.assertTrue(self.catalog.product(r2["product_id"])["is_current"])
        self.assertTrue(Path(r1["path"]).is_file())

    def test_a_reprocessed_summary_replaces_the_old_one_in_the_rollup(self):
        # After a Phase C bump of l2_summary, episode 2 has two summaries in the catalog. The L3 must count it
        # once, from the current one; a rollup over every summary row would count each reprocessed episode twice.
        refs = self.forward()
        level, ver, ext, media = products.ALGORITHMS["l2_summary"]
        with mock.patch.dict(products.ALGORITHMS, {"l2_summary": (level, bumped(ver), ext, media)}):
            pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)
        self.assertEqual(len(self.catalog.products(EP2_ID, "l2_summary")), 2)
        ref = pipeline.rollup(self.settings, self.catalog)
        want = sorted([refs[EP1_ID]["l2"]["l2_summary"]["product_id"], "%s/l2_summary@%s" % (EP2_ID, bumped(ver))])
        self.assertEqual(sorted(i["product_id"] for i in self.catalog.product(ref["product_id"])["inputs"]), want)
        doc = store.read_json(ref["path"])
        self.assertEqual((doc["episodes"], doc["inputs"]), (2, want))


class TestReprocess(PipelineCase):
    def test_reprocessing_an_up_to_date_episode_reproduces_l1_and_builds_nothing(self):
        refs = self.forward()
        rows, lines = self.catalog.products(), len(self.lineage_lines())
        report = pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID, run_id="re1")
        self.assertEqual(report["episode_id"], EP2_ID)
        self.assertTrue(report["l1"].startswith("reproduced"), report["l1"])
        self.assertIn(refs[EP2_ID]["l1"]["sha256"][:12], report["l1"])
        self.assertEqual(report["built"], [])
        self.assertEqual(report["up_to_date"], list(products.L2_TYPES))
        self.assertEqual(self.catalog.products(), rows)
        self.assertEqual(self.catalog.findings(), [])
        self.assertEqual(len(self.lineage_lines()), lines)
        self.assertEqual(list(Path(refs[EP2_ID]["l1"]["path"]).parent.glob("*rebuilt-*")), [])
        self.capture.assert_has_calls([mock.call(self.settings, 1, mock.ANY, flown=PRE_MAIN),
                                       mock.call(self.settings, 2, EP2_WINDOW[0], flown=PRE_MAIN)])
        self.assertEqual(self.capture.call_count, 2)          # forward only: reprocessing reuses the catalog's

    def test_reprocessing_an_episode_not_in_the_catalog_raises(self):
        with self.assertRaises(LookupError):
            pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)

    def test_a_version_bump_builds_only_that_product_beside_the_old_one(self):
        # Phase C: a change to the path image is a new l2_path version. Reprocessing builds that one product at
        # the new version; the old row and file stay, and the catalog's "current" moves to the new one.
        refs = self.forward()
        old = refs[EP2_ID]["l2"]["l2_path"]
        old_bytes = Path(old["path"]).read_bytes()
        level, ver, ext, media = products.ALGORITHMS["l2_path"]
        new_ver = bumped(ver)
        with mock.patch.dict(products.ALGORITHMS, {"l2_path": (level, new_ver, ext, media)}):
            self.assertEqual(products.version("l2_path"), new_ver)
            report = pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID, run_id="re1")
            self.assertTrue(report["l1"].startswith("reproduced"), report["l1"])
            new_id = "%s/l2_path@%s" % (EP2_ID, new_ver)
            self.assertEqual(report["built"], [new_id])
            self.assertEqual(report["up_to_date"], [t for t in products.L2_TYPES if t != "l2_path"])

            old_row, new_row = self.catalog.product(old["product_id"]), self.catalog.product(new_id)
            self.assertIsNotNone(old_row)
            self.assertFalse(old_row["is_current"])
            self.assertEqual(old_row["algorithm_version"], ver)
            self.assertTrue(new_row["is_current"])
            self.assertEqual(new_row["algorithm_version"], new_ver)
            self.assertEqual(self.catalog.current(EP2_ID, "l2_path")["product_id"], new_id)
            l1_ref = refs[EP2_ID]["l1"]
            self.assertEqual(new_row["inputs"], [{"product_id": l1_ref["product_id"], "sha256": l1_ref["sha256"]}])

            new_path = store.episode_product_path(self.settings, EP2_ID, "l2_path", new_ver, ext)
            self.assertEqual(new_row["path"], str(new_path))
            self.assertTrue(new_path.is_file())
            self.assertEqual(store.sha256_file(new_path), new_row["sha256"])
            self.assertEqual(Path(old["path"]).read_bytes(), old_bytes)

            # Episode 1 is not touched until it is reprocessed itself; a second pass then builds nothing.
            self.assertEqual(self.catalog.current(EP1_ID, "l2_path")["algorithm_version"], ver)
            again = pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)
            self.assertEqual(again["built"], [])
            self.assertEqual(again["up_to_date"], list(products.L2_TYPES))
        self.assertEqual(self.catalog.findings(), [])
        self.assertEqual(len(self.catalog.products(EP2_ID, "l2_path")), 2)

    def test_an_archive_that_reads_differently_is_reported_and_the_original_kept(self):
        refs = self.forward()
        l1_ref = refs[EP2_ID]["l1"]
        original = Path(l1_ref["path"])
        original_bytes = original.read_bytes()
        health = self.archive.parameters(["HEALTH"], EP2_WINDOW[0], EP2_WINDOW[1] + 1)["HEALTH"]
        t_last, v_last = health[-1][0], health[-1][2]
        changed = PerturbedArchive(self.archive, "HEALTH", t_last, v_last + 7)

        report = pipeline.reprocess(self.settings, changed, self.catalog, EP2_ID, run_id="re2")

        self.assertTrue(report["l1"].startswith("NOT reproduced"), report["l1"])
        rebuilt = list(original.parent.glob("*rebuilt-*.json"))
        self.assertEqual(len(rebuilt), 1)
        rebuilt_sha = store.sha256_file(rebuilt[0])
        ver = products.version("l1_episode")
        self.assertEqual(rebuilt[0], store.episode_product_path(self.settings, EP2_ID, "l1_episode", ver,
                                                                "rebuilt-%s.json" % rebuilt_sha[:12]))
        self.assertNotEqual(rebuilt_sha, l1_ref["sha256"])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(store.sha256_file(original), l1_ref["sha256"])
        row = self.catalog.product(l1_ref["product_id"])
        self.assertEqual((row["path"], row["sha256"], row["is_current"]), (str(original), l1_ref["sha256"], True))

        found = [f for f in self.catalog.findings() if f["kind"] == "l1_not_reproduced"]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["episode_id"], found[0]["product_id"]), (EP2_ID, l1_ref["product_id"]))
        self.assertEqual(found[0]["detail"], {"cataloged_sha256": l1_ref["sha256"], "rebuilt_sha256": rebuilt_sha,
                                              "rebuilt_path": str(rebuilt[0]), "run": "re2"})

        # The L2s are rebuilt from what the archive says today: every type, from the rebuilt L1.
        self.assertEqual(report["built"], ["%s/%s@%s" % (EP2_ID, t, products.version(t)) for t in products.L2_TYPES])
        self.assertEqual(report["up_to_date"], [])
        summary = self.catalog.current(EP2_ID, "l2_summary")
        self.assertEqual(store.read_json(summary["path"])["health"]["end"], v_last + 7)
        self.assertEqual([i["sha256"] for i in summary["inputs"]], [rebuilt_sha])
        # Episode 1 is untouched.
        self.assertEqual(self.catalog.product(refs[EP1_ID]["l1"]["product_id"])["sha256"], refs[EP1_ID]["l1"]["sha256"])

    # Regression test for a bug the tests found (now fixed): pipeline.reprocess (pipeline.py:236) rebuilds every L2 whenever L1 was not "reproduced",
    # including after a deliberate l1_episode version bump, and l2() (pipeline.py:175) names the product
    # "<episode>/<type>@<L2 version>" with nothing of the L1 it came from. So the rebuilt L2s land on the ids and
    # paths of the existing ones: l2_summary-1.0.0.json etc. are overwritten in place with documents built from
    # the new L1 (store.py: "A version never overwrites another"), and the catalog logs a checksum_changed finding
    # for each, whose documented meaning ("the same algorithm version produced different bytes from what should
    # be the same input") is false here: a planned change raises three reproducibility alarms.
    def test_an_l1_version_bump_overwrites_nothing_and_raises_no_false_alarm(self):
        self.forward()
        folder = store.episode_product_path(self.settings, EP2_ID, "l1_episode", "0", "json").parent
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        level, ver, ext, media = products.ALGORITHMS["l1_episode"]
        with mock.patch.dict(products.ALGORITHMS, {"l1_episode": (level, bumped(ver), ext, media)}):
            report = pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)
        self.assertTrue(report["l1"].startswith("built at new version"), report["l1"])
        self.assertTrue((folder / ("l1_episode-%s.json" % bumped(ver))).is_file())
        self.assertEqual(sorted(n for n, b in before.items() if (folder / n).read_bytes() != b), [])
        self.assertEqual([f for f in self.catalog.findings() if f["kind"] == "checksum_changed"], [])


class TestLineage(PipelineCase):
    def test_one_lineage_line_per_new_or_changed_product(self):
        refs = self.forward()
        pipeline.rollup(self.settings, self.catalog)
        pipeline.l1(self.settings, self.archive, self.catalog, refs[EP2_ID]["located"])     # unchanged: no line
        pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)                # builds nothing
        level, ver, ext, media = products.ALGORITHMS["l2_path"]
        with mock.patch.dict(products.ALGORITHMS, {"l2_path": (level, bumped(ver), ext, media)}):
            pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)            # one new l2_path
        health = self.archive.parameters(["HEALTH"], EP2_WINDOW[0], EP2_WINDOW[1] + 1)["HEALTH"]
        changed = PerturbedArchive(self.archive, "HEALTH", health[-1][0], health[-1][2] + 7)
        pipeline.reprocess(self.settings, changed, self.catalog, EP1_ID)                     # reproduces
        pipeline.reprocess(self.settings, changed, self.catalog, EP2_ID)                     # does not
        # Same product id, different bytes ("changed"): an L2 built again from an L1 that now reads differently.
        l1_ref = refs[EP1_ID]["l1"]
        doc = store.read_json(l1_ref["path"])
        doc["context"]["map"] = "E1M2"
        data = store.canonical_json(doc)
        other = store.write_atomic(self.tmp / "other_l1.json", data)
        ref = pipeline.l2(self.settings, self.catalog, dict(l1_ref, path=str(other), sha256=store.sha256(data)),
                          "l2_summary")
        self.assertEqual(ref["status"], "changed")

        written = [r for r in self.registered if r["status"] != "unchanged"]
        self.assertTrue({"new", "unchanged", "changed"} <= {r["status"] for r in self.registered})
        lines = self.lineage_lines()
        self.assertEqual(len(lines), len(written))
        got = [(e["outputs"][0]["name"], e["outputs"][0]["facets"]["version"]["datasetVersion"],
                e["run"]["facets"]["doomsat_sds"]["status"], e["job"]["name"]) for e in lines]
        self.assertEqual(got, [(r["product_id"], r["sha256"], r["status"], r["product_type"]) for r in written])
        # Every catalog row was announced once when new, and once more each time its bytes changed.
        changed_findings = [f for f in self.catalog.findings() if f["kind"] == "checksum_changed"]
        self.assertEqual(len(lines), len(self.catalog.products()) + len(changed_findings))
        for e in lines:
            self.assertEqual(e["eventType"], "COMPLETE")
        l2_line = next(e for e in lines if e["outputs"][0]["name"] == refs[EP2_ID]["l2"]["l2_summary"]["product_id"])
        self.assertEqual(l2_line["inputs"][0]["name"], refs[EP2_ID]["l1"]["product_id"])


if __name__ == "__main__":
    unittest.main()


class TestConfirmedClosures(unittest.TestCase):
    """An inferred closure is acted on only once the samples bear it out."""

    def stub(self, tic, episode):
        return StubArchive([], {"TIC": [(t, None, v) for t, v in tic], "EPISODE": [(t, None, v) for t, v in episode]})

    def test_a_payload_restart_is_confirmed_by_tic_falling_back(self):
        ep = episodes.ClosedEpisode(3, 100_000, "PayloadConnected", "interrupted", None, inferred=True)
        restart = self.stub([(90_000, 5_000), (99_000, 5_030), (101_000, 4), (102_000, 7)],
                            [(90_000, 3), (99_000, 3), (101_000, 1), (102_000, 1)])
        self.assertTrue(pipeline.confirmed(restart, ep))

    def test_a_socket_drop_that_carries_on_is_not_a_closure(self):
        ep = episodes.ClosedEpisode(3, 100_000, "PayloadConnected", "interrupted", None, inferred=True)
        drop = self.stub([(90_000, 5_000), (99_000, 5_030), (101_000, 5_090)],
                         [(90_000, 3), (99_000, 3), (101_000, 3)])
        self.assertFalse(pipeline.confirmed(drop, ep))

    def test_a_restart_with_nothing_after_it_ends_the_game(self):
        ep = episodes.ClosedEpisode(3, 100_000, "PayloadConnected", "interrupted", None, inferred=True)
        self.assertTrue(pipeline.confirmed(self.stub([(99_000, 5_030)], [(99_000, 3)]), ep))

    def test_an_inferred_reset_needs_the_episode_in_progress(self):
        ep = episodes.ClosedEpisode(4, 100_000, "EpisodeStarted", "reset", None, inferred=True)
        self.assertTrue(pipeline.confirmed(self.stub([], [(95_000, 4)]), ep))
        self.assertFalse(pipeline.confirmed(self.stub([], [(95_000, 2)]), ep))


class TestReprocessRepairs(PipelineCase):
    def test_l2s_built_from_an_l1_that_did_not_reproduce_are_rebuilt_once_it_does(self):
        # Review finding: an L2 rebuilt from a rebuilt L1 (the archive misread once) kept the L1's product id, so
        # once the archive read correctly again the L2 counted as up to date and never matched its L1 again.
        refs = self.forward()
        health = self.archive.parameters(["HEALTH"], EP2_WINDOW[0], EP2_WINDOW[1] + 1)["HEALTH"]
        misread = PerturbedArchive(self.archive, "HEALTH", health[-1][0], health[-1][2] + 7)
        bad = pipeline.reprocess(self.settings, misread, self.catalog, EP2_ID)
        self.assertTrue(bad["l1"].startswith("NOT"), bad["l1"])
        good = pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)
        self.assertTrue(good["l1"].startswith("reproduced"), good["l1"])
        # Only the summary reads HEALTH: the path and link statistics rebuilt from the misread L1 came out
        # byte-identical, were registered as unchanged and kept their original input, so only the summary is repaired.
        self.assertEqual(good["built"], ["%s/l2_summary@%s" % (EP2_ID, products.version("l2_summary"))])
        l1_sha = refs[EP2_ID]["l1"]["sha256"]
        for t in products.L2_TYPES:
            row = self.catalog.current(EP2_ID, t)
            self.assertEqual([i["sha256"] for i in row["inputs"]], [l1_sha], t)
            self.assertEqual(row["sha256"], refs[EP2_ID]["l2"][t]["sha256"], t)   # the forward bytes, exactly
        again = pipeline.reprocess(self.settings, self.archive, self.catalog, EP2_ID)
        self.assertEqual((again["built"], again["up_to_date"]), ([], list(products.L2_TYPES)))

    def test_reprocessing_asks_the_archive_what_the_forward_run_asked(self):
        # Review finding: a renamed host (Yamcs serverId) or another Yamcs address made every episode look
        # unreproducible, because the rebuild named the archive differently from the forward run.
        self.forward()
        renamed = PerturbedArchive(self.archive, "nothing", 0, None)
        renamed.server_id = lambda: "another-host"
        self.settings.yamcs = "127.0.0.1:8090"
        report = pipeline.reprocess(self.settings, renamed, self.catalog, EP2_ID)
        self.assertTrue(report["l1"].startswith("reproduced"), report["l1"])


class TestForwardRunIds(unittest.TestCase):
    """A failed forward run used to block its episode for good: the watcher kept triggering the same run id, which
    Airflow skips because it exists, whatever its state."""

    def test_new_running_done_failed_and_given_up(self):
        runs = {}
        state = runs.get
        self.assertEqual(pipeline.forward_run_id("E", state), ("fwd__E", "new"))
        for st in ("queued", "running", "success"):
            runs["fwd__E"] = st
            self.assertEqual(pipeline.forward_run_id("E", state)[0], None, st)
        runs["fwd__E"] = "failed"
        self.assertEqual(pipeline.forward_run_id("E", state), ("fwd__E__retry1", "retry 1"))
        runs["fwd__E__retry1"] = "running"
        self.assertIsNone(pipeline.forward_run_id("E", state)[0])
        runs["fwd__E__retry1"] = "failed"
        self.assertEqual(pipeline.forward_run_id("E", state)[0], "fwd__E__retry2")
        runs["fwd__E__retry2"] = "failed"
        run_id, why = pipeline.forward_run_id("E", state)
        self.assertIsNone(run_id)
        self.assertTrue(why.startswith("gave up"), why)
