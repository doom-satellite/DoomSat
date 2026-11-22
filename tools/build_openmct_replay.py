# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Turn one recorded DoomSat flight into a replay pack for Open MCT.

    python tools/build_openmct_replay.py                                   # research/out/flight-32
    python tools/build_openmct_replay.py --flight research/out/flight-27 --frame-stride 1
    python tools/build_openmct_replay.py --log path/to/decisions.jsonl     # frames, map, overlay: path/to

Writes ground/openmct/replay/<flight>/ (git-ignored: it is rebuilt from research/out, which is already in
the repo).

What goes in, and how honest each piece is (the pack carries the same labels in meta.provenance):

  recorded   the raw telemetry the pilot logged with every decision (RAW_KEYS in ground/pilot.py), the tic,
             kills, goal, the INTENT it sent, jev's answers and the selection detail, the downlinked JPEGs
  derived    computed here from recorded values only: decision source, rolling jev_share over intent changes,
             an approximate decision age (telemetry age + jev latency + command issue time), candidate slots,
             frame times (frame sequence spread linearly over the decision window), and events inferred from
             transitions (marked [derived], source REPLAY)
  absent     everything the log never carried (F' rate groups, CPU, queues, buffers, event text). Those
             displays show no data in replay. Nothing is synthesised to make a screen look busy.

Knowledge boundary: the grader overlay (overlay-*.png) is WAD-derived and is copied only into
grader/, which only the post-flight wall shows.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ground"))
import openmct_dict as doomdict  # noqa: E402
from ops_telemetry import Rolling  # noqa: E402  the live feed's own rules, so a replay reads what the flight showed

DOOM = doomdict.DOOM
GROUND = "/DoomGround"
CAND_KIND = {"frontier": "FRONTIER", "door": "DOOR", "exit": "EXIT", "key": "KEY", "item": "ITEM",
             "switch": "SWITCH", "enemy": "ENEMY"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flight", help="a flight directory holding decisions.jsonl and frames/ (default: the "
                    "--log file's directory, else research/out/flight-32)")
    ap.add_argument("--log", help="decisions.jsonl (default: <flight>/decisions.jsonl)")
    ap.add_argument("--frames", help="frames directory (default: <flight>/frames)")
    ap.add_argument("--out", help="default: ground/openmct/replay/<flight name>")
    ap.add_argument("--frame-stride", type=int, default=2)
    ap.add_argument("--name", default=None)
    ap.add_argument("--no-displays", action="store_true", help="build the pack only (tests use this)")
    a = ap.parse_args()
    # the frames, the map and the grader overlay come from the log's own flight, never another one's
    flight = (Path(a.log).resolve().parent if a.log and not a.flight
              else doomdict.ROOT / (a.flight or "research/out/flight-32"))
    a.name = a.name or flight.name
    a.log = a.log or str(flight / "decisions.jsonl")
    a.frames = a.frames or (str(flight / "frames") if (flight / "frames").is_dir() else None)
    a.out = a.out or str(doomdict.ROOT / "ground" / "openmct" / "replay" / a.name)

    rows = [json.loads(l) for l in open(a.log, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r.get("kind") == "control"]
    if not rows:
        sys.exit("no control rows in the log")
    params, drift = doomdict.load()
    out = Path(a.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)

    series = {}

    def put(q, t, v):
        if q not in params:
            raise KeyError(f"{q} is not in the dictionary")
        s = series.setdefault(q, [])
        if not s or s[-1][1] != v:
            s.append([t, v])

    events, commands = [], []
    prev = {}
    rolling = Rolling(40)
    for i, r in enumerate(rows):
        t = int(r["t"] * 1000)
        raw = r.get("raw") or {}
        for k, v in raw.items():
            q = f"{DOOM}/{k}"
            if q in params and v is not None:
                put(q, t, v)
        put(f"{DOOM}/TIC", t, r.get("tic"))
        put(f"{DOOM}/KILLS", t, r.get("kills"))
        put(f"{DOOM}/EPISODE", t, r.get("episode"))
        put(f"{DOOM}/GOAL", t, r.get("goal"))
        put(f"{DOOM}/PAYLOAD_LINK", t, True)
        put(f"{DOOM}/DEAD", t, (raw.get("HEALTH") or 1) <= 0)
        put(f"{DOOM}/CMDS_RECEIVED", t, i + 1)
        c = r.get("control") or {}
        if "intent_id" in c:
            put(f"{DOOM}/INTENT_ID", t, c["intent_id"])
        cands = r.get("cand_xy") or []
        put(f"{DOOM}/CAND_COUNT", t, len(cands))
        for n in range(8):
            base = f"{DOOM}/CAND{n}"
            if n < len(cands):
                put(f"{base}.kind", t, CAND_KIND.get(cands[n]["kind"], "FRONTIER"))
                put(f"{base}.x", t, cands[n]["x"])
                put(f"{base}.y", t, cands[n]["y"])

        # ---- ground: the autonomy block
        sel = r.get("select") or {}
        src = rolling.add(r)
        put(f"{GROUND}/SystemOneLatencyMs", t, float(r.get("latency_ms") or 0))
        age = (r.get("tel_age_ms") or 0) + (r.get("latency_ms") or 0) + (r.get("cmd_ms") or 0)
        put(f"{GROUND}/DecisionAgeMs", t, float(age))
        put(f"{GROUND}/ControlCommands", t, i + 1)
        put(f"{GROUND}/IntentMode", t, r.get("mode"))
        put(f"{GROUND}/DecisionSource", t, src)
        put(f"{GROUND}/PickGap", t, float(sel.get("gap") or 0))
        put(f"{GROUND}/PickConfidence", t, float(sel.get("confidence") or 0))
        put(f"{GROUND}/GraphVersion", t, r.get("graph_version") or 0)
        put(f"{GROUND}/Attempt", t, r.get("episode") or 0)
        pk = r.get("pick")
        put(f"{GROUND}/PickSlot", t, pk if pk is not None else 255)
        put(f"{GROUND}/PickKind", t, CAND_KIND.get(cands[pk]["kind"], "NONE") if pk is not None and pk < len(cands) else "NONE")
        ans = r.get("answers") or {}
        for n in range(8):
            v = ans.get(f"g_t{n}")
            put(f"{GROUND}/Score{n}", t, float(v) if v not in (None, "") else 0.0)
        if "engage" in ans:
            put(f"{GROUND}/EngageAnswer", t, ans["engage"])
        target = f"{(cands[pk]['kind'] if pk is not None and pk < len(cands) else '-')}"
        put(f"{GROUND}/Controls", t, f"{r.get('mode')} -> {target} ({len(cands)} candidates) [{src.lower()}]")

        if rolling.jev_share() is not None:          # none before the first intent change
            put(f"{GROUND}/JevShare", t, round(rolling.jev_share(), 3))
        put(f"{GROUND}/FallbackRate", t, round(rolling.fallback_rate(), 3))

        # the INTENT that went up is a recorded command
        if c:
            commands.append([t, "INTENT", {k: c[k] for k in ("intent_id", "mode", "target_x", "target_y", "stance",
                                                           "fire_policy", "weapon", "use_at_target", "ttl_ms")
                                           if k in c}])

        # ---- events inferred from transitions, marked as such
        def ev(sev, msg):
            events.append([t, sev, "REPLAY", "[derived] " + msg])
        h, kills = raw.get("HEALTH"), r.get("kills")
        if prev:
            if r.get("mode") != prev["mode"]:
                ev("INFO", f"mode {prev['mode']} -> {r.get('mode')}")
            if (kills or 0) > (prev["kills"] or 0):
                ev("INFO", f"kill: KILLS {prev['kills']} -> {kills}")
            for thr, sev in ((25, "DISTRESS"), (50, "WARNING")):
                if h is not None and prev["h"] is not None and h <= thr < prev["h"]:
                    ev(sev, f"HEALTH fell through {thr} (now {h})")
                    break
            if raw.get("STUCK") and not prev["stuck"]:
                ev("WARNING", "executor reports STUCK")
            if (raw.get("DOOR_OPENS") or 0) > (prev["opens"] or 0):
                ev("INFO", f"door opened ({raw.get('DOOR_OPENS')} of {raw.get('DOOR_PRESSES')} presses)")
            if (raw.get("EXIT_DIST") or 0) and not prev["exit"]:
                ev("INFO", f"exit line recognised at {raw.get('EXIT_DIST')} u")
            if (raw.get("KEYS") or 0) != (prev["keys"] or 0):
                ev("INFO", f"keys held bitmask {prev['keys']} -> {raw.get('KEYS')}")
        else:
            ev("INFO", f"episode {r.get('episode')} in progress, graph v{r.get('graph_version')}, {r.get('pinned_model')}")
        prev = {"mode": r.get("mode"), "kills": kills, "h": h, "stuck": raw.get("STUCK"),
                "opens": raw.get("DOOR_OPENS"), "exit": raw.get("EXIT_DIST"), "keys": raw.get("KEYS")}

    t0, t1 = int(rows[0]["t"] * 1000), int(rows[-1]["t"] * 1000)

    # ---- frames: sequence numbers spread linearly over the decision window (the log has no frame times)
    frame_list = []
    if a.frames:
        fr = sorted(Path(a.frames).glob("frame-*.jpg"))
        if fr:
            seqs = [int(p.stem.split("-")[1]) for p in fr]
            s0, s1 = seqs[0], seqs[-1]
            for j, (p, s) in enumerate(zip(fr, seqs)):
                if j % a.frame_stride:
                    continue
                shutil.copy(p, out / "frames" / p.name)
                ft = t0 + int((s - s0) / max(1, s1 - s0) * (t1 - t0))
                frame_list.append([ft, f"frames/{p.name}"])
                put(f"{DOOM}/FRAMES_SENT", ft, s)
                put(f"{GROUND}/FrameSeq", ft, s)
        mp = Path(a.frames) / "latest_map.png"
        if mp.exists():
            shutil.copy(mp, out / "map.png")
    # the grader writes its overlay beside decisions.jsonl (research/out/<flight>/), not into frames/
    for d in dict.fromkeys(p.resolve() for p in (flight, Path(a.log).parent, a.frames and Path(a.frames)) if p):
        for ov in sorted(Path(d).glob("overlay-*.png")):
            (out / "grader").mkdir(exist_ok=True)
            shutil.copy(ov, out / "grader" / ov.name)

    images = {f"{GROUND}/DoomFrame": frame_list}
    if (out / "map.png").exists():
        images[f"{GROUND}/DoomMap"] = [[t1, "map.png"]]

    recorded = sorted({f"{DOOM}/{k}" for r in rows for k in (r.get("raw") or {})} |
                      {f"{DOOM}/{k}" for k in ("TIC", "KILLS", "EPISODE", "GOAL")} |
                      {f"{GROUND}/{k}" for k in ("SystemOneLatencyMs", "IntentMode", "PickGap", "PickConfidence",
                                                 "GraphVersion", "EngageAnswer")} |
                      {f"{GROUND}/Score{n}" for n in range(8)})
    derived = sorted(set(series) - set(recorded))
    pack = {
        "meta": {
            "name": a.name or Path(a.log).parent.name,
            "source_log": str(Path(a.log).name),
            "t0": t0, "t1": t1, "rows": len(rows), "frames": len(frame_list),
            "graph_version": rows[0].get("graph_version"), "model": rows[0].get("pinned_model"),
            "provenance": {"recorded": recorded, "derived": derived,
                           "absent": "every dictionary parameter not listed above"},
            "xtce_snapshot_lag": drift,
        },
        "dictionary": {q: {k: v for k, v in e.items() if k in ("eng", "enum", "unit", "aliases", "member", "alarms")}
                       for q, e in params.items()},
        "series": series,
        "images": images,
        "events": events,
        "commands": commands,
    }
    (out / "pack.json").write_text(json.dumps(pack, separators=(",", ":")), encoding="utf-8")
    kb = (out / "pack.json").stat().st_size // 1024
    print(f"{pack['meta']['name']}: {len(rows)} decisions over {(t1 - t0) / 1000:.1f} s, {len(series)} series, "
          f"{len(frame_list)} frames, {len(events)} derived events, {len(commands)} recorded commands, {kb} KB")
    print(f"  recorded {len(recorded)}, derived {len(derived)}; in Doom.fpp but not the committed XTCE snapshot: "
          f"{', '.join(drift) or 'none'}")
    if a.no_displays:
        return
    # and the displays for it, anchored at this recording (git-ignored like the pack)
    import build_openmct_displays as bod
    b, root, _, _ = bod.build_all("replay", str(out / "pack.json"))
    target = bod.WEB / "displays" / "doomsat-displays.replay.json"
    target.write_text(bod.display_json(b, root), encoding="utf-8", newline="\n")
    print(f"  {target.relative_to(doomdict.ROOT)} built for it; serve with: python tools/openmct_serve.py")


if __name__ == "__main__":
    main()
