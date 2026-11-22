"""Phase E: publishing back into Yamcs, so the displays can reach the products.

- Products are copied to the bucket `doomsat-sds` (episodes/<id>/<type>-<version>.<ext>, l3/rollup-current.json,
  quicklook/latest.{png,json}); the dashboard reaches them same-origin through tools/serve_dashboard.py's /api
  proxy, Open MCT straight from Yamcs.
- Each episode gets one Timeline item (source "rdb", type EVENT) spanning its window, whose properties link to its
  current products. The item id is a uuid5 of the episode id, and POSTing a known id overwrites the item, so
  publishing is idempotent and a reprocessing campaign simply republishes with the new links.

The REST API is used directly: yamcs-client 2.1.0 cannot create Timeline items on Yamcs 5.12.8 (its save_item is
a PUT that only updates, with field numbers the server reads differently).

Bucket limits on 5.12.8: 1000 objects and 100 MB by default; a deleted bucket turns into an undeletable ghost, so
the bucket is created once and only objects are replaced. The bucket holds the current products only: publishing
an episode deletes its copies of versions that are no longer current (they stay in the product store and the
catalog), so it holds three objects per episode, about 330 episodes before the default cap; past that, give the
bucket a larger maxObjects in Yamcs's configuration. L1 records are not copied (about 3 KB per second of play,
they would fill 100 MB after a few hundred episodes); displays need the L2 and L3 products.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from . import products
from .catalog import Catalog
from .config import Settings

BUCKET = "doomsat-sds"
BAND_NAME = "DoomSat episodes (SDS)"
TAG = "doomsat-sds"
PUBLISHED_TYPES = products.L2_TYPES
OUTCOME_COLOURS = {"died": "#e05050", "level_finished": "#50c070", "reset": "#909090", "wad_switch": "#a070d0",
                   "interrupted": "#909090"}


def item_id(episode_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "doomsat-sds/episode/" + episode_id))


def object_name(row: dict) -> str:
    """Where a catalog product row goes in the bucket (names must match Yamcs's [ \\w\\s\\-./]+)."""
    ext = products.ALGORITHMS[row["product_type"]][2]
    return "episodes/%s/%s-%s.%s" % (row["episode_id"], row["product_type"], row["algorithm_version"], ext)


def object_url(settings: Settings, name: str) -> str:
    return "%s/api/storage/buckets/%s/objects/%s" % (settings.yamcs_url, BUCKET, name)


def timeline_item(settings: Settings, episode: dict, current: dict[str, dict], summary: dict | None) -> dict:
    """The CreateItemRequest for one episode. `current` maps product type -> its current catalog row."""
    props = {"episode_id": episode["episode_id"], "outcome": episode["outcome"], "number": str(episode["number"]),
             "wad": str(episode.get("wad")), "map": str(episode.get("map")), "level_set": str(episode.get("level_set")),
             "backgroundColor": OUTCOME_COLOURS.get(episode["outcome"], "#7090c0")}
    if summary:
        props.update(kills=str(summary.get("kills")), explored_cells=str(summary.get("explored_cells")),
                     distance_units=str(summary.get("distance_units")))
    links = []
    for ptype, row in sorted(current.items()):
        url = object_url(settings, object_name(row))
        props["product." + ptype] = url
        links.append("%s v%s: %s" % (ptype, row["algorithm_version"], url))
    return {
        "source": "rdb",
        "id": item_id(episode["episode_id"]),
        "type": "EVENT",
        "name": "Episode %d %s (%s %s)" % (episode["number"], episode["outcome"].replace("_", " "),
                                          episode.get("wad") or "?", episode.get("map") or "?"),
        "start": episode["start_utc"],
        "duration": "%.3fs" % episode["duration_s"],
        "tags": [TAG, "episode", episode["outcome"]] + ([episode["level_set"]] if episode.get("level_set") else []),
        "description": "DoomSat SDS episode %s\n%s" % (episode["episode_id"], "\n".join(links)),
        "properties": props,
    }


class Yamcs:
    """The handful of REST calls Phase E makes."""

    def __init__(self, settings: Settings, session=None):
        import requests
        self.base = settings.yamcs_url + "/api"
        self.instance = settings.instance
        self.http = session or requests.Session()

    def ensure_bucket(self) -> None:
        r = self.http.get("%s/storage/buckets/%s" % (self.base, BUCKET), timeout=10)
        if r.status_code == 404:
            self.http.post("%s/storage/buckets" % self.base, json={"name": BUCKET}, timeout=10).raise_for_status()
        else:
            r.raise_for_status()

    def bucket_info(self) -> dict:
        r = self.http.get("%s/storage/buckets/%s" % (self.base, BUCKET), timeout=10)
        r.raise_for_status()
        return r.json()

    def upload(self, name: str, data: bytes, content_type: str) -> None:
        r = self.http.post("%s/storage/buckets/%s/objects/%s" % (self.base, BUCKET, name),
                           files={name: (name.rsplit("/", 1)[-1], data, content_type)}, timeout=30)
        r.raise_for_status()

    def names(self, prefix: str) -> list[str]:
        r = self.http.get("%s/storage/buckets/%s/objects" % (self.base, BUCKET), params={"prefix": prefix}, timeout=10)
        r.raise_for_status()
        return [o["name"] for o in r.json().get("objects", [])]

    def delete(self, name: str) -> None:
        r = self.http.delete("%s/storage/buckets/%s/objects/%s" % (self.base, BUCKET, name), timeout=10)
        if r.status_code != 404:
            r.raise_for_status()

    def ensure_band(self) -> None:
        r = self.http.get("%s/timeline/%s/bands" % (self.base, self.instance), timeout=10)
        r.raise_for_status()
        if any(b.get("name") == BAND_NAME for b in r.json().get("bands", [])):
            return
        self.http.post("%s/timeline/%s/bands" % (self.base, self.instance), timeout=10, json={
            "name": BAND_NAME, "shared": True, "source": "rdb", "type": "ITEM_BAND", "tags": [TAG],
            "description": "One item per episode, linking to its SDS products"}).raise_for_status()

    def save_item(self, item: dict) -> dict:
        r = self.http.post("%s/timeline/%s/items" % (self.base, self.instance), json=item, timeout=10)
        r.raise_for_status()
        return r.json()

    def items(self, start: str | None = None, stop: str | None = None) -> list[dict]:
        params = {"source": "rdb", "limit": 500, "details": "true"}
        if start:
            params["start"] = start
        if stop:
            params["stop"] = stop
        r = self.http.get("%s/timeline/%s/items" % (self.base, self.instance), params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("items", [])


def publish_episode(settings: Settings, catalog: Catalog, episode_id: str, yamcs: Yamcs | None = None) -> dict:
    """Copy the episode's current L2 products to the bucket and write (or rewrite) its Timeline item."""
    import json
    yamcs = yamcs or Yamcs(settings)
    episode = catalog.episode(episode_id)
    if episode is None:
        raise LookupError("episode %s is not in the catalog" % episode_id)
    yamcs.ensure_bucket()
    current = {}
    for ptype in PUBLISHED_TYPES:
        row = catalog.current(episode_id, ptype)
        if row is None:
            continue
        yamcs.upload(object_name(row), Path(row["path"]).read_bytes(), row["media_type"])
        current[ptype] = row
    summary = json.loads(Path(current["l2_summary"]["path"]).read_text()) if "l2_summary" in current else None
    yamcs.ensure_band()
    saved = yamcs.save_item(timeline_item(settings, episode, current, summary))
    keep = {object_name(r) for r in current.values()}
    removed = [n for n in yamcs.names("episodes/%s/" % episode_id) if n not in keep]
    for n in removed:
        yamcs.delete(n)                         # superseded versions: the bucket holds what is current
    return {"episode_id": episode_id, "item_id": saved.get("id"), "item_name": saved.get("name"),
            "objects": sorted(keep), "removed": removed}


def publish_file(settings: Settings, name: str, path: str | Path, content_type: str, yamcs: Yamcs | None = None) -> str:
    """Copy one file (the current rollup, the latest quicklook) to a fixed object name."""
    yamcs = yamcs or Yamcs(settings)
    yamcs.ensure_bucket()
    yamcs.upload(name, Path(path).read_bytes(), content_type)
    return object_url(settings, name)
