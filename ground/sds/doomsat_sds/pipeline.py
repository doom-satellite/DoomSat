"""The steps the DAGs run, each a plain function of (settings, archive, catalog, ...) returning a small dict.

The dicts are what passes between Airflow tasks (XCom), so they stay small: ids, paths, checksums. The heavy
data is in the product files. Every step is idempotent: run it twice and the second run finds the same bytes
and changes nothing (Catalog.register returns "unchanged").

    watch      closed episodes in the archive that the catalog does not have yet
    locate     the episode's window in the archive
    l1         read the window, write the L1 record, catalog the episode and the product
    l2         one L2 product from an L1 product
    rollup     the L3 product over every current L2 summary
    reprocess  for one cataloged episode: rebuild L1 from the archive, check it reproduces, build missing L2s
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import config, context as context_mod, lineage, products
from .archive import Archive, iso, link_parameters
from .catalog import Catalog
from .config import Settings
from .episodes import END_PAD_MS, ClosedEpisode, closed_episodes, locate
from .store import (canonical_json, episode_product_path, l3_product_path, quicklook_path, read_json, sha256,
                    write_atomic)

# The Doom events kept in L1 (IntentSet only echoes commands L1 already has). Main's WAD events (WadLoaded,
# WadLoadFailed, ...) are left out on purpose: L1 embeds this list in its inputs, so adding a name changes every L1's
# bytes, not just a switched episode's, and would mean bumping l1_episode and every L2 with it. A switch is in the
# closure's outcome ("wad_switch") and the WAD flown in the context instead.
L1_EVENTS = tuple(config.EVENT_PREFIX + n for n in (
    "EpisodeStarted", "PlayerDied", "LevelFinished", "LevelStarted", "KeyPickedUp", "GoalSet", "ExploreHint",
    "PayloadConnected", "PayloadLost", "FrameTooLarge", "BadPayloadMessage"))
WATCH_MARGIN_S = 600        # events read before the look-back, so a closure near its edge has its context
SETTLE_S = 10               # leave an episode this long after it closes, so its last samples are archived


def now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def open_env(archive: bool = True):
    """(settings, archive, catalog) from the environment scripts/sds.sh sets up; what each DAG task starts with."""
    settings = Settings.from_env()
    arch = None
    if archive:
        from .archive import YamcsArchive
        arch = YamcsArchive(settings.yamcs, settings.instance)
    return settings, arch, Catalog(settings.catalog)


def _commit(settings: Settings) -> str | None:
    return context_mod.repo_commit(settings.repo)


# ------------------------------------------------------------------------------------------------ watch

def watch(settings: Settings, archive: Archive, catalog: Catalog, lookback_s: int = 6 * 3600,
          at_ms: int | None = None) -> list[dict]:
    """Closed episodes ending in the look-back window that have no L1 product yet, oldest first."""
    at_ms = now_ms() if at_ms is None else at_ms
    scan_start = at_ms - (lookback_s + WATCH_MARGIN_S) * 1000
    events = archive.events(scan_start, at_ms, types=[config.EVENT_PREFIX + n for n in config.EPISODE_EVENTS])
    known = [(e["number"], e["closing_ms"]) for e in catalog.episodes()
             if catalog.current(e["episode_id"], "l1_episode") is not None]
    out = []
    for ep in closed_episodes(events, scan_start_ms=scan_start):
        if ep.end_ms < at_ms - lookback_s * 1000 or ep.end_ms > at_ms - SETTLE_S * 1000:
            continue
        if catalog.current(ep.episode_id, "l1_episode") is not None:
            continue
        # The same episode closed by a slightly different event (say the closure rules changed): already cataloged.
        if any(n == ep.number and abs(t - ep.end_ms) <= SAME_EPISODE_MS for n, t in known):
            continue
        if ep.inferred and not confirmed(archive, ep):
            continue
        out.append(ep.as_conf())
    return out


SAME_EPISODE_MS = 10_000
FORWARD_ATTEMPTS = 3


def forward_run_id(episode_id: str, state, attempts: int = FORWARD_ATTEMPTS) -> tuple[str | None, str]:
    """The run id to trigger for an episode, or None, with the reason.

    `state(run_id)` is the state of an existing sds_forward run, or None if there is none. A run that is queued,
    running or done is left alone; a failed one (Yamcs down through all its task retries, say) is followed by
    fwd__<id>__retry1, then __retry2, and after that the episode is given up on for a person to look at.
    """
    for k in range(attempts):
        run_id = "fwd__" + episode_id + ("" if k == 0 else "__retry%d" % k)
        st = state(run_id)
        if st is None:
            return run_id, "new" if k == 0 else "retry %d" % k
        if st != "failed":
            return None, "%s is %s" % (run_id, st)
    return None, "gave up after %d failed runs" % attempts


def confirmed(archive: Archive, ep: ClosedEpisode) -> bool:
    """Is an inferred closure real? Checked against the archived EPISODE and TIC samples around it."""
    if ep.closing == "PayloadConnected":
        # A payload restart starts the game again: TIC falls back, or EPISODE changes. A socket drop and reconnect
        # carries on with the same episode and a TIC that keeps rising: no closure.
        s = archive.parameters(["EPISODE", "TIC"], ep.end_ms - 30_000, ep.end_ms + 30_000)
        before = [(t, v) for t, _, v in s["TIC"] if t <= ep.end_ms]
        after = [(t, v) for t, _, v in s["TIC"] if t > ep.end_ms]
        if not before or before[-1][1] is None:
            return False
        if not after:
            return True                          # nothing after it: that game is over
        if after[0][1] < before[-1][1]:
            return True
        return any(t > ep.end_ms and v != ep.number for t, _, v in s["EPISODE"])
    # Only an inference from a start event: confirm the episode before it really was in progress.
    s = archive.parameters(["EPISODE"], ep.end_ms - 10_000, ep.end_ms)["EPISODE"]
    return any(v == ep.number for _, _, v in s)


# ------------------------------------------------------------------------------------------------ locate

def locate_episode(archive: Archive, conf: dict) -> dict:
    """conf (from watch) plus its window [first_ms, last_ms]. Looks further back until the run is whole."""
    ep = ClosedEpisode.from_conf(conf)
    lookback = 15 * 60 * 1000
    while True:
        start = ep.end_ms - lookback
        s = archive.parameters(["EPISODE", "TIC"], start, ep.end_ms + END_PAD_MS + 1)
        win = locate(s["EPISODE"], s["TIC"], ep)
        if win is not None and win[0] - start > 5_000:
            return dict(conf, window=[win[0], win[1]])
        if lookback >= 4 * 3600 * 1000:
            if win is not None:          # a four-hour episode: take what the archive has
                return dict(conf, window=[win[0], win[1]])
            raise LookupError("no EPISODE == %d samples before %s" % (ep.number, iso(ep.end_ms)))
        lookback *= 2


# ------------------------------------------------------------------------------------------------ level 1

def make_l1(settings: Settings, archive: Archive, located: dict, context: dict,
            like: list | None = None) -> tuple[dict, bytes]:
    """Read the archive for one located episode and build its L1 document (not written anywhere yet).

    `like` is an earlier L1's `inputs`: when given (reprocessing), the archive is asked for exactly what that L1
    asked for, under the same source name, so a renamed host or another Yamcs address does not make an episode
    look unreproducible.
    """
    ep = ClosedEpisode.from_conf(located)
    start, end = located["window"]
    earlier = next((i for i in like or [] if i.get("kind") == "parameters"), None)
    if earlier is not None:
        ground = earlier["names"][-1]
        source = earlier["source"]
    else:
        ground = link_parameters(archive.server_id())[0]
        source = "yamcs:%s/%s" % (settings.yamcs, settings.instance)
    names = list(config.SCIENCE) + list(config.LINK) + [ground]
    series = archive.parameters(names, start, end + 1)
    science = {n: series[n] for n in config.SCIENCE}
    link = {n: series[n] for n in config.LINK}
    link[products.GROUND_LINK] = series[ground]
    off = products.clock_offset(science)["tm_minus_ground_ms"]
    commands = archive.commands(start - off - 2000, end - off + 2000)
    events = archive.events(start, end + END_PAD_MS + 1, types=list(L1_EVENTS))
    inputs = [
        {"source": source, "kind": "parameters", "names": names, "start": iso(start), "stop": iso(end + 1)},
        {"source": source, "kind": "command_history", "start": iso(start - off - 2000), "stop": iso(end - off + 2000)},
        {"source": source, "kind": "events", "types": list(L1_EVENTS), "start": iso(start),
         "stop": iso(end + END_PAD_MS + 1)},
    ]
    doc = products.build_l1(ep, (start, end), science, link, commands, events, context, inputs)
    return doc, canonical_json(doc)


def _episode_row(located: dict, context: dict) -> dict:
    ep = ClosedEpisode.from_conf(located)
    start, end = located["window"]
    return {"episode_id": ep.episode_id, "number": ep.number, "outcome": ep.outcome, "closing_event": ep.closing,
            "inferred_close": int(ep.inferred), "closing_ms": ep.end_ms, "start_event_ms": ep.start_event_ms,
            "start_utc": iso(start), "end_utc": iso(end), "start_ms": start, "end_ms": end,
            "duration_s": round((end - start) / 1000, 3), "wad": context.get("wad"), "map": context.get("map"),
            "skill": context.get("skill"), "seed": context.get("seed"), "pilot_mode": context.get("pilot_mode"),
            "repo_commit": context.get("repo_commit"), "level_set": context.get("level_set"), "context": context}


def _register(settings: Settings, catalog: Catalog, *, product_type: str, episode_id: str | None, product_id: str,
              path, data: bytes, inputs: list, run_id: str | None) -> dict:
    level, ver, _, media = products.ALGORITHMS[product_type]
    write_atomic(path, data)
    row, status = catalog.register(product_id=product_id, episode_id=episode_id, level=level,
                                   product_type=product_type, version=ver, path=path, media_type=media,
                                   sha256=sha256(data), size_bytes=len(data), inputs=inputs, run_id=run_id,
                                   code_commit=_commit(settings))
    lineage.emit(settings, row, status)
    return {"product_id": row["product_id"], "episode_id": episode_id, "path": row["path"], "sha256": row["sha256"],
            "status": status, "product_type": product_type, "version": ver}


def l1(settings: Settings, archive: Archive, catalog: Catalog, located: dict, run_id: str | None = None) -> dict:
    """Build, write and catalog the L1 record of a located episode (the forward path)."""
    ep = ClosedEpisode.from_conf(located)
    known = catalog.episode(ep.episode_id)
    if known:
        ctx = known["context"]
    else:
        # The WAD channels are a read of their own, so L1's inputs stay what they were and a mission database
        # without them (a flight build from before main) costs only the context's WAD source.
        flown = context_mod.flown_wad(archive, located["window"], ep.end_ms)
        ctx = context_mod.capture(settings, ep.number, located["window"][0], flown=flown)
    doc, data = make_l1(settings, archive, located, ctx)
    catalog.add_episode(_episode_row(located, ctx))
    ver = products.version("l1_episode")
    return _register(settings, catalog, product_type="l1_episode", episode_id=ep.episode_id,
                     product_id=f"{ep.episode_id}/l1_episode@{ver}",
                     path=episode_product_path(settings, ep.episode_id, "l1_episode", ver, "json"),
                     data=data, inputs=doc["inputs"], run_id=run_id)


# ------------------------------------------------------------------------------------------------ level 2

BUILDERS = {
    "l2_path": lambda l1doc: products.build_path_png(l1doc),
    "l2_summary": lambda l1doc: canonical_json(products.build_summary(l1doc)),
    "l2_linkstats": lambda l1doc: canonical_json(products.build_linkstats(l1doc)),
}


def l2(settings: Settings, catalog: Catalog, l1_ref: dict, product_type: str, run_id: str | None = None) -> dict:
    """One L2 product from an L1 product (`l1_ref` as returned by l1() or reprocess)."""
    l1doc = read_json(l1_ref["path"])
    data = BUILDERS[product_type](l1doc)
    eid = l1doc["episode"]["id"]
    _, ver, ext, _ = products.ALGORITHMS[product_type]
    return _register(settings, catalog, product_type=product_type, episode_id=eid,
                     product_id=f"{eid}/{product_type}@{ver}",
                     path=episode_product_path(settings, eid, product_type, ver, ext), data=data,
                     inputs=[{"product_id": l1_ref["product_id"], "sha256": l1_ref["sha256"]}], run_id=run_id)


# ------------------------------------------------------------------------------------------------ level 3

def rollup(settings: Settings, catalog: Catalog, run_id: str | None = None) -> dict:
    current = catalog.products(product_type="l2_summary", current_only=True)
    ids, shas = [p["product_id"] for p in current], [p["sha256"] for p in current]
    doc = products.build_rollup([read_json(p["path"]) for p in current], ids)
    h = products.rollup_inputs_hash(ids, shas)
    ver = products.version("l3_rollup")
    return _register(settings, catalog, product_type="l3_rollup", episode_id=None,
                     product_id=f"l3/l3_rollup@{ver}/{h[:12]}", path=l3_product_path(settings, "l3_rollup", ver, h),
                     data=canonical_json(doc), inputs=[{"product_id": i, "sha256": s} for i, s in zip(ids, shas)],
                     run_id=run_id)


# ------------------------------------------------------------------------------------------------ reprocess

def reprocess(settings: Settings, archive: Archive, catalog: Catalog, episode_id: str,
              types=products.L2_TYPES, run_id: str | None = None) -> dict:
    """Bring one cataloged episode up to the current algorithm versions, starting again from the archive.

    L1 is rebuilt from the archive with the cataloged window and context. If its algorithm version is the
    cataloged one, the bytes must match the cataloged checksum: a mismatch means the archive (or the reader)
    no longer returns what it returned, and that is a finding. The rebuilt file is kept beside the original
    (never over it) and the new L2s are built from it, so they are what the archive says today.
    Every L2 type without a product at its current version is then built and becomes current; older versions
    stay in the catalog. An L2 that exists at its current version but was built from another L1 version (L1 was
    bumped, the L2 was not) is reported as stale and left alone: rebuilding it under the same id would overwrite a
    product with different bytes. Bump its version to rebuild it.
    """
    row = catalog.episode(episode_id)
    if row is None:
        raise LookupError("episode %s is not in the catalog" % episode_id)
    located = {"number": row["number"], "end_ms": row["closing_ms"], "closing": row["closing_event"],
               "outcome": row["outcome"], "start_event_ms": row["start_event_ms"],
               "inferred": bool(row["inferred_close"]), "window": [row["start_ms"], row["end_ms"]]}
    earlier = catalog.current(episode_id, "l1_episode")
    doc, data = make_l1(settings, archive, located, row["context"], like=earlier["inputs"] if earlier else None)
    ver = products.version("l1_episode")
    pid = f"{episode_id}/l1_episode@{ver}"
    old = catalog.product(pid)
    report = {"episode_id": episode_id, "l1": None, "built": [], "up_to_date": [], "stale": []}
    if old is None:
        l1_ref = _register(settings, catalog, product_type="l1_episode", episode_id=episode_id, product_id=pid,
                           path=episode_product_path(settings, episode_id, "l1_episode", ver, "json"), data=data,
                           inputs=doc["inputs"], run_id=run_id)
        report["l1"] = "built at new version %s" % ver
    elif old["sha256"] == sha256(data):
        l1_ref = {"product_id": pid, "path": old["path"], "sha256": old["sha256"]}
        report["l1"] = "reproduced (sha256 %s)" % old["sha256"][:12]
    else:
        rebuilt = episode_product_path(settings, episode_id, "l1_episode", ver, "rebuilt-%s.json" % sha256(data)[:12])
        write_atomic(rebuilt, data)
        catalog.add_finding("l1_not_reproduced", {"cataloged_sha256": old["sha256"], "rebuilt_sha256": sha256(data),
                                                  "rebuilt_path": str(rebuilt), "run": run_id},
                            episode_id=episode_id, product_id=pid)
        l1_ref = {"product_id": pid, "path": str(rebuilt), "sha256": sha256(data)}
        report["l1"] = "NOT reproduced: rebuilt %s, cataloged %s" % (sha256(data)[:12], old["sha256"][:12])
    for t in types:
        existing = catalog.product(f"{episode_id}/{t}@{products.version(t)}")
        if existing is not None:
            sources = [(i.get("product_id"), i.get("sha256")) for i in existing["inputs"]]
            if pid not in [p for p, _ in sources]:
                report["stale"].append(t)            # made from another L1 version: bump t's version to rebuild
                continue
            if (pid, l1_ref["sha256"]) in sources:
                report["up_to_date"].append(t)       # made from these very bytes
                continue
            # Same L1 version, other bytes: either the archive now reads differently (L1 not reproduced), or an
            # earlier campaign built this L2 from an L1 that did not reproduce and the cataloged L1 is back.
        report["built"].append(l2(settings, catalog, l1_ref, t, run_id)["product_id"])
    return report


# ------------------------------------------------------------------------------------------------ quicklook

QL_CHANNELS = ("PAYLOAD_LINK", "EPISODE", "HEALTH", "KILLS", "EXPLORED_CELLS", "TIC", "FRAMES_SENT")
QL_RETENTION_H = 24


def frames_sent_between(series: list, start_ground_ms: int, stop_ground_ms: int) -> int | None:
    """How far FRAMES_SENT rose between two ground-clock times.

    The capture service counts by the ground's clock; FRAMES_SENT is stamped with F´ time, which an unpatched
    fprime-yamcs puts about a second ahead (docs/plans/fprime-yamcs-time.md).
    The offset is measured from the series itself (generation minus reception), and the counter's rises are
    summed between the two edges, so a restart of the flight software (the counter falling back to zero) inside
    the window costs only the frames sent between its last sample and the restart, never a negative count.
    """
    if len(series) < 2:
        return None
    offsets = sorted(g - r for g, r, _ in series if r is not None)
    off = offsets[len(offsets) // 2] if offsets else 0
    # the counter as it stood at the start edge (its last value at or before it), then every value up to the end
    before = [v for g, _, v in series if g <= start_ground_ms + off]
    inside = [v for g, _, v in series if start_ground_ms + off < g <= stop_ground_ms + off]
    values = before[-1:] + inside
    if len(values) < 2:
        return None
    return sum(b - a for a, b in zip(values, values[1:]) if b > a)


def quicklook(settings: Settings, catalog: Catalog, run_id: str | None = None, at_ms: int | None = None) -> dict:
    """Health and a contact sheet from the capture directory and a few realtime values (Phase B)."""
    import json
    from . import quicklook as ql
    from .archive import YamcsArchive, to_ms, value
    at_ms = now_ms() if at_ms is None else at_ms
    stamp = dt.datetime.fromtimestamp(at_ms / 1000, tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    status_path = settings.capture / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else None
    status_age = (now_ms() - status["updated_ms"]) / 1000 if status and status.get("updated_ms") else None
    window = ql.last_minutes(settings.capture / "stats.jsonl", stamp[:13])
    realtime, links, frames_sent, problems = {}, {}, None, []
    try:
        arch = YamcsArchive(settings.yamcs, settings.instance)
        proc = arch.client.get_processor(settings.instance, config.PROCESSOR)
        names = [config.qualified(n) for n in QL_CHANNELS]
        values = proc.get_parameter_values(names)
        asked = now_ms()
        for n, v in zip(names, values):
            if v is not None:
                age = (asked - to_ms(v.reception_time)) / 1000 if v.reception_time else None
                realtime[config.short(n)] = (value(v.eng_value), round(age, 1) if age is not None else None)
        for link in arch.client.list_links(settings.instance):
            links[link.name] = {"status": link.status, "in": link.in_count, "out": link.out_count}
        if window["minutes"]:
            # Over exactly the minutes the capture counts cover: a minute it missed must not count against it.
            starts = [int(dt.datetime.strptime(m, "%Y%m%dT%H%M").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                      for m in window["minutes"]]
            s = arch.parameters(["FRAMES_SENT"], starts[0] - 5_000, starts[-1] + 65_000)["FRAMES_SENT"]
            parts = [frames_sent_between(s, m0, m0 + 60_000) for m0 in starts]
            frames_sent = sum(parts) if all(p is not None for p in parts) else None
    except Exception as e:
        problems.append("%s: %s" % (type(e).__name__, str(e)[:200]))
    h = ql.health(stamp, status, window, realtime, links, frames_sent, status_age)
    h["problems"] = problems
    h["product"]["version"] = products.version("ql_health")
    sheet = ql.contact_sheet(ql.latest_frames(settings.capture), h)
    refs = []
    for ptype, data in (("ql_health", canonical_json(h)), ("ql_contact_sheet", sheet)):
        _, ver, ext, _ = products.ALGORITHMS[ptype]
        refs.append(_register(settings, catalog, product_type=ptype, episode_id=None,
                              product_id=f"ql/{ptype}@{ver}/{stamp}", path=quicklook_path(settings, ptype, stamp, ext),
                              data=data, inputs=[{"source": "capture", "path": str(settings.capture)},
                                                 {"source": "yamcs:%s/%s" % (settings.yamcs, settings.instance),
                                                  "kind": "realtime", "names": list(QL_CHANNELS)}], run_id=run_id))
        write_atomic(settings.products / "quicklook" / f"latest.{ext}", data)
    cutoff = dt.datetime.fromtimestamp(at_ms / 1000 - QL_RETENTION_H * 3600, tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    old = [p for p in catalog.products() if p["product_type"].startswith("ql_") and p["product_id"].rsplit("/", 1)[-1] < cutoff]
    for p in old:
        try:
            Path(p["path"]).unlink()
        except OSError:
            pass
    catalog.forget([p["product_id"] for p in old])
    return {"health": refs[0], "contact_sheet": refs[1], "go": h["go"], "verdicts": h["verdicts"],
            "last_5_min": {k: window[k] for k in ("frames_complete", "frames_incomplete", "frames_missing",
                                                   "frames_cut", "frame_completeness", "minutes")},
            "pruned": len(old)}
