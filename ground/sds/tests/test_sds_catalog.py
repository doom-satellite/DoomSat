"""The product catalog (doomsat_sds.catalog): the one SQLite file that says which products exist, what they were
made from, and which version of each is current.

Reprocessing (Phase C) never replaces a product: a new algorithm version is written beside the old one, and the
catalog's is_current flag is the only thing that says which to use. So the flag has to follow the numeric version
(1.10.0 is newer than 1.2.0, which a string compare gets wrong), exactly one row per episode and type may hold it,
and L3/quicklook products with no episode fall back to the newest one written. A retried Airflow task must be a
no-op ("unchanged"), while the same id coming back with different bytes is a reproducibility problem a person
must see (a checksum_changed finding). The episode's context belongs to the flight, so a later run must not
rewrite it. Quicklook retention (forget) may drop episode-less ql_* rows and nothing else. Several Airflow tasks
write the file at once, so two writers must both succeed. The CLI opens the catalog read-only and must never
create or change it.

Versions here are made up to exercise the ordering rules; they are not products.ALGORITHMS versions.
"""
import contextlib
import io
import itertools
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import catalog as catalog_mod  # noqa: E402
from doomsat_sds import products  # noqa: E402
from doomsat_sds.catalog import Catalog  # noqa: E402
from doomsat_sds.store import canonical_json, sha256, write_atomic  # noqa: E402

# Episode ids in the format episodes.episode_id makes; these are the two deaths in tests/data/two_deaths.json.gz.
EP1 = "20261004T004611Z-e0001"
EP2 = "20261004T004647Z-e0002"
CONTEXT = {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "pilot_mode": "system-one=code system-two=none",
           "repo_commit": "abc123def456", "level_set": "dev", "sources": {"wad": "payload argv"}}


def episode_row(episode_id=EP2, number=2, context=None, **over):
    ctx = CONTEXT if context is None else context
    row = {"episode_id": episode_id, "number": number, "outcome": "died", "closing_event": "PlayerDied",
           "inferred_close": 0, "closing_ms": 1791074807184, "start_event_ms": 1791074773533,
           "start_utc": "2026-10-04T00:46:13.533Z", "end_utc": "2026-10-04T00:46:47.183Z",
           "start_ms": 1791074773533, "end_ms": 1791074807183, "duration_s": 33.65,
           "wad": ctx.get("wad"), "map": ctx.get("map"), "skill": ctx.get("skill"), "seed": None,
           "pilot_mode": ctx.get("pilot_mode"), "repo_commit": ctx.get("repo_commit"),
           "level_set": ctx.get("level_set"), "context": ctx}
    row.update(over)
    return row


def fake_sha(*parts) -> str:
    return sha256("/".join(str(p) for p in parts).encode())


def register(cat, episode_id, product_type, version, *, product_id=None, sha=None, path=None, run_id=None,
             inputs=None, media_type=None):
    """Register one product the way pipeline._register does, with defaults that make each call distinct."""
    level, _, ext, media = products.ALGORITHMS.get(product_type, ("L2", None, "json", "application/json"))
    pid = product_id or "%s/%s@%s" % (episode_id, product_type, version)
    return cat.register(product_id=pid, episode_id=episode_id, level=level, product_type=product_type,
                        version=version, path=path or "/nonexistent/%s.%s" % (pid.replace("/", "_"), ext),
                        media_type=media_type or media, sha256=sha or fake_sha(pid), size_bytes=123,
                        inputs=inputs if inputs is not None else [], run_id=run_id, code_commit="abc123def456")


