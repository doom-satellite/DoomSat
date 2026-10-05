"""The products, built from a real recording of the archive: episode 2 of two_deaths.json.gz, which ran 33.65 s and
ended in a PlayerDied at 00:46:47.184Z.

L1 is the science record. Every L2 is computed from L1 alone, and reprocessing rebuilds L1 from the archive and
compares its sha256 with the cataloged one, so L1 must (1) carry every archived sample of the episode window,
(2) put commands on the telemetry's time axis with the measured clock offset (TM time runs about 0.95 s ahead
of the ground clock that stamps command history), and (3) serialise to the same bytes every time. The L2
builders must agree with the L1 table they read, and any change to what a builder writes has to come with a
version bump (products.ALGORITHMS): reprocess treats a product at the current version as up to date, so a
silent edit would leave stale products current. Assertions read versions from ALGORITHMS; only the checksum
table is keyed by them, and an entry stops applying (the check skips) when its version is bumped.
"""
import copy
import io
import json
import math
import os
import random
import struct
import sys
import tempfile
import unittest
import zlib
from collections import Counter
from pathlib import Path

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import archive, config, episodes, pipeline, png, products  # noqa: E402
from doomsat_sds.store import canonical_json, sha256  # noqa: E402

try:
    import PIL.Image
    HAVE_PIL = True
except ImportError:          # ground/.venv has no Pillow; the SDS venv does
    HAVE_PIL = False

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
FIXTURE_START, FIXTURE_STOP = 1791074769000, 1791074812000
WINDOW = (1791074773533, 1791074807183)         # episode 2's window as the live pipeline computed it
PLAYER_DIED_2 = 1791074807184
INTENT_SET = config.EVENT_PREFIX + "IntentSet"
PLAYER_DIED = config.EVENT_PREFIX + "PlayerDied"
EPISODE_STARTED = config.EVENT_PREFIX + "EpisodeStarted"
CONTEXT = {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "pilot_mode": "system-one=code system-two=none",
           "repo_commit": "test", "level_set": "dev", "sources": {}}

# sha256 of products built from this fixture with CONTEXT, keyed by the algorithm versions each depends on. A key
# that is missing (a version was bumped) skips the check until the new checksum is recorded here. A key that is
# present and disagrees means a builder's output changed without a version bump.
GOLDEN = {
    "l1_episode@1.0.0": "8eb8fd1c44758b218706392a43f5f5e442acadee519dd9060602e237c4ae40a6",
    "l1_episode@1.0.0 l2_summary@1.0.0": "21d94d34782ba94c57885d758b79ffebdc34a5d50ebb5d2a82b2f1b8e27a2be7",
    "l1_episode@1.0.0 l2_linkstats@1.0.0": "8563c7802764dec1c86d29e44dd590a111c14058c1073e819a1de55bff93f2c8",
    # the path image's decoded scanlines, not its file bytes: deflate output may differ between zlib builds
    "l1_episode@1.0.0 l2_path@2.0.0": "e554183c3bedc797ce60fd875bbb22f2b6154b31fb018bdea7880af52c01727d",
    # the 1.1.0 baseline: L1 keeps same-time samples (8 in this episode that 1.0.0 dropped)
    "l1_episode@1.1.0": "1c2ebe980a580af513c20b884d5919c64aba2a4b565ef6eeedf5adab34291cc4",
    "l1_episode@1.1.0 l2_summary@1.1.0": "20be54b46ffa127fe4c5d7f8881cf288189c09bf9ec430d3bd74353f45222a13",
    "l1_episode@1.1.0 l2_linkstats@1.1.0": "d8bc1b3e8a2b0643fd931d59d927c2a3a961e67f09548df11b8ea8f34895a76d",
    "l1_episode@1.1.0 l2_path@2.1.0": "f3498956a1481c8f9361af4b285d8b3c5626ea5300793b048274df334e542e56",
    # 1.2.0: the ground system's own commands counted apart
    "l1_episode@1.1.0 l2_summary@1.2.0": "3b4cea14371e756738ff410a2baf391450e33c1b91e3ab06694ecf0f761b90fc",
    "l1_episode@1.1.0 l2_linkstats@1.2.0": "3a369e98405513eb3163929b2e1c076a5b96d3fe589711e1486bc27a8593a60f",
    # l2_summary 1.3.0 carries pwad and wad_loads (null here: CONTEXT predates them), l3_rollup 1.1.0 keys a level by
    # its patch WAD too; the rollup is over this one summary
    "l1_episode@1.1.0 l2_summary@1.3.0": "93e5ce4e061280e8fe04e2dde4baf5916f096ce20e9108f41cb9ea072b509a4a",
    "l1_episode@1.1.0 l2_summary@1.3.0 l3_rollup@1.1.0":
        "c61be5d9fa2d556c227b81e67c34ff8c36e3f02f59994b1cb11d15f057be89d7",
    # l2_path 2.2.0 gave wad_switch its own end colour; this died episode differs from 2.1.0 only in the version shown
    "l1_episode@1.1.0 l2_path@2.2.0": "5cf3affc40faa34d6c9cdcb5110ad339e020d088433d858da1a05020b90bc936",
}


def golden_key(*product_types: str) -> str:
    return " ".join("%s@%s" % (t, products.version(t)) for t in product_types)


def episode_2(arch) -> episodes.ClosedEpisode:
    closed = episodes.closed_episodes(arch.events(FIXTURE_START, FIXTURE_STOP))
    return [e for e in closed if e.number == 2][0]


def build_l1(context=CONTEXT, home=None):
    """(doc, bytes) for episode 2, the way the forward pipeline builds it: closed episode conf plus window."""
    arch = archive.RecordedArchive(FIXTURE)
    located = dict(episode_2(arch).as_conf(), window=list(WINDOW))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / (home or "a")
        settings = config.Settings(home=root / "sds", doomsat_home=root / "doom")
        return pipeline.make_l1(settings, arch, located, copy.deepcopy(context))


def idat_raw(data: bytes) -> bytes:
    """The inflated scanlines of a PNG (filter byte + RGB per row). test_sds_png checks the format itself."""
    pos, idat = 8, b""
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        if data[pos + 4:pos + 8] == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    return zlib.decompress(idat)


def pixels(data: bytes) -> dict:
    """{(x, y): (r, g, b)} for every pixel of the PNG."""
    w, h = png.png_size(data)
    raw, stride = idat_raw(data), 1 + 3 * w
    return {(x, y): tuple(raw[y * stride + 1 + 3 * x:y * stride + 4 + 3 * x]) for y in range(h) for x in range(w)}


def colours(data: bytes) -> Counter:
    """How many pixels of each (r, g, b) the PNG has."""
    return Counter(pixels(data).values())


