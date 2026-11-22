"""Phase B: the quicklook, a contact sheet of the latest captured frames with link and payload health on top.

    ql_health          (JSON) what a person on console wants at a glance: is the payload talking, is the link
                       up, are frames arriving whole, and how many over the last five minutes
    ql_contact_sheet   (PNG)  the newest frames from the capture service as thumbnails, under a health banner

`health()` is pure (it takes the capture service's status, its per-minute stats and a few realtime values);
`contact_sheet()` needs Pillow, imported only there, because the JPEGs have to be decoded.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

WINDOW_MIN = 5
COUNT_KEYS = ("frames_complete", "frames_incomplete", "frames_missing", "frames_cut", "frame_duplicate_chunks",
              "frame_late_chunks", "maps_complete", "maps_incomplete", "maps_missing", "chunks", "bytes")


def last_minutes(stats_path: Path, now_minute: str, minutes: int = WINDOW_MIN) -> dict:
    """Sum the capture service's per-minute counts over the last `minutes` closed minutes (yyyymmddThhmm keys).

    A minute the capture service was only up for part of (it started or stopped in it) is left out entirely,
    whichever of its lines is complete, and so is a minute it was not up for at all: `minutes` in the result lists
    exactly the minutes counted, so anything compared with these counts must cover those minutes and no others.
    """
    import datetime as dt
    end = dt.datetime.strptime(now_minute, "%Y%m%dT%H%M").replace(tzinfo=dt.timezone.utc)
    wanted = {(end - dt.timedelta(minutes=i)).strftime("%Y%m%dT%H%M") for i in range(1, minutes + 1)}
    total = {k: 0 for k in COUNT_KEYS}
    rows, broken = [], set()
    if stats_path.exists():
        for line in stats_path.read_text(encoding="utf-8").splitlines()[-(minutes * 20):]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("minute") in wanted:
                if row.get("partial_minute") or row.get("started_mid_minute"):
                    broken.add(row["minute"])
                else:
                    rows.append(row)
    seen = []
    for row in rows:
        if row["minute"] not in broken:
            seen.append(row["minute"])
            for k in COUNT_KEYS:
                total[k] += row.get(k, 0)
    whole = total["frames_complete"] + total["frames_incomplete"] + total["frames_missing"]
    total["frame_completeness"] = round(total["frames_complete"] / whole, 4) if whole else None
    total["minutes"] = sorted(set(seen))
    return total


STATUS_STALE_S = 15          # the capture service rewrites status.json every 5 s; older than this, it has stopped


def health(now_utc: str, capture_status: dict | None, window: dict, realtime: dict, links: dict,
           frames_sent_window: int | None, status_age_s: float | None = None) -> dict:
    """The verdicts and the numbers behind them. `realtime` maps channel -> (value, age_s); `links` maps link
    name -> {"status", "in", "out"}; `frames_sent_window` is FRAMES_SENT's rise over the minutes in `window`;
    `status_age_s` is how old the capture service's status.json is (a dead service leaves its last one behind)."""
    def rt(name):
        v = realtime.get(name)
        return (v[0], v[1]) if v else (None, None)

    link_v, link_age = rt("PAYLOAD_LINK")
    tm = links.get("UDP_TM_IN", {})
    tc = links.get("UDP_TC_OUT", {})
    since_chunk = (capture_status or {}).get("seconds_since_last_chunk")
    stale = status_age_s is not None and status_age_s > STATUS_STALE_S
    received = window["frames_complete"] + window["maps_complete"]
    verdicts = {
        "yamcs": "GO" if realtime else "NO-GO",
        "payload": "GO" if link_v is True and link_age is not None and link_age < 10 else "NO-GO",
        "downlink": "GO" if tm.get("status") == "OK" else "NO-GO",
        "uplink": "GO" if tc.get("status") == "OK" else "NO-GO",
        "capture": "GO" if since_chunk is not None and since_chunk < 10 and not stale else "NO-GO",
    }
    return {
        "product": {"type": "ql_health", "level": "QL"},
        "utc": now_utc,
        "verdicts": verdicts,
        "go": all(v == "GO" for v in verdicts.values()),
        "payload": {k: rt(k)[0] for k in ("EPISODE", "HEALTH", "KILLS", "EXPLORED_CELLS", "TIC", "PAYLOAD_LINK")},
        "telemetry_age_s": link_age,
        "links": links,
        "capture": {"seconds_since_last_chunk": since_chunk, "subscriptions": (capture_status or {}).get("subscriptions"),
                    "in_flight": (capture_status or {}).get("in_flight"),
                    "status_age_s": None if status_age_s is None else round(status_age_s, 1)},
        "last_%d_min" % WINDOW_MIN: window,
        "frames_sent_on_board_last_%d_min" % WINDOW_MIN: frames_sent_window,
        "frame_yield_last_%d_min" % WINDOW_MIN: (round(received / frames_sent_window, 4)
                                                 if frames_sent_window else None),
    }


