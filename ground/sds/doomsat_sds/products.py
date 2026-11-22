"""The products, as pure functions: inputs in, a document or an image out. No Yamcs, no Airflow, no clock.

    L1  l1_episode     the episode record: telemetry, link housekeeping, commands, events, in one window
    L2  l2_path        the walked path as a PNG, from telemetry only (no WAD, no background)
        l2_summary     duration, kills, cells explored, health, outcome, distance walked
        l2_linkstats   how complete the status stream, the frames and the uplink were over the episode
    L3  l3_rollup      totals and distributions across the current L2 summaries

Every L2 product is computed from an L1 document and nothing else, so any L2 can be rebuilt from any L1, and
reprocessing (Phase C) is "same L1, newer L2 algorithm". ALGORITHMS holds each product's current version; a
change to what a builder produces is a version bump there, never a silent edit.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter

from . import config, png
from .archive import iso
from .store import canonical_json, sha256

ALGORITHMS = {
    # product type: (level, version, file extension, media type)
    # 1.1.0 / 2.1.0 / 1.1.0 / 1.1.0: a new processing baseline, after L1 stopped losing same-time samples (_table).
    # An L1 change reaches every L2, so their versions move with it; reprocess never rebuilds an L2 in place.
    "l1_episode": ("L1", "1.1.0", "json", "application/json"),
    "l2_path": ("L2", "2.2.0", "png", "image/png"),         # 2.0.0: Phase C, see build_path_png
    # l2_path 2.2.0: the end of a wad_switch episode has a colour of its own (END_COLOURS).
    # 1.2.0: commands counted are the payload's (the Doom component's); the ground system's own, such as a SendFile
    # that asked for an earlier episode's record, are reported apart and kept out of uplink completeness.
    # l2_summary 1.3.0: also the patch WAD and the count of in-flight WAD switches from the context (main's LOAD_WAD).
    "l2_summary": ("L2", "1.3.0", "json", "application/json"),
    "l2_linkstats": ("L2", "1.2.0", "json", "application/json"),
    "l3_rollup": ("L3", "1.1.0", "json", "application/json"),     # 1.1.0: a patch WAD is part of the level's key
    "ql_health": ("QL", "1.0.0", "json", "application/json"),
    "ql_contact_sheet": ("QL", "1.0.0", "png", "image/png"),
    "l0_record": ("L0", "1.0.0", "json", "application/json"),        # Phase D: the payload's record, as downlinked
    # qa_record_check 1.3.0: the patch WAD is compared too, and the record's "reset" agrees with a "wad_switch".
    "qa_record_check": ("QA", "1.3.0", "json", "application/json"),  # Phase D: that record against L1
}
L2_TYPES = ("l2_path", "l2_summary", "l2_linkstats")
# IntentSet echoes every INTENT command, which L1 already carries; the other Doom events are kept.
SKIP_EVENTS = {config.EVENT_PREFIX + "IntentSet"}
GROUND_LINK = "TM_FRAMES_IN"   # Yamcs's count of frames received on UDP_TM_IN, renamed for the table


def version(product_type: str) -> str:
    return ALGORITHMS[product_type][1]


def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def newer(a: str, b: str) -> bool:
    """Is version a newer than version b?"""
    return _version_key(a) > _version_key(b)


# --------------------------------------------------------------------------------------------- level 1

def clock_offset(series: dict) -> dict:
    """TM generation time minus ground reception time, from the samples themselves.

    F´ time tags come through fprime-yamcs, command history is stamped with the ground clock. This is the offset
    that puts commands on the telemetry's time axis: about +0.95 s with an unpatched fprime-yamcs (it adds 38 leap
    seconds where TAI-UTC is 37) and in data recorded before tools/yamcs_time_patch.py, about -0.05 s (the link
    delay) after it (docs/plans/fprime-yamcs-time.md). Measured, so it is right either way.
    """
    d = sorted(g - r for s in series.values() for g, r, _ in s if r is not None)
    if not d:
        return {"tm_minus_ground_ms": 0, "p05_ms": None, "p95_ms": None, "samples": 0}
    q = lambda f: d[min(len(d) - 1, int(f * len(d)))]
    return {"tm_minus_ground_ms": int(statistics.median_low(d)), "p05_ms": q(0.05), "p95_ms": q(0.95), "samples": len(d)}


def _table(series: dict, names: list, start_ms: int, end_ms: int) -> dict:
    """One row per TM time at which any of `names` updated; null where a channel did not.

    Lossless: every archived sample in the window is in exactly one cell, so consumers can forward-fill or
    not as their algorithm needs. F' time tags are coarse (the rate group's), and two statuses handled within
    one tag give a channel two samples with the same time: those go in consecutive rows with the same t_ms,
    in reception order, so rows are non-decreasing (not strictly increasing) in time. (1.0.0 kept one row per
    time and so lost the earlier of such a pair: 8 samples in a 34 s episode.) Within such a pair, the k-th
    sample of one channel and the k-th of another need not come from the same status, because a channel can
    lose one of the two on board; the downlink does not say, so nothing downstream should assume it.
    """
    rows: dict[tuple[int, int], list] = {}
    for i, n in enumerate(names):
        nth: dict[int, int] = {}
        for t, r, v in sorted(series.get(n, []), key=lambda s: (s[0], -1 if s[1] is None else s[1], repr(s[2]))):
            if start_ms <= t <= end_ms:
                k = nth.get(t, 0)
                nth[t] = k + 1
                rows.setdefault((t, k), [None] * len(names))[i] = v
    return {"columns": ["t_ms"] + list(names), "rows": [[t] + rows[(t, k)] for t, k in sorted(rows)]}


def build_l1(ep, window, science: dict, link: dict, commands: list, events: list, context: dict,
             inputs: list) -> dict:
    """The episode record. `ep` is an episodes.ClosedEpisode, `window` its (first_ms, last_ms).

    `science` and `link` map channel names to archive series; `link` may also hold GROUND_LINK. `commands`
    are archive command dicts over a window wide enough to cover the clock offset; only those whose TM-axis
    time falls inside the episode are kept.
    """
    start_ms, end_ms = window
    clock = clock_offset(science)
    off = clock["tm_minus_ground_ms"]
    cmds = []
    for c in commands:
        t_tm = c["t"] + off
        if start_ms <= t_tm <= end_ms:
            cmds.append({"t_ground_ms": c["t"], "t_tm_ms": t_tm, "name": c["name"], "args": c["args"],
                         "id": c["id"], "acks": c.get("acks", {})})
    evs = [{"t_ms": e["t"], "type": e["type"], "message": e["message"], "extra": e["extra"]}
           for e in events if e["type"].startswith(config.EVENT_PREFIX) and e["type"] not in SKIP_EVENTS
           and start_ms <= e["t"] <= end_ms + 1500]
    telemetry = _table(science, list(config.SCIENCE), start_ms, end_ms)
    housekeeping = _table(link, list(config.LINK) + [GROUND_LINK], start_ms, end_ms)
    return {
        "product": {"type": "l1_episode", "level": "L1", "version": version("l1_episode")},
        "episode": {"id": ep.episode_id, "number": ep.number, "outcome": ep.outcome, "closing_event": ep.closing,
                    "closing_utc": iso(ep.end_ms), "inferred_close": ep.inferred,
                    "start_event_utc": iso(ep.start_event_ms) if ep.start_event_ms is not None else None,
                    "start_ms": start_ms, "end_ms": end_ms, "start_utc": iso(start_ms), "end_utc": iso(end_ms),
                    "duration_s": round((end_ms - start_ms) / 1000, 3)},
        "context": context,
        "clock": clock,
        "telemetry": telemetry,
        "housekeeping": housekeeping,
        "commands": sorted(cmds, key=lambda c: (c["t_tm_ms"], c["id"])),
        "events": sorted(evs, key=lambda e: (e["t_ms"], e["type"])),
        "counts": {"telemetry_rows": len(telemetry["rows"]),
                   "samples": {n: sum(1 for t, _, _ in science.get(n, []) if start_ms <= t <= end_ms)
                               for n in config.SCIENCE},
                   "commands": len(cmds), "events": len(evs)},
        "inputs": inputs,
    }


# --------------------------------------------------------------------------------------------- helpers on L1

def column(l1: dict, name: str, table: str = "telemetry") -> list[tuple[int, object]]:
    """(t_ms, value) for every non-null cell of one channel, in time order."""
    cols = l1[table]["columns"]
    i = cols.index(name)
    return [(r[0], r[i]) for r in l1[table]["rows"] if r[i] is not None]


def path_points(l1: dict) -> list[tuple[int, float, float]]:
    """(t_ms, x, y) wherever the position changed: POS_X and POS_Y forward-filled across rows."""
    cols = l1["telemetry"]["columns"]
    ix, iy = cols.index("POS_X"), cols.index("POS_Y")
    x = y = None
    pts: list[tuple[int, float, float]] = []
    for r in l1["telemetry"]["rows"]:
        x = r[ix] if r[ix] is not None else x
        y = r[iy] if r[iy] is not None else y
        if x is not None and y is not None and (not pts or (pts[-1][1], pts[-1][2]) != (x, y)):
            pts.append((r[0], x, y))
    return pts


def _last(seq, default=None):
    return seq[-1][1] if seq else default


def payload_commands(l1: dict) -> list[dict]:
    """The commands in the window addressed to the Doom component, the only ones CMDS_RECEIVED counts."""
    return [c for c in l1["commands"] if c["name"].startswith(config.NAMESPACE)]


def other_commands(l1: dict) -> dict:
    """Commands in the window to anything else: the ground system's own, such as the record request (cfdpManager
    SendFile on main), and the CFDP PDUs Yamcs's CfdpService enters in command history as /doomsat/cfdp/pdu."""
    return dict(sorted(Counter(c["name"] for c in l1["commands"] if not c["name"].startswith(config.NAMESPACE))
                       .items()))


