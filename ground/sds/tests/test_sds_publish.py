"""Phase E: what the SDS writes back into Yamcs, a Timeline item per episode and copies of its products.

Yamcs 5.12.8 is particular: a Timeline item is created by POST with a CreateItemRequest (source "rdb", a UUID id,
a duration written as "<seconds>s", string-only properties), a POST with a known id overwrites the item, object
names must match [ \\w\\s\\-./]+, and a bucket holds at most 1000 objects. So the item id must be a stable uuid5,
publishing twice must change nothing, and superseded versions must leave the bucket (they stay in the catalog).
"""
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import config, products, publish  # noqa: E402
from doomsat_sds.catalog import Catalog  # noqa: E402
from doomsat_sds.store import canonical_json, sha256, write_atomic  # noqa: E402

EID = "20261004T004647Z-e0002"
OBJECT_NAME = re.compile(r"^[ \w\s\-./]+$")      # Yamcs 5.12.8 BucketsApi


class Response:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body or {}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class FakeYamcs:
    """The handful of REST routes publish uses, kept in memory."""

    def __init__(self):
        self.buckets, self.objects, self.items, self.bands = set(), {}, {}, []

    def get(self, url, params=None, timeout=None):
        if url.endswith("/storage/buckets/" + publish.BUCKET):
            return Response(200 if publish.BUCKET in self.buckets else 404, {"name": publish.BUCKET})
        if url.endswith("/storage/buckets/%s/objects" % publish.BUCKET):
            prefix = (params or {}).get("prefix", "")
            return Response(body={"objects": [{"name": n} for n in sorted(self.objects) if n.startswith(prefix)]})
        if url.endswith("/bands"):
            return Response(body={"bands": self.bands})
        raise AssertionError("unexpected GET " + url)

    def post(self, url, json=None, files=None, timeout=None):
        if url.endswith("/storage/buckets"):
            self.buckets.add(json["name"])
            return Response()
        if "/storage/buckets/%s/objects/" % publish.BUCKET in url:
            name = url.split("/objects/", 1)[1]
            (field, (_, data, ctype)), = files.items()
            assert field == name
            self.objects[name] = (data, ctype)
            return Response()
        if url.endswith("/bands"):
            self.bands.append(json)
            return Response()
        if url.endswith("/items"):
            self.items[json["id"]] = json                  # a known id overwrites, as Yamcs does
            return Response(body={"id": json["id"], "name": json["name"]})
        raise AssertionError("unexpected POST " + url)

    def delete(self, url, timeout=None):
        name = url.split("/objects/", 1)[1]
        return Response(200 if self.objects.pop(name, None) is not None else 404)


class PublishCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = config.Settings(home=Path(tmp.name) / "sds", doomsat_home=Path(tmp.name) / "doom")
        self.catalog = Catalog(self.settings.catalog)
        self.addCleanup(self.catalog.close)
        self.catalog.add_episode({
            "episode_id": EID, "number": 2, "outcome": "died", "closing_event": "PlayerDied", "closing_ms": 1,
            "start_utc": "2026-10-04T00:46:13.533Z", "end_utc": "2026-10-04T00:46:47.183Z", "start_ms": 0,
            "end_ms": 1, "duration_s": 33.65, "wad": "freedoom1.wad", "map": "E1M1", "level_set": "dev",
            "context": {"wad": "freedoom1.wad"}})
        self.fake = FakeYamcs()
        self.yamcs = publish.Yamcs(self.settings, session=self.fake)

    def product(self, ptype, version, doc=None):
        level, _, ext, media = products.ALGORITHMS[ptype]
        data = canonical_json(doc or {"product": {"type": ptype}, "kills": 3, "explored_cells": 128,
                                      "distance_units": 1234.5}) if ext == "json" else b"\x89PNG fake"
        path = write_atomic(self.settings.products / "episodes" / EID / ("%s-%s.%s" % (ptype, version, ext)), data)
        self.catalog.register(product_id="%s/%s@%s" % (EID, ptype, version), episode_id=EID, level=level,
                              product_type=ptype, version=version, path=path, media_type=media, sha256=sha256(data),
                              size_bytes=len(data), inputs=[])