def centroid(px: dict, rgb: tuple) -> tuple:
    pts = [p for p, v in px.items() if v == rgb]
    return (round(sum(x for x, _ in pts) / len(pts)), round(sum(y for _, y in pts) / len(pts)))


def with_positions(l1: dict, positions: list) -> dict:
    """A copy of an L1 whose only position samples are `positions`, on its first rows, in order."""
    doc = strip_positions(l1)
    cols = doc["telemetry"]["columns"]
    for r, (x, y) in zip(doc["telemetry"]["rows"], positions):
        r[cols.index("POS_X")], r[cols.index("POS_Y")] = x, y
    return doc


def strip_positions(l1: dict) -> dict:
    """A copy of an L1 whose POS_X and POS_Y never updated."""
    doc = copy.deepcopy(l1)
    cols = doc["telemetry"]["columns"]
    ix, iy = cols.index("POS_X"), cols.index("POS_Y")
    for r in doc["telemetry"]["rows"]:
        r[ix] = r[iy] = None
    return doc


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arch = archive.RecordedArchive(FIXTURE)
        cls.ep = episode_2(cls.arch)
        cls.doc, cls.data = build_l1()
        cls.start, cls.end = WINDOW
        names = list(config.SCIENCE) + list(config.LINK) + list(archive.link_parameters(cls.arch.server_id()))
        cls.samples = cls.arch.parameters(names, cls.start, cls.end + 1)     # everything in [start, end]

    def doc_copy(self) -> dict:
        return copy.deepcopy(self.doc)