# --------------------------------------------------------------------------------------------- level 2

def build_summary(l1: dict) -> dict:
    ep = l1["episode"]
    tic = column(l1, "TIC")
    health = [v for _, v in column(l1, "HEALTH")]
    pts = path_points(l1)
    dist = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts, pts[1:]))
    cmds = Counter(c["name"].rsplit("/", 1)[-1] for c in payload_commands(l1))
    return {
        "product": {"type": "l2_summary", "level": "L2", "version": version("l2_summary")},
        "episode_id": ep["id"], "number": ep["number"], "outcome": ep["outcome"],
        "start_utc": ep["start_utc"], "end_utc": ep["end_utc"], "duration_s": ep["duration_s"],
        "tics": (tic[-1][1] - tic[0][1]) if len(tic) > 1 else 0,
        "kills": max((v for _, v in column(l1, "KILLS")), default=0),
        "explored_cells": _last(column(l1, "EXPLORED_CELLS"), 0),
        "level": _last(column(l1, "LEVEL")),
        "health": {"start": health[0] if health else None, "min": min(health) if health else None,
                   "end": health[-1] if health else None},
        "distance_units": round(dist, 1),
        "path_points": len(pts),
        "commands": dict(sorted(cmds.items())),
        "other_commands": other_commands(l1),
        "wad": l1["context"].get("wad"), "pwad": l1["context"].get("pwad"), "map": l1["context"].get("map"),
        "wad_loads": l1["context"].get("wad_loads"),
        "skill": l1["context"].get("skill"), "pilot_mode": l1["context"].get("pilot_mode"),
        "inputs": [l1["episode"]["id"] + "/l1_episode@" + l1["product"]["version"]],
    }