class TestTimelineItem(PublishCase):
    def test_it_is_a_create_item_request_yamcs_5_12_accepts(self):
        for t in products.L2_TYPES:
            self.product(t, "1.0.0")
        current = {t: self.catalog.current(EID, t) for t in products.L2_TYPES}
        item = publish.timeline_item(self.settings, self.catalog.episode(EID), current, {"kills": 3})
        self.assertEqual(item["source"], "rdb")
        self.assertEqual(item["type"], "EVENT")
        self.assertEqual(item["id"], str(uuid.uuid5(uuid.NAMESPACE_URL, "doomsat-sds/episode/" + EID)))
        self.assertEqual(uuid.UUID(item["id"]).version, 5)
        self.assertEqual(item["start"], "2026-10-04T00:46:13.533Z")
        self.assertEqual(item["duration"], "33.650s")
        self.assertIn(publish.TAG, item["tags"])
        self.assertTrue(all(isinstance(v, str) for v in item["properties"].values()), item["properties"])
        for t in products.L2_TYPES:
            self.assertTrue(item["properties"]["product." + t].endswith(publish.object_name(current[t])))

    def test_object_names_are_legal_in_a_yamcs_bucket(self):
        for t in products.L2_TYPES:
            self.product(t, products.version(t))
            self.assertRegex(publish.object_name(self.catalog.current(EID, t)), OBJECT_NAME)


class TestPublishEpisode(PublishCase):
    def test_current_products_go_up_once_and_the_item_is_saved(self):
        for t in products.L2_TYPES:
            self.product(t, "1.0.0")
        out = publish.publish_episode(self.settings, self.catalog, EID, yamcs=self.yamcs)
        self.assertEqual(sorted(self.fake.objects), sorted(out["objects"]))
        self.assertEqual(len(self.fake.objects), len(products.L2_TYPES))
        self.assertEqual(self.fake.objects["episodes/%s/l2_path-1.0.0.png" % EID][1], "image/png")
        self.assertEqual(list(self.fake.items), [publish.item_id(EID)])
        self.assertEqual(len(self.fake.bands), 1)
        again = publish.publish_episode(self.settings, self.catalog, EID, yamcs=self.yamcs)
        self.assertEqual((again["removed"], len(self.fake.items), len(self.fake.bands)), ([], 1, 1))

    def test_a_campaign_leaves_only_the_current_version_in_the_bucket(self):
        # Review finding: every version ever published stayed in the bucket, which fills at 1000 objects, and then
        # every forward run's publish task fails.
        for t in products.L2_TYPES:
            self.product(t, "1.0.0")
        publish.publish_episode(self.settings, self.catalog, EID, yamcs=self.yamcs)
        self.product("l2_path", "2.0.0")
        out = publish.publish_episode(self.settings, self.catalog, EID, yamcs=self.yamcs)
        self.assertEqual(out["removed"], ["episodes/%s/l2_path-1.0.0.png" % EID])
        self.assertIn("episodes/%s/l2_path-2.0.0.png" % EID, self.fake.objects)
        self.assertNotIn("episodes/%s/l2_path-1.0.0.png" % EID, self.fake.objects)
        self.assertEqual(len(self.fake.objects), len(products.L2_TYPES))
        item = self.fake.items[publish.item_id(EID)]
        self.assertTrue(item["properties"]["product.l2_path"].endswith("l2_path-2.0.0.png"))
        # the old version is still in the catalog and on disk
        self.assertEqual(len(self.catalog.products(EID, "l2_path")), 2)

    def test_another_episodes_objects_are_never_touched(self):
        self.fake.objects["episodes/%sX/l2_path-1.0.0.png" % EID] = (b"", "image/png")
        self.product("l2_summary", "1.0.0")
        publish.publish_episode(self.settings, self.catalog, EID, yamcs=self.yamcs)
        self.assertIn("episodes/%sX/l2_path-1.0.0.png" % EID, self.fake.objects)


if __name__ == "__main__":
    unittest.main()