class TestL1(FixtureCase):
    def test_window_is_what_locate_finds(self):
        located = pipeline.locate_episode(self.arch, self.ep.as_conf())
        self.assertEqual(tuple(located["window"]), WINDOW)

    def test_product_header(self):
        level, ver, _, _ = products.ALGORITHMS["l1_episode"]
        self.assertEqual(self.doc["product"], {"type": "l1_episode", "level": level, "version": ver})
        self.assertEqual(level, "L1")

    def test_episode_fields(self):
        e = self.doc["episode"]
        self.assertEqual(e["number"], 2)
        self.assertEqual(e["outcome"], "died")
        self.assertEqual(e["closing_event"], "PlayerDied")
        self.assertEqual(e["id"], episodes.episode_id(PLAYER_DIED_2, 2))
        self.assertEqual(e["id"], "20261004T004647Z-e0002")
        self.assertEqual(e["closing_utc"], "2026-10-04T00:46:47.184Z")
        self.assertEqual(e["start_event_utc"], "2026-10-04T00:46:13.533Z")
        self.assertIs(e["inferred_close"], False)
        self.assertEqual((e["start_ms"], e["end_ms"]), WINDOW)
        self.assertEqual((e["start_utc"], e["end_utc"]), (archive.iso(WINDOW[0]), archive.iso(WINDOW[1])))
        self.assertEqual(e["duration_s"], 33.65)

    def test_context_is_carried_unchanged(self):
        self.assertEqual(self.doc["context"], CONTEXT)

    def test_telemetry_columns_and_row_order(self):
        tel = self.doc["telemetry"]
        self.assertEqual(tel["columns"], ["t_ms", *config.SCIENCE])
        ts = [r[0] for r in tel["rows"]]
        self.assertTrue(ts, "no telemetry rows")
        # non-decreasing: two samples of one channel can share an F' time tag, and each gets its own row
        self.assertTrue(all(a <= b for a, b in zip(ts, ts[1:])), "rows must be in time order")
        self.assertTrue(all(self.start <= t <= self.end for t in ts))
        self.assertEqual((ts[0], ts[-1]), WINDOW, "the window is the first and last EPISODE == 2 sample")
        self.assertTrue(all(len(r) == len(tel["columns"]) for r in tel["rows"]))
        self.assertEqual(self.doc["counts"]["telemetry_rows"], len(tel["rows"]))

    def test_every_row_has_a_value_and_every_cell_is_an_archived_sample(self):
        archived = {n: {(t, v) for t, _, v in self.samples[n]} for n in config.SCIENCE}
        for r in self.doc["telemetry"]["rows"]:
            self.assertTrue(any(v is not None for v in r[1:]), "an all-null row at %d" % r[0])
        for n in config.SCIENCE:
            for t, v in products.column(self.doc, n):
                self.assertIn((t, v), archived[n], "%s at %d is not an archived sample" % (n, t))

    def test_counts_are_the_archive_samples_in_the_window(self):
        for n in config.SCIENCE:
            self.assertEqual(self.doc["counts"]["samples"][n], len(self.samples[n]), n)

    def test_every_archived_time_tag_has_a_cell(self):
        # The part of "lossless" that holds today, and must go on holding whatever fix test_telemetry_is_lossless
        # gets: no time at which a channel was archived is missing from its column. A dropped row or channel
        # fails here even while the same-time-tag collisions keep the full check an expected failure.
        ground = archive.link_parameters(self.arch.server_id())[0]
        tables = [("telemetry", n, n) for n in config.SCIENCE] + [("housekeeping", n, n) for n in config.LINK]
        tables.append(("housekeeping", products.GROUND_LINK, ground))
        for table, column, source in tables:
            archived = {t for t, _, _ in self.samples[source]}
            self.assertTrue(archived, source)
            self.assertEqual({t for t, _ in products.column(self.doc, column, table)}, archived, column)

    def test_telemetry_is_lossless(self):
        # Regression test for a bug the tests found (now fixed): products._table keys rows by TM time alone, so when the archive holds two samples of one
        # channel with the same generation time (they arrive ~50 ms apart) the later overwrites the earlier. In
        # this window HEALTH has 5 such pairs and POS_X, POS_Y and KILLS one each (at 1791074778183, POS_X is both
        # -80.736618 and -78.7017975), so the table has 368 HEALTH cells where counts.samples and the archive say
        # 373, contradicting the docstring ("every archived sample in the window is in exactly one cell").
        for n in config.SCIENCE:
            cells = len(products.column(self.doc, n))
            self.assertEqual(cells, self.doc["counts"]["samples"][n], n)
            self.assertEqual(cells, len(self.samples[n]), n)

    def test_housekeeping_columns_and_lossless(self):
        hk = self.doc["housekeeping"]
        self.assertEqual(hk["columns"], ["t_ms", *config.LINK, products.GROUND_LINK])
        ground = archive.link_parameters(self.arch.server_id())[0]
        for n in config.LINK:
            self.assertEqual(len(products.column(self.doc, n, "housekeeping")), len(self.samples[n]), n)
        self.assertEqual(len(products.column(self.doc, products.GROUND_LINK, "housekeeping")),
                         len(self.samples[ground]))
        self.assertTrue(products.column(self.doc, products.GROUND_LINK, "housekeeping"))
        ts = [r[0] for r in hk["rows"]]
        self.assertTrue(all(a <= b for a, b in zip(ts, ts[1:])))
        self.assertTrue(all(self.start <= t <= self.end for t in ts))

    def test_clock_offset(self):
        clock = self.doc["clock"]
        off = clock["tm_minus_ground_ms"]
        self.assertTrue(800 <= off <= 1100, off)                 # leap-second skew plus link latency
        # the median of this recording's 3723 differences, and its spread (a mean or a sign slip moves these)
        self.assertEqual((off, clock["p05_ms"], clock["p95_ms"]), (943, 899, 991))
        # measured from every science sample in the window (each has a reception time)
        self.assertEqual(clock["samples"], sum(len(self.samples[n]) for n in config.SCIENCE))

    def test_commands_are_placed_on_the_tm_axis_and_complete(self):
        off = self.doc["clock"]["tm_minus_ground_ms"]
        cmds = self.doc["commands"]
        self.assertTrue(cmds)
        for c in cmds:
            self.assertEqual(c["t_tm_ms"], c["t_ground_ms"] + off)
            self.assertTrue(self.start <= c["t_tm_ms"] <= self.end, c)
        every = self.arch.commands(FIXTURE_START, FIXTURE_STOP)
        expected = sorted(c["id"] for c in every if self.start <= c["t"] + off <= self.end)
        self.assertEqual(sorted(c["id"] for c in cmds), expected)
        self.assertLess(len(cmds), len(every), "commands of episode 1 and 3 must stay out")
        self.assertEqual(cmds, sorted(cmds, key=lambda c: (c["t_tm_ms"], c["id"])))
        self.assertEqual(self.doc["counts"]["commands"], len(cmds))
        by_id = {c["id"]: c for c in every}
        for c in cmds:
            self.assertEqual(c["args"], by_id[c["id"]]["args"])
            self.assertEqual(c["name"], by_id[c["id"]]["name"])

    def test_events_drop_intentset_and_keep_the_death(self):
        types = [e["type"] for e in self.doc["events"]]
        self.assertNotIn(INTENT_SET, types)
        self.assertEqual(types, [EPISODE_STARTED, PLAYER_DIED])
        died = self.doc["events"][-1]
        self.assertEqual(died["t_ms"], PLAYER_DIED_2)            # 1 ms after the window's last sample
        self.assertEqual(died["extra"]["episode"], "2")
        self.assertEqual(self.doc["counts"]["events"], 2)

    def test_build_l1_drops_intentset_itself(self):
        # make_l1 already asks the archive for L1_EVENTS only; build_l1 must hold the line on its own too.
        evs = self.arch.events(self.start, self.end + episodes.END_PAD_MS + 1)
        self.assertTrue(any(e["type"] == INTENT_SET for e in evs))
        science = self.arch.parameters(config.SCIENCE, self.start, self.end + 1)
        doc = products.build_l1(self.ep, WINDOW, science, {}, [], evs, {}, [])
        self.assertEqual([e["type"] for e in doc["events"]], [EPISODE_STARTED, PLAYER_DIED])

    def test_archive_return_order_does_not_change_the_bytes(self):
        # Yamcs pages command history and events in an order of its choosing; L1 sorts them, so the record and the
        # checksum reprocess compares do not depend on it. Each channel's series stays in time order (the readers
        # sort it); same-time samples within a series are the subject of test_telemetry_is_lossless.
        ground = archive.link_parameters(self.arch.server_id())[0]
        science = {n: self.samples[n] for n in config.SCIENCE}
        link = {n: self.samples[n] for n in config.LINK}
        link[products.GROUND_LINK] = self.samples[ground]
        commands = self.arch.commands(FIXTURE_START, FIXTURE_STOP)
        events = self.arch.events(self.start, self.end + episodes.END_PAD_MS + 1)
        doc = products.build_l1(self.ep, WINDOW, science, link, commands, events, CONTEXT, [])
        self.assertEqual(doc["commands"], self.doc["commands"])
        rng = random.Random(7)
        commands, events = commands[:], events[:]
        rng.shuffle(commands)
        rng.shuffle(events)
        shuffled = products.build_l1(self.ep, WINDOW, dict(reversed(list(science.items()))),
                                     dict(reversed(list(link.items()))), commands, events, CONTEXT, [])
        self.assertEqual(canonical_json(shuffled), canonical_json(doc))

    def test_inputs_name_the_archive_reads(self):
        kinds = [i["kind"] for i in self.doc["inputs"]]
        self.assertEqual(kinds, ["parameters", "command_history", "events"])
        self.assertIn(archive.link_parameters("vm")[0], self.doc["inputs"][0]["names"])

    def test_deterministic_bytes(self):
        doc2, data2 = build_l1(home="elsewhere")                 # fresh archive, other Settings paths
        self.assertEqual(data2, self.data)
        self.assertEqual(canonical_json(doc2), self.data)
        self.assertEqual(canonical_json(self.doc), self.data)
        self.assertEqual(sha256(data2), sha256(self.data))
        self.assertNotIn(b"elsewhere", data2)
        # the file read back re-serialises to itself, so a checksum of the stored product is the checksum
        self.assertEqual(canonical_json(json.loads(self.data)), self.data)

    def test_sha256_matches_the_recorded_checksum(self):
        key = golden_key("l1_episode")
        if key not in GOLDEN:
            self.skipTest("no recorded checksum for %s yet; add it to GOLDEN" % key)
        self.assertEqual(sha256(self.data), GOLDEN[key],
                         "L1 bytes changed but l1_episode is still %s: bump its version in products.ALGORITHMS "
                         "(every cataloged L1 at this version would stop reproducing)" % products.version("l1_episode"))