def latest_frames(capture_root: Path, n: int = 12) -> list[Path]:
    """The newest n JPEGs the capture service wrote (names sort by time)."""
    hours = sorted((capture_root / "frames").glob("2*/[0-2][0-9]"))[-2:]
    files = sorted(f for h in hours for f in h.glob("*.jpg"))
    return files[-n:]


def contact_sheet(frames: list[Path], h: dict, cols: int = 4, thumb: tuple[int, int] = (160, 120)) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.load_default(size=11)        # Pillow's bundled FreeType face
    except (TypeError, OSError):
        font = ImageFont.load_default()
    rows = max(1, (len(frames) + cols - 1) // cols)
    banner = 92
    sheet = Image.new("RGB", (cols * thumb[0], banner + rows * thumb[1]), (16, 20, 24))
    d = ImageDraw.Draw(sheet)
    go = (64, 208, 112)
    nogo = (224, 64, 64)
    x = 8
    for name, v in h["verdicts"].items():
        d.rectangle([x, 6, x + 112, 22], fill=go if v == "GO" else nogo)
        d.text((x + 4, 8), "%s  %s" % (name.upper(), v), fill=(0, 0, 0), font=font)
        x += 120
    w = h["last_%d_min" % WINDOW_MIN]
    p = h["payload"]
    lines = [
        "DoomSat quicklook  %s   episode %s  health %s  kills %s  cells %s" % (
            h["utc"], p["EPISODE"], p["HEALTH"], p["KILLS"], p["EXPLORED_CELLS"]),
        "last %d min: frames complete %s  incomplete %s  missing %s  cut %s  completeness %s" % (
            WINDOW_MIN, w["frames_complete"], w["frames_incomplete"], w["frames_missing"], w["frames_cut"],
            w["frame_completeness"]),
        "on board sent %s (frames + maps), received whole %s, yield %s   maps %s/%s" % (
            h["frames_sent_on_board_last_%d_min" % WINDOW_MIN], w["frames_complete"] + w["maps_complete"],
            h["frame_yield_last_%d_min" % WINDOW_MIN], w["maps_complete"], w["maps_complete"] + w["maps_incomplete"]),
        "TM in %s  TC out %s  telemetry age %s s  last chunk %s s ago" % (
            h["links"].get("UDP_TM_IN", {}).get("in"), h["links"].get("UDP_TC_OUT", {}).get("out"),
            h["telemetry_age_s"], h["capture"]["seconds_since_last_chunk"]),
    ]
    for i, line in enumerate(lines):
        d.text((8, 30 + 14 * i), line, fill=(208, 216, 224), font=font)
    for i, f in enumerate(frames):
        try:
            im = Image.open(f).convert("RGB").resize(thumb)
        except Exception:
            continue
        cx, cy = (i % cols) * thumb[0], banner + (i // cols) * thumb[1]
        sheet.paste(im, (cx, cy))
        n = f.name                                   # 20261004T010137.283Z-00008275.jpg
        d.text((cx + 3, cy + 2), "%s:%s:%s  #%d" % (n[9:11], n[11:13], n[13:19], int(n[21:29])), fill=(255, 255, 0),
               font=font)
    out = io.BytesIO()
    sheet.save(out, format="PNG", optimize=False)
    return out.getvalue()