def _delta(seq) -> int | None:
    return (seq[-1][1] - seq[0][1]) if len(seq) > 1 else None


def build_linkstats(l1: dict) -> dict:
    """Downlink and uplink completeness over the episode, from the L1 record alone.

    - Status stream: the payload sends a STATUS every few tics (3 by default) and F´ downlinks each channel at
      20 Hz, so consecutive TIC samples should step by that amount; a bigger step is a status that never reached
      the ground (overwritten on board between two TlmChan cycles, or lost on the link).
    - Uplink: commands the ground sent in the window against the CMDS_RECEIVED counter on board.
    """
    ep = l1["episode"]
    dur = max(ep["duration_s"], 1e-3)
    tic = [v for _, v in column(l1, "TIC")]
    steps = [b - a for a, b in zip(tic, tic[1:]) if b > a]
    step = Counter(steps).most_common(1)[0][0] if steps else None
    expected = (tic[-1] - tic[0]) // step + 1 if step and len(tic) > 1 else len(tic)
    received = len(set(tic))
    frames = column(l1, "FRAMES_SENT", "housekeeping")
    chunks = column(l1, "CHUNKS_SENT", "housekeeping")
    cmds_rx = column(l1, "CMDS_RECEIVED", "housekeeping")
    link = [v for _, v in column(l1, "PAYLOAD_LINK", "housekeeping")]
    tm_in = column(l1, GROUND_LINK, "housekeeping")
    sent = len(payload_commands(l1))
    rx = _delta(cmds_rx)
    return {
        "product": {"type": "l2_linkstats", "level": "L2", "version": version("l2_linkstats")},
        "episode_id": ep["id"], "duration_s": ep["duration_s"],
        "status_stream": {"step_tics": step, "expected": expected, "received": received,
                          "missing": max(0, expected - received),
                          "completeness": round(received / expected, 4) if expected else None,
                          "max_gap_tics": max(steps) if steps else None},
        "downlink": {"frames_sent": _delta(frames), "chunks_sent": _delta(chunks),
                     "frames_per_s": round(_delta(frames) / dur, 2) if _delta(frames) is not None else None,
                     "tm_frames_received": _delta(tm_in),
                     "tm_frames_per_s": round(_delta(tm_in) / dur, 2) if _delta(tm_in) is not None else None},
        "uplink": {"commands_sent": sent, "commands_received_on_board": rx,
                   "completeness": round(rx / sent, 4) if sent and rx is not None else None,
                   "other_commands": other_commands(l1)},
        "payload_link_up_fraction": round(sum(1 for v in link if v is True) / len(link), 4) if link else None,
        "sample_rate_hz": {n: round(c / dur, 2) for n, c in sorted(l1["counts"]["samples"].items())},
        "clock": l1["clock"],
        "inputs": [l1["episode"]["id"] + "/l1_episode@" + l1["product"]["version"]],
    }


