"""OpenLineage run events (doomsat_sds.lineage): the record an operations agent reads to learn what made each product,
from what, in which Airflow run, without opening Airflow's database or the catalog.

Each event must be a valid OpenLineage 2-0-2 RunEvent (COMPLETE, producer, schemaURL, a UUID runId, and
_producer/_schemaURL on every facet) or consumers drop it. The runId must be the same every time it is computed
for one (Airflow run, product), so a retried task cannot invent a second run. Inputs and outputs carry the
products' sha256 as the dataset version, which is what lets lineage be checked against the catalog, and the
output's dataSource uri must lead back to the product file. One line is
written per product actually written ("new" or "changed"), none for a retry that found the same bytes
("unchanged"), and an unwritable lineage file must never fail the product it describes.

Rows come from a real Catalog.register (the row pipeline._register hands to lineage.emit), and the archive reads
are the input references pipeline.make_l1 builds for episode 2 of tests/data/two_deaths.json.gz.
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import uuid
from pathlib import Path

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import archive, lineage, pipeline, products  # noqa: E402
from doomsat_sds.catalog import Catalog  # noqa: E402
from doomsat_sds.config import Settings  # noqa: E402
from doomsat_sds.store import episode_product_path, sha256  # noqa: E402

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
EP2 = "20261004T004647Z-e0002"
CONF2 = {"number": 2, "end_ms": 1791074807184, "closing": "PlayerDied", "outcome": "died",
         "start_event_ms": 1791074773533, "inferred": False, "episode_id": EP2}
WINDOW = [1791074773533, 1791074807183]         # episode 2's window as the live pipeline computed it
CONTEXT = {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "pilot_mode": "system-one=code system-two=none",
           "repo_commit": "test", "level_set": "dev", "sources": {}}
AT = "2026-10-04T00:47:00.000Z"


def uri_path(uri: str) -> str | None:
    """The local file a file: URI names, or None if it is not a plain file:/// URI (host, query, fragment)."""
    u = urllib.parse.urlsplit(uri)
    if u.scheme != "file" or u.netloc or u.query or u.fragment or "#" in uri or " " in uri:
        return None
    return urllib.parse.unquote(u.path)


def facets_in(ev: dict):
    """Every (where, name, facet) in a RunEvent: the run's facets and each input and output dataset's."""
    for name, f in ev["run"].get("facets", {}).items():
        yield "run", name, f
    for side in ("inputs", "outputs"):
        for ds in ev[side]:
            for name, f in ds.get("facets", {}).items():
                yield "%s %s" % (side, ds["name"]), name, f


class LineageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.settings = Settings(home=tmp / "sds", doomsat_home=tmp / "doom")
        self.cat = Catalog(self.settings.catalog)
        self.cat.add_episode({"episode_id": EP2, "number": 2, "outcome": "died", "closing_event": "PlayerDied",
                              "closing_ms": CONF2["end_ms"], "start_ms": WINDOW[0], "end_ms": WINDOW[1],
                              "context": CONTEXT})

    def tearDown(self):
        self.cat.close()
        self._tmp.cleanup()

    def register(self, product_type, data: bytes, inputs, run_id="fwd__" + EP2, episode_id=EP2, product_id=None):
        level, ver, ext, media = products.ALGORITHMS[product_type]
        pid = product_id or "%s/%s@%s" % (episode_id, product_type, ver)
        path = episode_product_path(self.settings, episode_id or "l3", product_type, ver, ext)
        return self.cat.register(product_id=pid, episode_id=episode_id, level=level, product_type=product_type,
                                 version=ver, path=path, media_type=media, sha256=sha256(data),
                                 size_bytes=len(data), inputs=inputs, run_id=run_id, code_commit="abc123def456")

    def l1_row(self, run_id="fwd__" + EP2):
        """The L1 product of episode 2 built from the recording, as cataloged: its inputs are three archive reads."""
        arch = archive.RecordedArchive(FIXTURE)
        doc, data = pipeline.make_l1(self.settings, arch, dict(CONF2, window=WINDOW), CONTEXT)
        row, status = self.register("l1_episode", data, doc["inputs"], run_id=run_id)
        self.assertEqual(status, "new")
        return row

    def summary_row(self, l1, run_id="fwd__" + EP2, data=b'{"kills":0}\n'):
        return self.register("l2_summary", data, [{"product_id": l1["product_id"], "sha256": l1["sha256"]}],
                             run_id=run_id)

    def lines(self):
        p = self.settings.lineage / "openlineage.jsonl"
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8")
        self.assertTrue(text == "" or text.endswith("\n"))
        return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------------------------- event