class TestBuildL1ByHand(unittest.TestCase):
    """build_l1 on a few hand-made samples, where every boundary is known."""

    OFF = 950
    WIN = (10_000, 20_000)

    def series(self, points):
        return [(t, t - self.OFF, v) for t, v in points]

    def build(self, science, commands=(), events=(), link=None):
        ep = episodes.ClosedEpisode(1, 20_001, "PlayerDied", "died", 10_000)
        return products.build_l1(ep, self.WIN, science, link or {}, list(commands), list(events), {"wad": "x"}, [])

    def test_window_and_command_boundaries_are_inclusive(self):
        science = {"TIC": self.series([(9_999, 1), (10_000, 2), (15_000, 3), (20_000, 4), (20_001, 5)])}
        cmds = [{"t": t, "id": "c%d" % t, "name": config.NAMESPACE + "INTENT", "args": {}} for t in
                (9_049, 9_050, 19_050, 19_051)]
        doc = self.build(science, commands=cmds)
        self.assertEqual(doc["clock"]["tm_minus_ground_ms"], self.OFF)
        self.assertEqual([r[0] for r in doc["telemetry"]["rows"]], [10_000, 15_000, 20_000])
        self.assertEqual([c["t_ground_ms"] for c in doc["commands"]], [9_050, 19_050])
        self.assertEqual([c["t_tm_ms"] for c in doc["commands"]], [10_000, 20_000])
        self.assertEqual(doc["commands"][0]["acks"], {})

    def test_event_filter(self):
        ev = lambda t, name: {"t": t, "type": name, "message": name, "extra": {}}
        events = [ev(9_999, PLAYER_DIED), ev(10_000, EPISODE_STARTED), ev(12_000, INTENT_SET),
                  ev(13_000, "Other.component.Thing"), ev(20_001, PLAYER_DIED),
                  ev(20_000 + episodes.END_PAD_MS, config.EVENT_PREFIX + "GoalSet"),
                  ev(20_001 + episodes.END_PAD_MS, config.EVENT_PREFIX + "GoalSet")]
        doc = self.build({"TIC": self.series([(10_000, 1)])}, events=events)
        self.assertEqual([(e["t_ms"], e["type"]) for e in doc["events"]],
                         [(10_000, EPISODE_STARTED), (20_001, PLAYER_DIED),
                          (20_000 + episodes.END_PAD_MS, config.EVENT_PREFIX + "GoalSet")])

    def test_cells_are_null_where_a_channel_did_not_update(self):
        science = {"POS_X": self.series([(11_000, 1.5)]), "TIC": self.series([(11_000, 7), (12_000, 10)])}
        doc = self.build(science)
        cols = doc["telemetry"]["columns"]
        rows = {r[0]: dict(zip(cols, r)) for r in doc["telemetry"]["rows"]}
        self.assertEqual(rows[11_000]["POS_X"], 1.5)
        self.assertIsNone(rows[12_000]["POS_X"])
        self.assertEqual(rows[12_000]["TIC"], 10)
        self.assertIsNone(rows[11_000]["HEALTH"])

    def test_two_samples_with_one_time_tag_both_survive(self):
        # Regression test for a bug the tests found (now fixed): products._table (rows.setdefault(t, ...)[i] = v) overwrites the first of two same-time
        # samples; the minimal form of TestL1.test_telemetry_is_lossless. Expected 2 POS_X cells, as
        # counts.samples says; actual 1.
        science = {"POS_X": [(15_000, 14_050, 1.0), (15_000, 14_060, 2.0)]}
        doc = self.build(science)
        self.assertEqual(doc["counts"]["samples"]["POS_X"], 2)
        self.assertEqual(sorted(v for _, v in products.column(doc, "POS_X")), [1.0, 2.0])

    def test_no_reception_times_means_no_offset(self):
        doc = self.build({"TIC": [(15_000, None, 1)]})
        self.assertEqual(doc["clock"], {"tm_minus_ground_ms": 0, "p05_ms": None, "p95_ms": None, "samples": 0})


class TestSummary(FixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.summary = products.build_summary(cls.doc)

    def test_header_and_identity(self):
        s = self.summary
        level, ver, _, _ = products.ALGORITHMS["l2_summary"]
        self.assertEqual(s["product"], {"type": "l2_summary", "level": level, "version": ver})
        self.assertIn("l2_summary", products.L2_TYPES)
        self.assertEqual(s["episode_id"], self.doc["episode"]["id"])
        self.assertEqual(s["number"], 2)
        self.assertEqual(s["outcome"], "died")
        self.assertEqual(s["duration_s"], 33.65)
        l1_id = "%s/l1_episode@%s" % (self.doc["episode"]["id"], products.version("l1_episode"))
        self.assertEqual(s["inputs"], [l1_id])
        for k in ("wad", "map", "skill", "pilot_mode"):
            self.assertEqual(s[k], CONTEXT[k])
        self.assertEqual((s["pwad"], s["wad_loads"]), (None, None))      # a context from before main has neither

    def test_the_patch_wad_and_the_switches_come_from_the_context(self):
        doc = self.doc_copy()
        doc["context"].update(wad="freedoom2.wad", pwad="basic.wad", wad_loads=2, map="MAP01")
        s = products.build_summary(doc)
        self.assertEqual((s["wad"], s["pwad"], s["wad_loads"], s["map"]), ("freedoom2.wad", "basic.wad", 2, "MAP01"))

    def test_values_agree_with_the_table(self):
        s = self.summary
        col = lambda n: [v for _, v in products.column(self.doc, n)]
        self.assertEqual(s["kills"], max(col("KILLS")))
        self.assertEqual(s["kills"], 1)
        self.assertEqual(s["explored_cells"], col("EXPLORED_CELLS")[-1])
        self.assertEqual(s["explored_cells"], 137)
        self.assertEqual(s["level"], col("LEVEL")[-1])
        health = col("HEALTH")
        self.assertEqual(s["health"], {"start": health[0], "min": min(health), "end": health[-1]})
        self.assertEqual(s["health"], {"start": 100, "min": 4, "end": 4})
        self.assertIs(col("DEAD")[-1], True)
        self.assertEqual(s["commands"], dict(Counter(c["name"].rsplit("/", 1)[-1] for c in self.doc["commands"])))
        self.assertEqual(s["commands"], {"INTENT": len(self.doc["commands"])})

    def test_tics_and_the_death_tic(self):
        tic = products.column(self.doc, "TIC")
        self.assertEqual(self.summary["tics"], tic[-1][1] - tic[0][1])
        died = [e for e in self.doc["events"] if e["type"] == PLAYER_DIED][0]
        # F´ makes PlayerDied from the final STATUS, so the last TIC on the ground is the tic the event reports,
        # one millisecond before the event's own time tag.
        self.assertEqual(tic[-1][1], int(died["extra"]["tic"]))
        self.assertEqual(tic[-1][1], 1180)
        self.assertTrue(0 <= died["t_ms"] - tic[-1][0] <= episodes.END_PAD_MS)
        self.assertEqual(self.summary["tics"], 1176)

    def test_distance_is_the_walked_path(self):
        pts = products.path_points(self.doc)
        self.assertGreater(len(pts), 100)
        self.assertEqual(self.summary["path_points"], len(pts))
        walked = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts, pts[1:]))
        self.assertEqual(self.summary["distance_units"], round(walked, 1))
        self.assertGreater(self.summary["distance_units"], 0)
        # consecutive points differ; each is a position the table held at that time
        self.assertTrue(all((a[1], a[2]) != (b[1], b[2]) for a, b in zip(pts, pts[1:])))

    def test_built_from_the_stored_l1_too(self):
        # pipeline.l2 reads L1 back from disk: the parsed document must give the same L2 bytes.
        stored = json.loads(self.data)
        self.assertEqual(canonical_json(products.build_summary(stored)), canonical_json(self.summary))
        self.assertEqual(canonical_json(products.build_linkstats(stored)),
                         canonical_json(products.build_linkstats(self.doc)))
        self.assertEqual(products.build_path_png(stored), products.build_path_png(self.doc))

    def test_l2_builders_leave_the_l1_unchanged(self):
        # "Pure functions: inputs in, a document or an image out." Any L2 must be rebuildable from the L1 as
        # stored, so a builder that edited the document it was given would change what the next builder reads.
        doc = self.doc_copy()
        for build in (products.build_summary, products.build_linkstats, products.build_path_png,
                      products.path_points, products.kill_points):
            build(doc)
            self.assertEqual(canonical_json(doc), self.data, build.__name__)

    def test_sha256_matches_the_recorded_checksum(self):
        key = golden_key("l1_episode", "l2_summary")
        if key not in GOLDEN:
            self.skipTest("no recorded checksum for %s yet; add it to GOLDEN" % key)
        self.assertEqual(sha256(canonical_json(self.summary)), GOLDEN[key],
                         "l2_summary changed without a version bump in products.ALGORITHMS")

    def test_rollup_sha256_matches_the_recorded_checksum(self):
        key = golden_key("l1_episode", "l2_summary", "l3_rollup")
        if key not in GOLDEN:
            self.skipTest("no recorded checksum for %s yet; add it to GOLDEN" % key)
        ids = ["%s/l2_summary@%s" % (self.summary["episode_id"], products.version("l2_summary"))]
        self.assertEqual(sha256(canonical_json(products.build_rollup([self.summary], ids))), GOLDEN[key],
                         "l3_rollup changed without a version bump in products.ALGORITHMS")

    def test_path_points_forward_fill(self):
        doc = self.doc_copy()
        cols = doc["telemetry"]["columns"]
        blank = lambda t, **kv: [t] + [kv.get(n) for n in cols[1:]]
        doc["telemetry"]["rows"] = [
            blank(100, POS_X=1.0),                  # no POS_Y yet: no point
            blank(150, POS_Y=2.0),                  # (1, 2)
            blank(200, TIC=5),                      # nothing moved
            blank(250, POS_X=1.0, POS_Y=2.0),       # re-sent, same place
            blank(300, POS_X=4.0),                  # (4, 2)
            blank(350, POS_Y=6.0),                  # (4, 6)
        ]
        self.assertEqual(products.path_points(doc), [(150, 1.0, 2.0), (300, 4.0, 2.0), (350, 4.0, 6.0)])
        s = products.build_summary(doc)
        self.assertEqual((s["distance_units"], s["path_points"]), (7.0, 3))
        self.assertEqual(s["tics"], 0)                          # a single TIC sample
        self.assertEqual(s["explored_cells"], 0)
        self.assertEqual(s["health"], {"start": None, "min": None, "end": None})