# l2_path 2.1.0 had no "wad_switch" here and drew that end in the fallback white; 2.2.0 added it.
END_COLOURS = {"died": "#e04040", "level_finished": "#40c0e0", "reset": "#a0a0a0", "wad_switch": "#b080e0",
               "interrupted": "#a0a0a0"}


def _scale_bar_units(span_units: float) -> int:
    """A round length near a fifth of the drawn span: 64, 128, 256, 512, 1024..."""
    target = max(span_units / 5, 1)
    return 2 ** max(4, int(round(math.log2(target))))


# Doom's running speed is about 500 units/s and status arrives at about 11 Hz, so consecutive samples are at most
# ~50 units apart (twice that across a dropped status). A longer step is a teleporter, not a walk.
JUMP_UNITS = 192
TIME_STOPS = ((0.0, (48, 96, 240)), (0.5, (240, 208, 48)), (1.0, (240, 64, 48)))   # early blue, mid yellow, late red


def _time_colour(f: float) -> tuple[int, int, int]:
    for (f0, c0), (f1, c1) in zip(TIME_STOPS, TIME_STOPS[1:]):
        if f <= f1:
            k = (f - f0) / (f1 - f0) if f1 > f0 else 0.0
            return tuple(int(round(a + (b - a) * k)) for a, b in zip(c0, c1))
    return TIME_STOPS[-1][1]


def kill_points(l1: dict) -> list[tuple[int, float, float]]:
    """(t_ms, x, y) at each rise of KILLS, using the position held at that moment."""
    cols = l1["telemetry"]["columns"]
    ix, iy, ik = cols.index("POS_X"), cols.index("POS_Y"), cols.index("KILLS")
    x = y = kills = None
    out = []
    for r in l1["telemetry"]["rows"]:
        x = r[ix] if r[ix] is not None else x
        y = r[iy] if r[iy] is not None else y
        if r[ik] is not None:
            if kills is not None and r[ik] > kills and x is not None and y is not None:
                out.append((r[0], x, y))
            kills = r[ik]
    return out