class FakeClock:
    """Stands in for catalog.now_utc so 'newest created' is decided by the test, not by the machine's speed."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return "2026-10-04T01:00:%02d.000000Z" % self.n


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "sds" / "catalog.sqlite"
        self.cat = Catalog(self.path)

    def tearDown(self):
        self.cat.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------------------------- schema

class TestSchema(CatalogCase):
    def test_opening_twice_keeps_the_data(self):
        self.cat.add_episode(episode_row())
        register(self.cat, EP2, "l2_summary", "1.0.0")
        self.cat.close()
        self.cat = Catalog(self.path)          # CREATE ... IF NOT EXISTS runs again: no error, nothing lost
        with Catalog(self.path) as again:      # and a second writer on the same file at the same time
            self.assertEqual([e["episode_id"] for e in again.episodes()], [EP2])
            self.assertEqual([p["product_id"] for p in again.products()], [EP2 + "/l2_summary@1.0.0"])
        tables = {r[0] for r in self.cat.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"episodes", "products", "findings"} <= tables)

    def test_creates_its_directory_and_uses_wal(self):
        # Settings.catalog sits under $DOOMSAT_SDS_HOME, which may not exist yet on the first run.
        deep = self.tmp / "a" / "b" / "catalog.sqlite"
        with Catalog(deep) as c:
            self.assertTrue(deep.exists())
            self.assertEqual(c.db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")


# ---------------------------------------------------------------------------------------------- episodes

class TestEpisodes(CatalogCase):
    def test_add_episode_returns_the_row_with_its_context(self):
        row = self.cat.add_episode(episode_row())
        self.assertEqual(row["episode_id"], EP2)
        self.assertEqual(row["number"], 2)
        self.assertEqual(row["outcome"], "died")
        self.assertEqual(row["wad"], "freedoom1.wad")
        self.assertEqual(row["context"], CONTEXT)
        self.assertNotIn("context_json", row)
        self.assertTrue(row["first_cataloged_utc"].endswith("Z"))
        self.assertEqual(self.cat.episode(EP2), row)

    def test_a_second_add_keeps_the_first_context(self):
        first = self.cat.add_episode(episode_row())
        later = dict(CONTEXT, wad="doom1.wad", map="E1M2", repo_commit="feedfacecafe", level_set="test")
        again = self.cat.add_episode(episode_row(context=later, outcome="reset", first_cataloged_utc="2030-01-01Z"))
        self.assertEqual(again, first)
        self.assertEqual(again["context"], CONTEXT)
        self.assertEqual(again["wad"], "freedoom1.wad")
        self.assertEqual(again["outcome"], "died")
        self.assertEqual(self.cat.episode(EP2)["context"], CONTEXT)
        self.assertEqual(len(self.cat.episodes()), 1)

    def test_episodes_are_listed_in_id_order_and_unknown_is_none(self):
        self.cat.add_episode(episode_row(EP2, 2))
        self.cat.add_episode(episode_row(EP1, 1))
        self.assertEqual([e["episode_id"] for e in self.cat.episodes()], [EP1, EP2])
        self.assertIsNone(self.cat.episode("20991231T000000Z-e0001"))


# ---------------------------------------------------------------------------------------------- register

class TestRegister(CatalogCase):
    def setUp(self):
        super().setUp()
        self.cat.add_episode(episode_row())
        self.pid = EP2 + "/l2_summary@1.0.0"

    def test_new_then_unchanged_for_the_same_checksum(self):
        inputs = [{"product_id": EP2 + "/l1_episode@1.0.0", "sha256": fake_sha("l1")}]
        row, status = register(self.cat, EP2, "l2_summary", "1.0.0", sha=fake_sha("a"), run_id="fwd__1",
                               inputs=inputs)
        self.assertEqual(status, "new")
        self.assertEqual(row["product_id"], self.pid)
        self.assertEqual(row["sha256"], fake_sha("a"))
        self.assertEqual(row["inputs"], inputs)
        self.assertIs(row["is_current"], True)
        self.assertEqual(row["airflow_run_id"], "fwd__1")
        # A retried task: same id, same bytes. Nothing changes, not even the run that made it, and no finding.
        row2, status2 = register(self.cat, EP2, "l2_summary", "1.0.0", sha=fake_sha("a"), run_id="fwd__1_retry",
                                 inputs=inputs)
        self.assertEqual(status2, "unchanged")
        self.assertEqual(row2, row)
        self.assertEqual(self.cat.findings(), [])
        self.assertEqual(len(self.cat.products()), 1)

    def test_a_different_checksum_at_the_same_id_is_changed_with_a_finding(self):
        register(self.cat, EP2, "l2_summary", "1.0.0", sha=fake_sha("a"), run_id="fwd__1", path="/p/old.json")
        row, status = register(self.cat, EP2, "l2_summary", "1.0.0", sha=fake_sha("b"), run_id="reprocess__2",
                               path="/p/new.json")
        self.assertEqual(status, "changed")
        self.assertEqual(row["sha256"], fake_sha("b"))           # the row now describes the file on disk
        self.assertEqual(row["path"], "/p/new.json")
        self.assertEqual(row["airflow_run_id"], "reprocess__2")
        self.assertIs(row["is_current"], True)
        self.assertEqual(len(self.cat.products()), 1)
        findings = self.cat.findings()
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "checksum_changed")
        self.assertEqual(f["episode_id"], EP2)
        self.assertEqual(f["product_id"], self.pid)
        self.assertEqual(f["detail"]["old_sha256"], fake_sha("a"))
        self.assertEqual(f["detail"]["new_sha256"], fake_sha("b"))
        self.assertEqual(f["detail"]["old_run"], "fwd__1")
        self.assertEqual(f["detail"]["new_run"], "reprocess__2")
        # The changed bytes registered again are a retry of the change: unchanged, no second finding.
        _, status3 = register(self.cat, EP2, "l2_summary", "1.0.0", sha=fake_sha("b"))
        self.assertEqual(status3, "unchanged")
        self.assertEqual(len(self.cat.findings()), 1)

    def test_a_product_of_an_uncataloged_episode_is_refused_and_leaves_nothing(self):
        # products.episode_id references episodes; the transaction is rolled back, so the catalog stays usable.
        with self.assertRaises(sqlite3.IntegrityError):
            register(self.cat, EP1, "l2_summary", "1.0.0")
        self.assertEqual(self.cat.products(), [])
        self.assertEqual(self.cat.findings(), [])
        _, status = register(self.cat, EP2, "l2_summary", "1.0.0")
        self.assertEqual(status, "new")


# ---------------------------------------------------------------------------------------------- current

class TestCurrent(CatalogCase):
    def assert_one_current(self, rows, version):
        cur = [r for r in rows if r["is_current"]]
        self.assertEqual(len(cur), 1, rows)
        self.assertEqual(cur[0]["algorithm_version"], version)

    def test_the_numerically_newest_version_is_current_whatever_the_order(self):
        versions = ("1.0.0", "1.2.0", "1.10.0")             # "1.2.0" > "1.10.0" as strings
        for i, order in enumerate(itertools.permutations(versions)):
            eid = "20261004T0100%02dZ-e%04d" % (i, i + 1)
            self.cat.add_episode(episode_row(eid, i + 1))
            with self.subTest(order=order):
                for v in order:
                    register(self.cat, eid, "l2_path", v)
                rows = self.cat.products(eid, "l2_path")
                self.assertEqual(sorted(r["algorithm_version"] for r in rows), sorted(versions))
                self.assert_one_current(rows, "1.10.0")
                self.assertEqual(self.cat.current(eid, "l2_path")["product_id"], eid + "/l2_path@1.10.0")
                self.assertEqual([r["algorithm_version"] for r in self.cat.products(eid, "l2_path", current_only=True)],
                                 ["1.10.0"])

    def test_current_is_per_episode_and_type(self):
        self.cat.add_episode(episode_row(EP1, 1))
        self.cat.add_episode(episode_row(EP2, 2))
        for eid in (EP1, EP2):
            register(self.cat, eid, "l2_path", "1.0.0")
            register(self.cat, eid, "l2_summary", "1.0.0")
        register(self.cat, EP2, "l2_path", "2.0.0")            # reprocessing one episode, one type
        self.assertEqual(self.cat.current(EP2, "l2_path")["algorithm_version"], "2.0.0")
        self.assertEqual(self.cat.current(EP1, "l2_path")["algorithm_version"], "1.0.0")
        self.assertEqual(self.cat.current(EP2, "l2_summary")["algorithm_version"], "1.0.0")
        self.assertIs(self.cat.product(EP2 + "/l2_path@1.0.0")["is_current"], False)   # kept, not current
        self.assertEqual(len(self.cat.products(current_only=True)), 4)

    def test_current_is_none_when_there_is_nothing(self):
        self.cat.add_episode(episode_row())
        self.assertIsNone(self.cat.current(EP2, "l2_path"))
        register(self.cat, EP2, "l2_summary", "1.0.0")
        self.assertIsNone(self.cat.current(EP2, "l2_path"))
        # An episode product is never the current episode-less product of its type.
        self.assertIsNone(self.cat.current(None, "l2_summary"))

    def test_episode_less_products_go_by_version_then_newest_created(self):
        self.cat.add_episode(episode_row())
        register(self.cat, EP2, "l3_rollup", "9.0.0")          # an episode row of the same type is a separate group
        with mock.patch.object(catalog_mod, "now_utc", FakeClock()):
            h = ["%012x" % (0xF0 - i) for i in range(4)]     # each hash sorts before the one written earlier
            l3 = lambda v, i: register(self.cat, None, "l3_rollup", v, product_id="l3/l3_rollup@%s/%s" % (v, h[i]))
            l3("1.0.0", 0)
            l3("1.0.0", 1)
            self.assertEqual(self.cat.current(None, "l3_rollup")["product_id"], "l3/l3_rollup@1.0.0/" + h[1])
            l3("1.1.0", 2)
            l3("1.0.0", 3)                                       # newer, but an older algorithm version
            cur = self.cat.current(None, "l3_rollup")
            self.assertEqual(cur["product_id"], "l3/l3_rollup@1.1.0/" + h[2])
            self.assertIsNone(cur["episode_id"])
        rows = [r for r in self.cat.products(product_type="l3_rollup") if r["episode_id"] is None]
        self.assertEqual(len(rows), 4)
        self.assert_one_current(rows, "1.1.0")
        self.assertEqual(self.cat.current(EP2, "l3_rollup")["algorithm_version"], "9.0.0")

    def test_quicklooks_at_one_version_go_by_newest_created(self):
        with mock.patch.object(catalog_mod, "now_utc", FakeClock()):
            for stamp in ("20261004T004600Z", "20261004T004700Z", "20261004T004800Z"):
                ver = products.version("ql_health")
                register(self.cat, None, "ql_health", ver, product_id="ql/ql_health@%s/%s" % (ver, stamp))
        self.assertTrue(self.cat.current(None, "ql_health")["product_id"].endswith("/20261004T004800Z"))
        self.assertEqual(len(self.cat.products(product_type="ql_health", current_only=True)), 1)


# ---------------------------------------------------------------------------------------------- queries

class TestProductsQuery(CatalogCase):
    def setUp(self):
        super().setUp()
        self.cat.add_episode(episode_row(EP1, 1))
        self.cat.add_episode(episode_row(EP2, 2))
        for eid in (EP1, EP2):
            register(self.cat, eid, "l1_episode", "1.0.0")
            register(self.cat, eid, "l2_summary", "1.0.0")
        register(self.cat, EP2, "l2_summary", "1.1.0")
        register(self.cat, None, "l3_rollup", "1.0.0", product_id="l3/l3_rollup@1.0.0/0123456789ab")

    def ids(self, **kw):
        return [p["product_id"] for p in self.cat.products(**kw)]

    def test_no_filter_lists_everything_in_id_order(self):
        ids = self.ids()
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), 6)

    def test_filters(self):
        self.assertEqual(self.ids(episode_id=EP1), [EP1 + "/l1_episode@1.0.0", EP1 + "/l2_summary@1.0.0"])
        self.assertEqual(self.ids(product_type="l2_summary"),
                         [EP1 + "/l2_summary@1.0.0", EP2 + "/l2_summary@1.0.0", EP2 + "/l2_summary@1.1.0"])
        self.assertEqual(self.ids(product_type="l2_summary", current_only=True),
                         [EP1 + "/l2_summary@1.0.0", EP2 + "/l2_summary@1.1.0"])
        self.assertEqual(self.ids(episode_id=EP2, product_type="l2_summary", current_only=True),
                         [EP2 + "/l2_summary@1.1.0"])
        self.assertEqual(self.ids(product_type="l3_rollup"), ["l3/l3_rollup@1.0.0/0123456789ab"])
        self.assertEqual(len(self.ids(current_only=True)), 5)
        self.assertEqual(self.ids(episode_id="20991231T000000Z-e0009"), [])
        self.assertIsNone(self.cat.product("nope"))


# ---------------------------------------------------------------------------------------------- forget

class TestForget(CatalogCase):
    def test_only_episode_less_quicklooks_are_forgotten(self):
        self.cat.add_episode(episode_row())
        keep = [register(self.cat, EP2, "l1_episode", "1.0.0")[0]["product_id"],
                register(self.cat, EP2, "l2_path", "1.0.0")[0]["product_id"],
                register(self.cat, None, "l3_rollup", "1.0.0", product_id="l3/l3_rollup@1.0.0/0123456789ab")[0][
                    "product_id"]]
        drop = [register(self.cat, None, "ql_health", "1.0.0", product_id="ql/ql_health@1.0.0/20261004T004600Z")[0][
                    "product_id"],
                register(self.cat, None, "ql_contact_sheet", "1.0.0",
                         product_id="ql/ql_contact_sheet@1.0.0/20261004T004600Z")[0]["product_id"]]
        self.assertEqual(self.cat.forget(keep + drop + ["ql/ql_health@1.0.0/never-registered"]), 2)
        self.assertEqual(sorted(p["product_id"] for p in self.cat.products()), sorted(keep))
        self.assertEqual(self.cat.forget(keep), 0)
        self.assertEqual(self.cat.forget([]), 0)
        self.assertEqual(sorted(p["product_id"] for p in self.cat.products()), sorted(keep))

    def test_an_episode_product_is_never_forgotten_even_of_a_quicklook_type(self):
        # "Episode products are never forgotten" is its own guard, not a consequence of today's type names: a
        # per-episode quicklook would carry an episode id and must survive retention like any episode product.
        self.cat.add_episode(episode_row())
        ver = products.version("ql_health")
        pid = register(self.cat, EP2, "ql_health", ver)[0]["product_id"]
        self.assertEqual(self.cat.forget([pid]), 0)
        self.assertIsNotNone(self.cat.product(pid))

    # Regression test for a bug the tests found (now fixed): catalog.py forget() matches the type with LIKE 'ql_%'. In SQL LIKE "_" is a one-character
    # wildcard and ASCII matching is case-insensitive, so an episode-less "qlx_rollup" or "QL_HEALTH" is deleted
    # too. Latent today (no such type in products.ALGORITHMS, and pipeline.quicklook pre-filters on
    # startswith("ql_")), but forget() is the guard that is meant to hold on its own.
    def test_only_types_that_start_with_ql_underscore_are_forgotten(self):
        lookalikes = [register(self.cat, None, t, "1.0.0", product_id="l3/%s@1.0.0/0123456789ab" % t)[0]["product_id"]
                      for t in ("qlx_rollup", "QL_HEALTH")]
        self.assertEqual(self.cat.forget(lookalikes), 0)
        self.assertEqual(sorted(p["product_id"] for p in self.cat.products()), sorted(lookalikes))


# ---------------------------------------------------------------------------------------------- findings, summary

class TestFindingsAndSummary(CatalogCase):
    def test_add_finding_and_findings(self):
        self.assertEqual(self.cat.findings(), [])
        self.cat.add_finding("record_disagrees", {"field": "kills", "record": 3, "l1": 2}, episode_id=EP2,
                             product_id=EP2 + "/l1_episode@1.0.0")
        self.cat.add_finding("note", {"text": "no episode"})
        f = self.cat.findings()
        self.assertEqual([x["kind"] for x in f], ["record_disagrees", "note"])
        self.assertLess(f[0]["finding_id"], f[1]["finding_id"])
        self.assertEqual(f[0]["detail"], {"field": "kills", "record": 3, "l1": 2})
        self.assertNotIn("detail_json", f[0])
        self.assertEqual((f[0]["episode_id"], f[0]["product_id"]), (EP2, EP2 + "/l1_episode@1.0.0"))
        self.assertEqual((f[1]["episode_id"], f[1]["product_id"]), (None, None))
        self.assertTrue(f[1]["created_utc"].endswith("Z"))

    def test_summary_counts(self):
        self.assertEqual(self.cat.summary(), {"episodes": 0, "products": {}, "current": {}, "findings": 0})
        self.cat.add_episode(episode_row(EP1, 1))
        self.cat.add_episode(episode_row(EP2, 2))
        register(self.cat, EP1, "l2_path", "1.0.0")
        register(self.cat, EP2, "l2_path", "1.0.0")
        register(self.cat, EP2, "l2_path", "1.2.0")
        register(self.cat, None, "l3_rollup", "1.0.0", product_id="l3/l3_rollup@1.0.0/0123456789ab")
        register(self.cat, EP1, "l2_path", "1.0.0", sha=fake_sha("other"))      # changed: one finding
        self.cat.add_finding("note", {})
        self.assertEqual(self.cat.summary(), {
            "episodes": 2,
            "products": {"l2_path@1.0.0": 2, "l2_path@1.2.0": 1, "l3_rollup@1.0.0": 1},
            "current": {"l2_path": 2, "l3_rollup": 1},
            "findings": 2})


# ---------------------------------------------------------------------------------------------- read-only

class TestReadonly(CatalogCase):
    def setUp(self):
        super().setUp()
        self.cat.add_episode(episode_row())
        register(self.cat, EP2, "l2_summary", "1.0.0")
        register(self.cat, None, "ql_health", "1.0.0", product_id="ql/ql_health@1.0.0/20261004T004600Z")
        self.cat.add_finding("note", {"n": 1})

    def test_reads_work(self):
        with Catalog(self.path, readonly=True) as ro:
            self.assertEqual(ro.episodes(), self.cat.episodes())
            self.assertEqual(ro.products(), self.cat.products())
            self.assertEqual(ro.findings(), self.cat.findings())
            self.assertEqual(ro.summary(), self.cat.summary())
            self.assertEqual(ro.current(EP2, "l2_summary")["product_id"], EP2 + "/l2_summary@1.0.0")

    def test_writes_raise_and_change_nothing(self):
        before = (self.cat.episodes(), self.cat.products(), self.cat.findings())
        writes = {
            "add_episode": lambda ro: ro.add_episode(episode_row(EP1, 1)),
            "register": lambda ro: register(ro, EP2, "l2_path", "1.0.0"),
            "add_finding": lambda ro: ro.add_finding("note", {"n": 2}),
            "forget": lambda ro: ro.forget(["ql/ql_health@1.0.0/20261004T004600Z"]),
        }
        for name, write in writes.items():
            with self.subTest(write=name), Catalog(self.path, readonly=True) as ro:
                with self.assertRaises(sqlite3.Error):
                    write(ro)
        self.assertEqual((self.cat.episodes(), self.cat.products(), self.cat.findings()), before)

    def test_a_reader_sees_later_writes(self):
        with Catalog(self.path, readonly=True) as ro:
            self.assertIsNone(ro.product(EP2 + "/l2_path@1.0.0"))
            register(self.cat, EP2, "l2_path", "1.0.0")
            self.assertIsNotNone(ro.product(EP2 + "/l2_path@1.0.0"))

    def test_opening_a_missing_file_read_only_does_not_create_it(self):
        missing = self.tmp / "nowhere" / "catalog.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            Catalog(missing, readonly=True)
        self.assertFalse(missing.exists())


# ---------------------------------------------------------------------------------------------- CLI

class TestCli(CatalogCase):
    def setUp(self):
        super().setUp()
        self.cat.add_episode(episode_row(EP1, 1))
        self.cat.add_episode(episode_row(EP2, 2))
        self.summary_doc = {"product": {"type": "l2_summary"}, "episode_id": EP2, "kills": 3, "marker": "cli-show"}
        data = canonical_json(self.summary_doc)
        self.summary_path = write_atomic(self.tmp / "sds" / "products" / "summary.json", data)
        self.summary_id = register(self.cat, EP2, "l2_summary", "1.0.0", path=str(self.summary_path),
                                   sha=sha256(data), run_id="fwd__" + EP2)[0]["product_id"]
        register(self.cat, EP2, "l2_path", "1.0.0")
        register(self.cat, EP2, "l2_path", "1.2.0")
        register(self.cat, EP1, "l2_path", "1.0.0")
        self.cat.add_finding("note", {"text": "from the cli test"}, episode_id=EP1)

    def run_cli(self, *args, catalog=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = catalog_mod.main(["--catalog", str(catalog or self.path)] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_summary(self):
        rc, out, _ = self.run_cli("summary")
        self.assertEqual(rc, 0)
        self.assertIn("catalog: 2 episodes, 1 findings", out)
        self.assertIn('"l2_path@1.2.0": 1', out)

    def test_episodes(self):
        rc, out, _ = self.run_cli("episodes")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertIn("episode_id", lines[0])
        self.assertIn(EP1, out)
        self.assertIn(EP2, out)
        self.assertIn("freedoom1.wad", out)

    def test_products_and_filters(self):
        rc, out, _ = self.run_cli("products")
        self.assertEqual(rc, 0)
        for pid in (self.summary_id, EP2 + "/l2_path@1.0.0", EP2 + "/l2_path@1.2.0", EP1 + "/l2_path@1.0.0"):
            self.assertIn(pid, out)
        _, out, _ = self.run_cli("products", "--episode", EP1)
        self.assertIn(EP1 + "/l2_path@1.0.0", out)
        self.assertNotIn(EP2, out)
        _, out, _ = self.run_cli("products", "--type", "l2_path", "--current")
        self.assertIn(EP2 + "/l2_path@1.2.0", out)
        self.assertIn(EP1 + "/l2_path@1.0.0", out)
        self.assertNotIn(EP2 + "/l2_path@1.0.0", out)
        self.assertNotIn("l2_summary", out)
        _, out, _ = self.run_cli("products", "--type", "l9_nothing")
        self.assertIn("(none)", out)

    def test_show_prints_the_row_and_the_json_content(self):
        rc, out, _ = self.run_cli("show", self.summary_id)
        self.assertEqual(rc, 0)
        dec = json.JSONDecoder()
        row, end = dec.raw_decode(out)
        content, _ = dec.raw_decode(out[end:].lstrip())
        self.assertEqual(row["product_id"], self.summary_id)
        self.assertEqual(row["path"], str(self.summary_path))
        self.assertEqual(content, self.summary_doc)

    def test_show_of_a_non_json_product_prints_only_the_row(self):
        rc, out, _ = self.run_cli("show", EP2 + "/l2_path@1.0.0")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["media_type"], "image/png")

    def test_show_of_an_unknown_product_returns_1(self):
        rc, out, err = self.run_cli("show", "no/such@1.0.0")
        self.assertEqual(rc, 1)
        self.assertIn("no such product", err)

    def test_findings_and_sql(self):
        rc, out, _ = self.run_cli("findings")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.strip())["detail"], {"text": "from the cli test"})
        rc, out, _ = self.run_cli("sql", "SELECT COUNT(*) AS n FROM products WHERE is_current = 1")
        self.assertEqual(rc, 0)
        self.assertEqual(out.split(), ["n", "-", "3"])

    def test_sql_cannot_write(self):
        # Whether the CLI lets sqlite's error escape or reports it and returns non-zero is presentation; what
        # matters is that the write is refused and the catalog is untouched.
        try:
            rc = self.run_cli("sql", "DELETE FROM products")[0]
        except sqlite3.Error:
            rc = None
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.cat.products()), 4)

    def test_the_cli_never_writes_the_file(self):
        # data_version, read on one connection, changes when any other connection commits to the file.
        version = lambda: self.cat.db.execute("PRAGMA data_version").fetchone()[0]
        before = version()
        for args in (["summary"], ["episodes"], ["products"], ["show", self.summary_id], ["findings"],
                     ["sql", "SELECT * FROM episodes"]):
            self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertEqual(version(), before)
        with Catalog(self.path) as writer:      # the check itself works: a real write does move it
            writer.add_finding("note", {})
        self.assertNotEqual(version(), before)

    def test_a_missing_catalog_returns_1_and_is_not_created(self):
        missing = self.tmp / "elsewhere" / "catalog.sqlite"
        rc, out, err = self.run_cli("summary", catalog=missing)
        self.assertEqual(rc, 1)
        self.assertIn("no catalog yet", err)
        self.assertEqual(out, "")
        self.assertFalse(missing.exists())

    def test_default_catalog_is_under_doomsat_sds_home(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DOOMSAT_SDS_HOME": str(self.tmp / "sds")}), \
                contextlib.redirect_stdout(out):
            rc = catalog_mod.main(["summary"])
        self.assertEqual(rc, 0)
        self.assertIn("catalog: 2 episodes", out.getvalue())


# ---------------------------------------------------------------------------------------------- concurrency

class TestConcurrency(CatalogCase):
    """Airflow runs up to four tasks at once, each with its own Catalog on the same file."""

    def test_two_catalogs_alternating(self):
        self.cat.add_episode(episode_row())
        with Catalog(self.path) as other:
            for i in range(10):
                _, s1 = register(self.cat, EP2, "l2_path", "1.%d.0" % (2 * i))
                _, s2 = register(other, EP2, "l2_path", "1.%d.0" % (2 * i + 1))
                self.assertEqual((s1, s2), ("new", "new"))
            self.assertEqual(len(other.products()), 20)
        rows = self.cat.products(EP2, "l2_path")
        self.assertEqual(len(rows), 20)
        self.assertEqual([r["algorithm_version"] for r in rows if r["is_current"]], ["1.19.0"])

    def test_two_threads_at_once(self):
        self.cat.add_episode(episode_row(EP1, 1))
        self.cat.add_episode(episode_row(EP2, 2))
        n, errors, statuses = 15, [], []
        start = threading.Barrier(2)

        def writer(k):
            try:
                with Catalog(self.path) as c:
                    start.wait(timeout=10)
                    for i in range(n):
                        # same episode and type from both threads: is_current must still end up on one row
                        statuses.append(register(c, EP2, "l2_path", "2.%d.0" % (2 * i + k))[1])
                        statuses.append(register(c, (EP1, EP2)[k], "l2_summary", "1.%d.%d" % (i, k))[1])
            except Exception as e:      # noqa: BLE001 - reported below
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(k,)) for k in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(statuses, ["new"] * (4 * n))
        path_rows = self.cat.products(EP2, "l2_path")
        self.assertEqual(len(path_rows), 2 * n)
        self.assertEqual([r["algorithm_version"] for r in path_rows if r["is_current"]], ["2.%d.0" % (2 * n - 1)])
        self.assertEqual(self.cat.current(EP1, "l2_summary")["algorithm_version"], "1.%d.0" % (n - 1))
        self.assertEqual(self.cat.current(EP2, "l2_summary")["algorithm_version"], "1.%d.1" % (n - 1))


if __name__ == "__main__":
    unittest.main()