class TestLinkstats(FixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stats = products.build_linkstats(cls.doc)

    def test_header(self):
        level, ver, _, _ = products.ALGORITHMS["l2_linkstats"]
        self.assertEqual(self.stats["product"], {"type": "l2_linkstats", "level": level, "version": ver})
        self.assertEqual(self.stats["episode_id"], self.doc["episode"]["id"])
        self.assertEqual(self.stats["clock"], self.doc["clock"])

    def test_status_stream_arithmetic(self):
        ss = self.stats["status_stream"]
        tic = [v for _, v in products.column(self.doc, "TIC")]
        steps = [b - a for a, b in zip(tic, tic[1:]) if b > a]
        self.assertEqual(ss["step_tics"], Counter(steps).most_common(1)[0][0])
        self.assertEqual(ss["step_tics"], 3)                    # the payload sends STATUS every 3 tics
        self.assertEqual(ss["expected"], (tic[-1] - tic[0]) // ss["step_tics"] + 1)
        self.assertEqual(ss["received"], len(set(tic)))
        self.assertEqual(ss["missing"], ss["expected"] - ss["received"])
        self.assertEqual(ss["completeness"], round(ss["received"] / ss["expected"], 4))
        self.assertEqual(ss["max_gap_tics"], max(steps))
        # every step here is a whole number of status periods, so the missing ones can be counted directly
        self.assertTrue(all(s % 3 == 0 for s in steps))
        self.assertEqual(ss["missing"], sum(s // 3 - 1 for s in steps))
        self.assertTrue(0 < ss["completeness"] <= 1)

    def test_uplink_and_downlink(self):
        up = self.stats["uplink"]
        self.assertEqual(up["commands_sent"], len(self.doc["commands"]))
        rx = [v for _, v in products.column(self.doc, "CMDS_RECEIVED", "housekeeping")]
        self.assertEqual(up["commands_received_on_board"], rx[-1] - rx[0])
        self.assertEqual(up["completeness"], round(up["commands_received_on_board"] / up["commands_sent"], 4))
        frames = [v for _, v in products.column(self.doc, "FRAMES_SENT", "housekeeping")]
        down = self.stats["downlink"]
        self.assertEqual(down["frames_sent"], frames[-1] - frames[0])
        self.assertEqual(down["frames_per_s"], round(down["frames_sent"] / self.doc["episode"]["duration_s"], 2))
        tm_in = [v for _, v in products.column(self.doc, products.GROUND_LINK, "housekeeping")]
        self.assertEqual(down["tm_frames_received"], tm_in[-1] - tm_in[0])
        self.assertEqual(self.stats["payload_link_up_fraction"], 1.0)

    def test_sample_rates(self):
        dur = self.doc["episode"]["duration_s"]
        self.assertEqual(self.stats["sample_rate_hz"],
                         {n: round(c / dur, 2) for n, c in self.doc["counts"]["samples"].items()})

    def test_by_hand(self):
        ep = episodes.ClosedEpisode(1, 20_001, "PlayerDied", "died", 10_000)
        s = lambda pts: [(t, t - 950, v) for t, v in pts]
        science = {"TIC": s([(10_000, 10), (10_100, 13), (10_200, 16), (10_300, 22), (10_400, 25), (10_500, 25)])}
        link = {"CMDS_RECEIVED": s([(10_000, 5), (19_000, 7)]), "FRAMES_SENT": s([(10_000, 100), (20_000, 150)]),
                "PAYLOAD_LINK": s([(10_000, True), (12_000, False), (14_000, True), (16_000, True)]),
                products.GROUND_LINK: s([(10_000, 0), (20_000, 900)])}
        cmds = [{"t": t, "id": "c%d" % t, "name": config.NAMESPACE + "INTENT", "args": {}} for t in (11_000, 12_000)]
        doc = products.build_l1(ep, (10_000, 20_000), science, link, cmds, [], {}, [])
        st = products.build_linkstats(doc)
        self.assertEqual(st["status_stream"], {"step_tics": 3, "expected": 6, "received": 5, "missing": 1,
                                               "completeness": 0.8333, "max_gap_tics": 6})
        self.assertEqual(st["uplink"], {"commands_sent": 2, "commands_received_on_board": 2, "completeness": 1.0,
                                        "other_commands": {}})
        self.assertEqual(st["downlink"]["frames_sent"], 50)
        self.assertEqual(st["downlink"]["frames_per_s"], 5.0)
        self.assertEqual(st["downlink"]["tm_frames_received"], 900)
        self.assertEqual(st["downlink"]["chunks_sent"], None)
        self.assertEqual(st["payload_link_up_fraction"], 0.75)

    def test_the_ground_systems_own_commands_are_not_the_payloads(self):
        # Seen live: Phase D's SendFile for an earlier episode's record landed in the window of the episode then
        # being flown, and was counted among its commands and against CMDS_RECEIVED, which only the Doom component
        # increments. L1 keeps it (it is a command in the window); L2 reports it apart.
        ep = episodes.ClosedEpisode(1, 20_001, "PlayerDied", "died", 10_000)
        link = {"CMDS_RECEIVED": [(10_000, 9_050, 5), (19_000, 18_050, 7)]}
        cmds = [{"t": t, "id": "c%d" % t, "name": config.NAMESPACE + "INTENT", "args": {}} for t in (11_000, 12_000)]
        cmds.append({"t": 13_000, "id": "s", "name": config.SENDFILE, "args": {"sourceFileName": "x", "destFileName": "y"}})
        doc = products.build_l1(ep, (10_000, 20_000), {"TIC": [(10_000, 9_050, 1)]}, link, cmds, [], {}, [])
        self.assertEqual(len(doc["commands"]), 3)
        st = products.build_linkstats(doc)
        self.assertEqual(st["uplink"]["commands_sent"], 2)
        self.assertEqual(st["uplink"]["completeness"], 1.0)
        self.assertEqual(st["uplink"]["other_commands"], {config.SENDFILE: 1})
        summary = products.build_summary(doc)
        self.assertEqual(summary["commands"], {"INTENT": 2})
        self.assertEqual(summary["other_commands"], {config.SENDFILE: 1})

    def test_cfdp_pdus_are_not_payload_commands(self):
        # On main Yamcs's CfdpService enters each transfer in command history as /doomsat/cfdp/pdu (origin
        # cfdp-service, seen live); like cfdpManager's SendFile it never reaches the Doom component's CMDS_RECEIVED.
        ep = episodes.ClosedEpisode(1, 20_001, "PlayerDied", "died", 10_000)
        link = {"CMDS_RECEIVED": [(10_000, 9_050, 5), (19_000, 18_050, 6)]}
        cmds = [{"t": 11_000, "id": "c1", "name": config.NAMESPACE + "INTENT", "args": {}},
                {"t": 12_000, "id": "p1", "name": "/doomsat/cfdp/pdu", "args": {}, "origin": "cfdp-service"}]
        doc = products.build_l1(ep, (10_000, 20_000), {"TIC": [(10_000, 9_050, 1)]}, link, cmds, [], {}, [])
        st = products.build_linkstats(doc)
        self.assertEqual((st["uplink"]["commands_sent"], st["uplink"]["completeness"]), (1, 1.0))
        self.assertEqual(st["uplink"]["other_commands"], {"/doomsat/cfdp/pdu": 1})
        summary = products.build_summary(doc)
        self.assertEqual(summary["commands"], {"INTENT": 1})
        self.assertEqual(summary["other_commands"], {"/doomsat/cfdp/pdu": 1})

    def test_sha256_matches_the_recorded_checksum(self):
        key = golden_key("l1_episode", "l2_linkstats")
        if key not in GOLDEN:
            self.skipTest("no recorded checksum for %s yet; add it to GOLDEN" % key)
        self.assertEqual(sha256(canonical_json(self.stats)), GOLDEN[key],
                         "l2_linkstats changed without a version bump in products.ALGORITHMS")


class TestPathImage(FixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.image = products.build_path_png(cls.doc)

    def test_valid_png_of_the_expected_size(self):
        self.assertEqual(png.png_size(self.image), (600, 640))
        self.assertEqual(len(idat_raw(self.image)), 640 * (1 + 3 * 600))

    def test_byte_deterministic(self):
        self.assertEqual(products.build_path_png(build_l1(home="b")[0]), self.image)

    def test_markers(self):
        seen = colours(self.image)
        self.assertGreater(seen[png.hex_rgb("#40d070")], 0, "no start marker")
        self.assertGreater(seen[png.hex_rgb(products.END_COLOURS["died"])], 0, "no end marker in the died colour")
        doc = self.doc_copy()
        doc["episode"]["outcome"] = "level_finished"
        seen = colours(products.build_path_png(doc))
        self.assertGreater(seen[png.hex_rgb(products.END_COLOURS["level_finished"])], 0)
        self.assertEqual(seen[png.hex_rgb(products.END_COLOURS["died"])], 0)
        doc["episode"]["outcome"] = "wad_switch"
        seen = colours(products.build_path_png(doc))
        self.assertGreater(seen[png.hex_rgb(products.END_COLOURS["wad_switch"])], 0)

    def test_every_outcome_has_its_colours(self):
        from doomsat_sds import publish
        outcomes = {"died", "level_finished", "reset", "wad_switch", "interrupted"}     # episodes.ClosedEpisode
        self.assertEqual(set(products.END_COLOURS), outcomes)
        self.assertEqual(set(publish.OUTCOME_COLOURS), outcomes)

    def test_no_position_samples_still_renders(self):
        doc = strip_positions(self.doc)
        self.assertEqual(products.path_points(doc), [])
        img = products.build_path_png(doc)
        self.assertEqual(png.png_size(img), (600, 640))
        seen = colours(img)
        self.assertGreater(seen[png.hex_rgb("#e04040")], 0, "the NO POSITION SAMPLES label")
        self.assertEqual(seen[png.hex_rgb("#40d070")], 0, "a start marker with nothing to mark")
        self.assertNotEqual(img, self.image)
        s = products.build_summary(doc)
        self.assertEqual((s["distance_units"], s["path_points"]), (0, 0))

    def test_a_single_position_still_renders(self):
        doc = strip_positions(self.doc)
        cols = doc["telemetry"]["columns"]
        doc["telemetry"]["rows"][0][cols.index("POS_X")] = 10.0
        doc["telemetry"]["rows"][0][cols.index("POS_Y")] = -20.0
        img = products.build_path_png(doc)
        self.assertEqual(png.png_size(img), (600, 640))
        self.assertGreater(colours(img)[png.hex_rgb(products.END_COLOURS["died"])], 0)

    def test_a_teleport_is_not_drawn_as_a_walk(self):
        # Phase C (l2_path 2.0.0): a step longer than JUMP_UNITS is a teleporter, and v1's straight line across the
        # map for it was wrong. Two short walks joined by a jump leave the space between them empty; the same
        # distance walked in short steps is drawn.
        background = png.hex_rgb("#101418")
        green, red = png.hex_rgb("#40d070"), png.hex_rgb(products.END_COLOURS["died"])
        far = 5000.0
        jump = with_positions(self.doc, [(0.0, 0.0), (100.0, 0.0), (far - 100, 0.0), (far, 0.0)])
        walk = with_positions(self.doc, [(far * i / 30, 0.0) for i in range(31)])
        self.assertGreater(far - 200, products.JUMP_UNITS)
        self.assertLess(far / 30, products.JUMP_UNITS)
        for doc, drawn in ((jump, False), (walk, True)):
            px = pixels(products.build_path_png(doc))
            (sx, sy), (ex, ey) = centroid(px, green), centroid(px, red)
            self.assertEqual(sy, ey)
            for f in (0.3, 0.5, 0.7):
                mid = px[(round(sx + (ex - sx) * f), sy)]
                if drawn:
                    self.assertNotEqual(mid, background, "a walk of short steps must be drawn")
                else:
                    self.assertEqual(mid, background, "a jump must not be drawn as a line")

    def test_north_is_up_and_east_is_right(self):
        # Doom's y axis points north and the image's points down. A walk north must end above where it started and
        # a walk east to its right; otherwise every map is drawn mirrored. (Markers: start green, end in the
        # outcome colour; the kill in this episode happens after these positions, so its cross sits at the end.)
        green, red = png.hex_rgb("#40d070"), png.hex_rgb(products.END_COLOURS["died"])
        north = pixels(products.build_path_png(with_positions(self.doc, [(0.0, 40.0 * i) for i in range(10)])))
        (sx, sy), (ex, ey) = centroid(north, green), centroid(north, red)
        self.assertEqual(sx, ex)
        self.assertLess(ey, sy - 100, "walking north must move up the image")
        east = pixels(products.build_path_png(with_positions(self.doc, [(40.0 * i, 0.0) for i in range(10)])))
        (sx, sy), (ex, ey) = centroid(east, green), centroid(east, red)
        self.assertEqual(sy, ey)
        self.assertGreater(ex, sx + 100, "walking east must move right")

    def test_kill_points(self):
        kills = products.kill_points(self.doc)
        k = products.column(self.doc, "KILLS")
        self.assertEqual(len(kills), k[-1][1] - k[0][1])           # one kill in this episode, KILLS 0 -> 1
        self.assertEqual(len(kills), 1)
        first_kill_t = next(t for t, v in k if v == 1)
        self.assertEqual(kills[0][0], first_kill_t)
        held = [p for p in products.path_points(self.doc) if p[0] <= first_kill_t][-1]
        self.assertEqual(kills[0][1:], held[1:], "a kill is marked where the player stood")

    def test_kill_points_by_hand(self):
        doc = self.doc_copy()
        cols = doc["telemetry"]["columns"]
        blank = lambda t, **kv: [t] + [kv.get(n) for n in cols[1:]]
        doc["telemetry"]["rows"] = [
            blank(50, KILLS=0),
            blank(60, KILLS=1),                     # a rise with no position yet: not marked
            blank(150, POS_X=1.0, POS_Y=2.0),
            blank(200, KILLS=2),                    # (1, 2)
            blank(250, KILLS=2),                    # no rise
            blank(300, POS_X=5.0, KILLS=4),         # one mark per rise, at this row's position (5, 2)
        ]
        self.assertEqual(products.kill_points(doc), [(200, 1.0, 2.0), (300, 5.0, 2.0)])

    def test_pixels_match_the_recorded_checksum(self):
        key = golden_key("l1_episode", "l2_path")
        if key not in GOLDEN:
            self.skipTest("no recorded checksum for %s yet; add it to GOLDEN" % key)
        self.assertEqual(sha256(idat_raw(self.image)), GOLDEN[key],
                         "l2_path draws differently without a version bump in products.ALGORITHMS")

    @unittest.skipUnless(HAVE_PIL, "Pillow is not installed (ground/.venv); the SDS venv has it")
    def test_pillow_opens_it(self):
        with PIL.Image.open(io.BytesIO(self.image)) as im:
            im.load()
            self.assertEqual((im.format, im.mode, im.size), ("PNG", "RGB", (600, 640)))


def summary(episode_id, outcome, duration_s, kills, explored, wad, map_, pwad=None):
    return {"episode_id": episode_id, "outcome": outcome, "duration_s": duration_s, "kills": kills,
            "explored_cells": explored, "wad": wad, "map": map_, "pwad": pwad}


class TestRollup(unittest.TestCase):
    A = summary("20261004T004647Z-e0002", "died", 33.65, 1, 137, "freedoom1.wad", "E1M1")
    B = summary("20261004T004611Z-e0001", "level_finished", 10.0, 3, 50, "doom1.wad", "E1M1")
    IDS = ["20261004T004647Z-e0002/l2_summary@x", "20261004T004611Z-e0001/l2_summary@x"]

    def test_totals(self):
        r = products.build_rollup([self.A, self.B], self.IDS)
        level, ver, _, _ = products.ALGORITHMS["l3_rollup"]
        self.assertEqual(r["product"], {"type": "l3_rollup", "level": level, "version": ver})
        self.assertEqual(r["episodes"], 2)
        self.assertEqual(r["outcomes"], {"died": 1, "level_finished": 1})
        self.assertEqual(r["kills"], 4)
        self.assertAlmostEqual(r["duration_s"]["total"], 43.65)
        self.assertAlmostEqual(r["duration_s"]["mean"], 21.825)
        self.assertAlmostEqual(r["duration_s"]["median"], 21.825)
        self.assertEqual(r["duration_s"]["max"], 33.65)
        self.assertEqual(r["explored_cells"], {"mean": 93.5, "max": 137})
        self.assertEqual(r["inputs"], sorted(self.IDS))
        self.assertEqual([e["episode_id"] for e in r["episode_list"]],
                         sorted([self.A["episode_id"], self.B["episode_id"]]))

    def test_grouped_by_wad_and_map(self):
        C = summary("20261004T005000Z-e0003", "reset", 5.5, None, None, "freedoom1.wad", "E1M1")
        D = summary("20261004T005100Z-e0001", "died", 1.0, 0, 2, None, None)
        r = products.build_rollup([self.A, self.B, C, D], ["d", "c", "b", "a"])
        self.assertEqual(list(r["by_level"]), sorted(["freedoom1.wad E1M1", "doom1.wad E1M1", "? ?"]))
        f = r["by_level"]["freedoom1.wad E1M1"]
        self.assertEqual(f["episodes"], 2)
        self.assertEqual(f["outcomes"], {"died": 1, "reset": 1})
        self.assertEqual(f["kills"], 1)                          # a None kill count counts as none
        self.assertEqual(f["best_explored_cells"], 137)
        self.assertAlmostEqual(f["duration_s"], 39.15)
        self.assertEqual(r["by_level"]["doom1.wad E1M1"]["episodes"], 1)
        self.assertEqual(r["by_level"]["? ?"]["outcomes"], {"died": 1})
        self.assertEqual(r["inputs"], ["a", "b", "c", "d"])

    def test_a_patch_wad_is_another_level(self):
        # The same map name over another patch WAD is another level; summaries from before 1.3.0 have no pwad.
        P = summary("20261004T005200Z-e0002", "wad_switch", 7.0, 0, 9, "freedoom1.wad", "E1M1", pwad="basic.wad")
        old = {k: v for k, v in self.A.items() if k != "pwad"}
        r = products.build_rollup([old, P], ["a", "p"])
        self.assertEqual(list(r["by_level"]), ["basic.wad over freedoom1.wad E1M1", "freedoom1.wad E1M1"])
        self.assertEqual(r["by_level"]["basic.wad over freedoom1.wad E1M1"]["outcomes"], {"wad_switch": 1})
        self.assertEqual([e["pwad"] for e in r["episode_list"]], [None, "basic.wad"])
        self.assertEqual(products.level_key({"pwad": "basic.wad"}), "basic.wad over ? ?")

    def test_order_of_inputs_does_not_matter(self):
        one = canonical_json(products.build_rollup([self.A, self.B], self.IDS))
        two = canonical_json(products.build_rollup([self.B, self.A], list(reversed(self.IDS))))
        self.assertEqual(one, two)

    def test_empty(self):
        r = products.build_rollup([], [])
        self.assertEqual((r["episodes"], r["kills"], r["outcomes"], r["by_level"]), (0, 0, {}, {}))
        self.assertIsNone(r["duration_s"]["mean"])
        canonical_json(r)                                        # serialisable (no NaN)

    def test_inputs_hash_is_order_independent(self):
        ids, shas = ["b/l2_summary@1", "a/l2_summary@1"], ["22" * 32, "11" * 32]
        h = products.rollup_inputs_hash(ids, shas)
        self.assertEqual(h, products.rollup_inputs_hash(list(reversed(ids)), list(reversed(shas))))
        self.assertEqual(len(h), 64)
        self.assertNotEqual(h, products.rollup_inputs_hash(ids, ["22" * 32, "33" * 32]), "a changed input")
        self.assertNotEqual(h, products.rollup_inputs_hash(ids, list(reversed(shas))), "swapped pairing")
        self.assertNotEqual(h, products.rollup_inputs_hash(ids[:1], shas[:1]), "a dropped input")


class TestVersions(unittest.TestCase):
    def test_newer_compares_numerically(self):
        self.assertTrue(products.newer("1.10.0", "1.9.0"))
        self.assertFalse(products.newer("1.9.0", "1.10.0"))
        self.assertTrue(products.newer("1.0.10", "1.0.9"))
        self.assertTrue(products.newer("2.0.0", "1.99.99"))
        self.assertFalse(products.newer("1.2.3", "1.2.3"))

    def test_algorithms_table(self):
        for t, (level, ver, ext, media) in products.ALGORITHMS.items():
            self.assertEqual(products.version(t), ver)
            self.assertEqual(len(ver.split(".")), 3, t)
            self.assertTrue(all(p.isdigit() for p in ver.split(".")), t)
            self.assertFalse(products.newer(ver, ver))
            self.assertEqual(media, {"json": "application/json", "png": "image/png"}[ext], t)
        for t in products.L2_TYPES:
            self.assertEqual(products.ALGORITHMS[t][0], "L2")


def f32(x: float) -> float:
    """x as float32, widened back to a Python float (how F32 channels reach yamcs-client)."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


class TestArchiveValue(unittest.TestCase):
    def test_bools(self):
        self.assertIs(archive.value("True"), True)
        self.assertIs(archive.value("False"), False)
        self.assertIs(archive.value(True), True)
        self.assertEqual(archive.value("RETREAT"), "RETREAT")    # other enum labels stay labels

    def test_widened_float32_is_normalised_and_round_trips(self):
        widened = 592.05224609375                                  # archive.py's example: a float32, widened
        self.assertEqual(f32(widened), widened)
        self.assertEqual(archive.value(widened), 592.052246)
        for x in (592.0522, -80.736618, 1 / 3, 0.1, -1e-30, 3.0e38, 1e-45, 16777217.0, 0.0):
            w = f32(x)
            v = archive.value(w)
            self.assertIsInstance(v, float)
            self.assertEqual(f32(v), w, "value(%r) = %r does not round-trip through float32" % (w, v))
            self.assertLessEqual(len(("%.9e" % v).split("e")[0].replace("-", "").replace(".", "").rstrip("0")), 9)
            self.assertEqual(archive.value(v), v, "normalising twice changes nothing")

    def test_fixture_floats_are_already_normalised(self):
        data = archive.RecordedArchive(FIXTURE).data["parameters"]
        floats = [s[2] for n in ("POS_X", "POS_Y") for s in data[n]]
        self.assertTrue(floats)
        for v in floats:
            self.assertEqual(archive.value(v), v)
            self.assertEqual(archive.value(f32(v)), v, "a stored float is the 9-digit form of a float32")

    def test_ints_none_and_bytes(self):
        self.assertEqual(archive.value(7788), 7788)
        self.assertIsNone(archive.value(None))
        self.assertEqual(archive.value(b"\x00\x01\xab\xff"), "0001abff")
        self.assertEqual(archive.value(bytearray(b"\x10\x20")), "1020")
        self.assertEqual(archive.value(b""), "")


if __name__ == "__main__":
    unittest.main()