class TestEvent(LineageCase):
    def test_a_run_event_with_job_run_and_spec_fields(self):
        l1 = self.l1_row()
        row, _ = self.summary_row(l1)
        ev = lineage.event(row, "new", at=AT)
        self.assertEqual(ev["eventType"], "COMPLETE")
        self.assertEqual(ev["eventTime"], AT)
        self.assertEqual(ev["producer"], lineage.PRODUCER)
        self.assertTrue(ev["producer"].startswith("https://"))
        self.assertEqual(ev["schemaURL"], lineage.SCHEMA)
        self.assertRegex(ev["schemaURL"], r"^https://openlineage\.io/spec/\d+-\d+-\d+/OpenLineage\.json"
                                          r"#/\$defs/RunEvent$")      # as openlineage-python 1.53 writes it
        self.assertEqual(ev["job"], {"namespace": lineage.NAMESPACE, "name": "l2_summary"})
        run_facet = ev["run"]["facets"]["doomsat_sds"]
        self.assertEqual(run_facet["airflow_run_id"], "fwd__" + EP2)
        self.assertEqual(run_facet["status"], "new")
        self.assertEqual(run_facet["algorithm_version"], products.version("l2_summary"))
        self.assertEqual(run_facet["level"], "L2")
        self.assertEqual(run_facet["episode_id"], EP2)
        self.assertEqual(run_facet["code_commit"], "abc123def456")
        self.assertEqual(json.loads(json.dumps(ev)), ev)          # plain JSON, as emit writes it

    def test_the_run_id_is_a_uuid_fixed_by_airflow_run_and_product(self):
        l1 = self.l1_row()
        row, _ = self.summary_row(l1)
        rid = lineage.event(row, "new", at=AT)["run"]["runId"]
        self.assertEqual(str(uuid.UUID(rid)), rid)                 # canonical lower-case 8-4-4-4-12
        # Recomputed later, with another status or time, or after the bytes changed: the same run is the same id.
        self.assertEqual(lineage.event(row, "changed")["run"]["runId"], rid)
        self.assertEqual(lineage.event(dict(row, sha256="0" * 64), "new", at=AT)["run"]["runId"], rid)
        # Another Airflow run, or another product in the same run, is another id.
        other_run = lineage.event(dict(row, airflow_run_id="reprocess__2026-10-05"), "new", at=AT)["run"]["runId"]
        other_product = lineage.event(l1, "new", at=AT)["run"]["runId"]
        # A product made outside Airflow has no run id to be deterministic on. Whether two such manual runs may
        # share a runId is not settled (lineage.event uses "manual" for all of them), so only its shape is pinned.
        no_run = lineage.event(dict(row, airflow_run_id=None), "new", at=AT)["run"]["runId"]
        self.assertEqual(len({rid, other_run, other_product, no_run}), 4)
        for r in (other_run, other_product, no_run):
            self.assertEqual(str(uuid.UUID(r)), r)

    def test_product_inputs_carry_their_checksum_as_the_dataset_version(self):
        l1 = self.l1_row()
        row, _ = self.summary_row(l1)
        ev = lineage.event(row, "new", at=AT)
        self.assertEqual(len(ev["inputs"]), 1)
        ds = ev["inputs"][0]
        self.assertEqual(ds["namespace"], lineage.NAMESPACE)
        self.assertEqual(ds["name"], l1["product_id"])
        self.assertEqual(ds["facets"]["version"]["datasetVersion"], l1["sha256"])

    def test_archive_reads_become_datasets_named_by_kind_and_window(self):
        l1 = self.l1_row()
        ev = lineage.event(l1, "new", at=AT)
        self.assertEqual(ev["job"]["name"], "l1_episode")
        self.assertEqual([r["kind"] for r in l1["inputs"]], ["parameters", "command_history", "events"])
        self.assertEqual(ev["inputs"], [{"namespace": r["source"], "name": "%s[%s,%s)" % (r["kind"], r["start"],
                                                                                        r["stop"])}
                                        for r in l1["inputs"]])
        source = "yamcs:%s/%s" % (self.settings.yamcs, self.settings.instance)
        self.assertEqual(ev["inputs"][0], {"namespace": source,
                                           "name": "parameters[2026-10-04T00:46:13.533Z,2026-10-04T00:46:47.184Z)"})

    def test_mixed_inputs_keep_their_order(self):
        row = {"product_id": "x/l2_summary@1", "product_type": "l2_summary", "algorithm_version": "1",
               "level": "L2", "sha256": "ab" * 32, "path": "/products/x.json", "airflow_run_id": "r",
               "inputs": [{"source": "capture", "kind": "files", "start": "a", "stop": "b"},
                          {"product_id": "x/l1_episode@1", "sha256": "cd" * 32}]}
        ev = lineage.event(row, "new", at=AT)
        self.assertEqual([d["name"] for d in ev["inputs"]], ["files[a,b)", "x/l1_episode@1"])
        self.assertEqual([d["namespace"] for d in ev["inputs"]], ["capture", lineage.NAMESPACE])

    def test_the_output_is_the_product_file_at_its_checksum(self):
        l1 = self.l1_row()
        row, _ = self.summary_row(l1)
        ev = lineage.event(row, "new", at=AT)
        self.assertEqual(len(ev["outputs"]), 1)
        out = ev["outputs"][0]
        self.assertEqual(out["namespace"], lineage.NAMESPACE)
        self.assertEqual(out["name"], row["product_id"])
        self.assertEqual(out["facets"]["version"]["datasetVersion"], row["sha256"])
        self.assertEqual(uri_path(out["facets"]["dataSource"]["uri"]), row["path"])
        self.assertTrue(os.path.isabs(row["path"]))

    # Regression test for a bug the tests found (now fixed): lineage.py event() builds the dataSource uri as "file://" + path with no percent-encoding. A
    # "#" in $DOOMSAT_SDS_HOME starts a URI fragment and a space is not a legal URI character, so a consumer that
    # follows the uri opens "/srv/doomsat sds/run " instead of the product. Path(path).as_uri() encodes it.
    def test_the_output_uri_names_the_product_file_whatever_its_path(self):
        path = "/srv/doomsat sds/run #2/products/episodes/%s/l2_summary-%s.json" % (EP2, products.version("l2_summary"))
        row = {"product_id": EP2 + "/l2_summary@" + products.version("l2_summary"), "product_type": "l2_summary",
               "algorithm_version": products.version("l2_summary"), "level": "L2", "sha256": "ab" * 32,
               "path": path, "airflow_run_id": "fwd__" + EP2, "inputs": []}
        uri = lineage.event(row, "new", at=AT)["outputs"][0]["facets"]["dataSource"]["uri"]
        self.assertEqual(uri_path(uri), path)

    def test_every_facet_names_its_producer_and_schema(self):
        l1 = self.l1_row()
        row, _ = self.summary_row(l1)
        seen = set()
        for r in (row, l1):
            for where, name, facet in facets_in(lineage.event(r, "new", at=AT)):
                seen.add(name)
                with self.subTest(where=where, facet=name):
                    self.assertEqual(facet["_producer"], lineage.PRODUCER)
                    self.assertTrue(facet["_schemaURL"].startswith("https://"), facet["_schemaURL"])
        self.assertEqual(seen, {"doomsat_sds", "version", "dataSource"})

    def test_an_episode_less_product(self):
        l1 = self.l1_row()
        summary, _ = self.summary_row(l1)
        ver = products.version("l3_rollup")
        row, _ = self.register("l3_rollup", b'{"episodes":1}\n',
                               [{"product_id": summary["product_id"], "sha256": summary["sha256"]}],
                               run_id="rollup__1", episode_id=None, product_id="l3/l3_rollup@%s/0123456789ab" % ver)
        ev = lineage.event(row, "new", at=AT)
        self.assertEqual(ev["job"]["name"], "l3_rollup")
        self.assertIsNone(ev["run"]["facets"]["doomsat_sds"]["episode_id"])
        self.assertEqual(ev["run"]["facets"]["doomsat_sds"]["level"], "L3")
        self.assertEqual(ev["inputs"][0]["name"], summary["product_id"])

    def test_event_time_defaults_to_now_in_utc(self):
        import datetime as dt
        row, _ = self.summary_row(self.l1_row())
        before = dt.datetime.now(dt.timezone.utc)
        t = lineage.event(row, "new")["eventTime"]
        after = dt.datetime.now(dt.timezone.utc)
        self.assertRegex(t, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        when = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        self.assertEqual(when.utcoffset(), dt.timedelta(0))
        # millisecond precision: truncated or rounded, it is within a millisecond of the call
        self.assertLessEqual(before - dt.timedelta(milliseconds=1), when)
        self.assertLessEqual(when, after + dt.timedelta(milliseconds=1))

    def test_the_same_row_gives_the_same_event(self):
        row, _ = self.summary_row(self.l1_row())
        self.assertEqual(lineage.event(row, "new", at=AT), lineage.event(row, "new", at=AT))


# ---------------------------------------------------------------------------------------------- emit

class TestEmit(LineageCase):
    def test_one_line_for_new_and_changed_none_for_unchanged(self):
        l1 = self.l1_row()
        lineage.emit(self.settings, l1, "new")
        row, status = self.summary_row(l1, data=b'{"kills":0}\n')
        self.assertEqual(status, "new")
        lineage.emit(self.settings, row, status)
        self.assertEqual(len(self.lines()), 2)
        retry, status = self.summary_row(l1, run_id="fwd__retry", data=b'{"kills":0}\n')
        self.assertEqual(status, "unchanged")
        lineage.emit(self.settings, retry, status)
        self.assertEqual(len(self.lines()), 2)
        moved, status = self.summary_row(l1, run_id="reprocess__1", data=b'{"kills":1}\n')
        self.assertEqual(status, "changed")
        lineage.emit(self.settings, moved, status)
        lines = self.lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([ev["outputs"][0]["name"] for ev in lines],
                         [l1["product_id"], row["product_id"], row["product_id"]])
        self.assertEqual([ev["run"]["facets"]["doomsat_sds"]["status"] for ev in lines], ["new", "new", "changed"])
        self.assertEqual(lines[2]["outputs"][0]["facets"]["version"]["datasetVersion"], sha256(b'{"kills":1}\n'))
        self.assertEqual(lines[2]["run"]["runId"], lineage.event(moved, "changed")["run"]["runId"])
        for ev in lines:
            self.assertEqual(ev["eventType"], "COMPLETE")

    def test_the_file_is_under_settings_lineage_and_only_appended_to(self):
        self.assertFalse(self.settings.lineage.exists())
        l1 = self.l1_row()
        lineage.emit(self.settings, l1, "unchanged")
        self.assertIsNone(self.lines())                         # nothing written, not even an empty file
        lineage.emit(self.settings, l1, "new")
        first = (self.settings.lineage / "openlineage.jsonl").read_text(encoding="utf-8")
        row, _ = self.summary_row(l1)
        lineage.emit(self.settings, row, "new")
        text = (self.settings.lineage / "openlineage.jsonl").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(first))
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(self.settings.lineage, self.settings.home / "lineage")

    def test_an_unwritable_lineage_directory_does_not_raise(self):
        row, _ = self.summary_row(self.l1_row())
        self.settings.home.mkdir(parents=True, exist_ok=True)
        self.settings.lineage.write_text("not a directory\n")       # a file where the directory should be
        lineage.emit(self.settings, row, "new")
        lineage.emit(self.settings, row, "changed")
        self.assertEqual(self.settings.lineage.read_text(), "not a directory\n")

    def test_an_unwritable_lineage_file_does_not_raise(self):
        row, _ = self.summary_row(self.l1_row())
        (self.settings.lineage / "openlineage.jsonl").mkdir(parents=True)   # a directory where the file should be
        lineage.emit(self.settings, row, "new")
        self.assertTrue((self.settings.lineage / "openlineage.jsonl").is_dir())


if __name__ == "__main__":
    unittest.main()