def build_path_png(l1: dict, width: int = 600, height: int = 640) -> bytes:
    """The walked path, drawn from POS_X/POS_Y alone.

    v2.0.0 (Phase C): the line breaks at jumps longer than JUMP_UNITS (teleporters; v1 drew a straight line across
    the map for each), the path is coloured by elapsed time from blue through yellow to red so that revisits are
    visible, and each kill is marked with a white cross. Start is the green disc, the end is coloured by outcome.
    v1.0.0 joined every sample in one colour; its images stay in the catalog beside the v2 ones. v2.2.0 gives the end
    of an episode a WAD switch cut short a colour of its own (2.1.0 drew it white).

    There is no level geometry behind it: nothing here opens a WAD (charter 2.4), and this image never goes back
    to the pilot (charter 2.2; tests/test_sds_guard.py).
    """
    ep = l1["episode"]
    pts = path_points(l1)
    c = png.Canvas(width, height, "#101418")
    top, margin = 44, 24
    c.rect(0, 0, width, top - 6, "#1c232b")
    c.text(10, 10, ep["id"], "#ffffff", 2)
    c.text(10, 28, "%s  %.0fS  L1 %s  PATH V%s" % (ep["outcome"].replace("_", " "), ep["duration_s"],
                                                    l1["product"]["version"], version("l2_path")), "#a8b4c0", 1)
    if not pts:
        c.text(margin, top + 20, "NO POSITION SAMPLES", "#e04040", 2)
        return c.to_png()
    xs, ys = [p[1] for p in pts], [p[2] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span = max(x1 - x0, y1 - y0, 64.0)
    scale = min((width - 2 * margin) / span, (height - top - 2 * margin) / span)
    ox = margin + ((width - 2 * margin) - (x1 - x0) * scale) / 2
    oy = top + margin + ((height - top - 2 * margin) - (y1 - y0) * scale) / 2
    to_px = lambda x, y: (ox + (x - x0) * scale, oy + (y1 - y) * scale)   # Doom's y points up, the image's down
    t0, t1 = pts[0][0], pts[-1][0]
    jumps = 0
    for a, b in zip(pts, pts[1:]):
        if math.hypot(b[1] - a[1], b[2] - a[2]) > JUMP_UNITS:
            jumps += 1
            continue
        c.line(*to_px(a[1], a[2]), *to_px(b[1], b[2]), _time_colour((a[0] - t0) / max(t1 - t0, 1)), 2)
    for _, kx, ky in kill_points(l1):
        px, py = to_px(kx, ky)
        c.line(px - 4, py - 4, px + 4, py + 4, "#ffffff", 2)
        c.line(px - 4, py + 4, px + 4, py - 4, "#ffffff", 2)
    c.disc(*to_px(pts[0][1], pts[0][2]), 6, "#40d070")
    c.disc(*to_px(pts[-1][1], pts[-1][2]), 6, END_COLOURS.get(ep["outcome"], "#ffffff"))
    bar = _scale_bar_units(span)
    bx, by = margin, height - 14
    c.line(bx, by, bx + bar * scale, by, "#d0d8e0", 2)
    c.text(int(bx + bar * scale) + 6, by - 3, "%d UNITS" % bar, "#d0d8e0", 1)
    lx = width - margin - 150                                     # time legend, bottom right
    for i in range(120):
        c.line(lx + i, by - 3, lx + i, by + 3, _time_colour(i / 119), 1)
    c.text(lx - 36, by - 3, "START", "#a8b4c0", 1)
    c.text(lx + 124, by - 3, "END", "#a8b4c0", 1)
    if jumps:
        c.text(width - margin - 150, top + 4, "%d JUMP%s NOT DRAWN" % (jumps, "" if jumps == 1 else "S"), "#a8b4c0", 1)
    return c.to_png()


# --------------------------------------------------------------------------------------------- level 3

def level_key(d: dict) -> str:
    """'freedoom1.wad E1M1', or with a patch WAD as an operator says it, 'basic.wad over freedoom2.wad MAP01':
    the same map name over another patch is another level."""
    wad = d.get("wad") or "?"
    if d.get("pwad"):
        wad = "%s over %s" % (d["pwad"], wad)
    return "%s %s" % (wad, d.get("map") or "?")


def build_rollup(summaries: list[dict], summary_ids: list[str]) -> dict:
    """Across episodes, from their current L2 summaries (each carries its own WAD/map context)."""
    s = sorted(summaries, key=lambda d: d["episode_id"])
    dur = [d["duration_s"] for d in s]
    explored = [d["explored_cells"] or 0 for d in s]
    by_level: dict[str, dict] = {}
    for d in s:
        key = level_key(d)
        g = by_level.setdefault(key, {"episodes": 0, "outcomes": {}, "kills": 0, "best_explored_cells": 0,
                                      "duration_s": 0.0})
        g["episodes"] += 1
        g["outcomes"][d["outcome"]] = g["outcomes"].get(d["outcome"], 0) + 1
        g["kills"] += d["kills"] or 0
        g["best_explored_cells"] = max(g["best_explored_cells"], d["explored_cells"] or 0)
        g["duration_s"] = round(g["duration_s"] + d["duration_s"], 3)
    return {
        "product": {"type": "l3_rollup", "level": "L3", "version": version("l3_rollup")},
        "episodes": len(s),
        "outcomes": dict(sorted(Counter(d["outcome"] for d in s).items())),
        "kills": sum(d["kills"] or 0 for d in s),
        "duration_s": {"total": round(sum(dur), 3), "mean": round(statistics.fmean(dur), 3) if dur else None,
                       "median": statistics.median(dur) if dur else None, "max": max(dur, default=None)},
        "explored_cells": {"mean": round(statistics.fmean(explored), 1) if explored else None,
                           "max": max(explored, default=None)},
        "by_level": dict(sorted(by_level.items())),
        "episode_list": [{"episode_id": d["episode_id"], "outcome": d["outcome"], "duration_s": d["duration_s"],
                          "kills": d["kills"], "explored_cells": d["explored_cells"], "wad": d.get("wad"),
                          "pwad": d.get("pwad"), "map": d.get("map")} for d in s],
        "inputs": sorted(summary_ids),
    }


def rollup_inputs_hash(summary_ids: list[str], checksums: list[str]) -> str:
    return sha256(canonical_json(sorted(zip(summary_ids, checksums))))
