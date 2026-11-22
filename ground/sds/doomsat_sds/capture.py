# /// script
# requires-python = ">=3.10"
# dependencies = ["yamcs-client==2.1.0"]
# ///
"""Phase B: the near-real-time frame capture service.

Frames are never archived (the flight launcher marks FRAME_CHUNK realtime-only, so the packets skip the tm table
and the parameter archive alike), which makes this subscription the only place they can be kept. It:

- subscribes to FRAME_CHUNK on the realtime processor and reassembles images (doomsat_sds.frames);
- writes each JPEG to capture/frames/<yyyymmdd>/<hh>/<time>-<seq>.jpg and each map PNG under capture/maps/,
  named by the TM time of the image's first chunk, and prunes hours older than the retention;
- appends one line per minute to capture/stats.jsonl (complete, incomplete, missing, duplicate and late counts)
  and rewrites capture/status.json every few seconds for the quicklook DAG;
- resubscribes when the stream goes quiet or the connection drops, because yamcs-client never reconnects.

It writes nothing to Yamcs, so it cannot collide with the pilot's /DoomGround parameters, and nothing on the
pilot side reads what it writes (tests/test_sds_guard.py).

    scripts/sds.sh start runs it.  By hand:  uv run ground/sds/doomsat_sds/capture.py   (or python -m doomsat_sds.capture)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):                       # run as a script (uv run): make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doomsat_sds import config
from doomsat_sds.frames import FrameAssembler
from doomsat_sds.store import write_atomic

QUIET_S = 20             # no chunk for this long: drop the subscription and make a new one
STATUS_EVERY_S = 5


def utc_stamp(t_ms: int) -> str:
    t = dt.datetime.fromtimestamp(t_ms / 1000, tz=dt.timezone.utc)
    return t.strftime("%Y%m%dT%H%M%S.") + "%03dZ" % (t_ms % 1000)


class Capture:
    def __init__(self, settings: config.Settings, retention_h: float = 2.0):
        self.root = settings.capture
        self.retention_h = retention_h
        self.lock = threading.Lock()
        self.assembler = FrameAssembler(self._save)
        self.minute: str | None = None
        self.first_minute = True               # the minute the service started in is only part of a minute
        self.last_chunk_mono: float | None = None   # when a chunk last arrived (None: never, since start)
        self.subscribed_mono = time.monotonic()      # when the current subscription began
        self.last_image: dict = {}
        self.totals: dict = {}
        self.started = utc_stamp(int(time.time() * 1000))
        self.subscriptions = 0
        self.last_status = 0.0
        (self.root / "frames").mkdir(parents=True, exist_ok=True)

    # called on the websocket thread
    def on_data(self, pdata) -> None:
        try:
            with self.lock:
                for p in pdata.parameters:
                    if p.name != config.FRAME_CHUNK or not isinstance(p.eng_value, dict):
                        continue
                    t_ms = int(p.generation_time.timestamp() * 1000)
                    self.last_chunk_mono = time.monotonic()
                    self.assembler.add(p.eng_value, t_ms, self.last_chunk_mono)
        except Exception as e:      # an exception here would close the subscription
            print("capture: bad chunk: %r" % e, flush=True)

    def _save(self, kind: str, seq: int, data: bytes, t_ms: int) -> None:
        stamp = utc_stamp(t_ms)
        sub = "frames" if kind == "frame" else "maps"
        path = self.root / sub / stamp[:8] / stamp[9:11] / ("%s-%08d.%s" % (stamp, seq, "jpg" if kind == "frame" else "png"))
        write_atomic(path, data)
        self.last_image[kind] = {"path": str(path), "t_ms": t_ms, "seq": seq, "bytes": len(data)}

    def tick(self) -> None:
        """Once a second: expire stale partials, close the minute when it changes, refresh status.json."""
        now_ms = int(time.time() * 1000)
        minute = utc_stamp(now_ms)[:13]                       # yyyymmddThhmm
        with self.lock:
            self.assembler.expire(time.monotonic())
            if self.minute is None:
                self.minute = minute
            if minute != self.minute:
                self._close_minute()
                self.minute = minute
        if time.monotonic() - self.last_status >= STATUS_EVERY_S:
            self.last_status = time.monotonic()
            self._write_status(now_ms)

    def _close_minute(self, partial: bool = False) -> None:
        counts = self.assembler.take_counts()
        for k, v in counts.items():
            self.totals[k] = self.totals.get(k, 0) + v
        line = dict(sorted(counts.items()), minute=self.minute, subscriptions=self.subscriptions)
        if partial:
            line["partial_minute"] = True          # stopped part-way through it
        if self.first_minute:
            line["started_mid_minute"] = True      # started part-way through it
            self.first_minute = False
        with open(self.root / "stats.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")

    def _write_status(self, now_ms: int) -> None:
        with self.lock:
            since = None if self.last_chunk_mono is None else round(time.monotonic() - self.last_chunk_mono, 1)
            status = {"updated_utc": utc_stamp(now_ms), "updated_ms": now_ms, "started_utc": self.started,
                      "subscriptions": self.subscriptions, "seconds_since_last_chunk": since,
                      "current_minute": dict(self.assembler.counts), "totals": dict(self.totals),
                      "in_flight": len(self.assembler.partial), "last_image": self.last_image}
        write_atomic(self.root / "status.json", (json.dumps(status, sort_keys=True, indent=1) + "\n").encode())

    def prune(self) -> None:
        cutoff = time.time() - self.retention_h * 3600
        for sub in ("frames", "maps"):
            for day in sorted((self.root / sub).glob("2*")):
                for hour in sorted(day.iterdir()):
                    try:
                        t = dt.datetime.strptime(day.name + hour.name, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
                    except ValueError:
                        continue
                    if t.timestamp() + 3600 < cutoff:
                        shutil.rmtree(hour, ignore_errors=True)
                if day.exists() and not any(day.iterdir()):
                    day.rmdir()

    def shutdown(self) -> None:
        with self.lock:
            self.assembler.expire(time.monotonic(), everything=True)
            if self.minute:
                self._close_minute(partial=True)
        self._write_status(int(time.time() * 1000))


def main() -> int:
    from yamcs.client import YamcsClient
    settings = config.Settings.from_env()
    cap = Capture(settings, float(os.environ.get("DOOMSAT_SDS_CAPTURE_RETENTION_H", "2")))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print("capture: writing to %s, Yamcs %s" % (cap.root, settings.yamcs), flush=True)
    last_prune = 0.0
    while not stop.is_set():
        client = sub = None
        try:
            client = YamcsClient(settings.yamcs)
            processor = client.get_processor(settings.instance, config.PROCESSOR)
            sub = processor.create_parameter_subscription([config.FRAME_CHUNK], on_data=cap.on_data,
                                                          send_from_cache=False)
            cap.subscriptions += 1
            cap.subscribed_mono = time.monotonic()
            with cap.lock:
                cap.assembler.subscribed(time.monotonic())
            print("capture: subscribed (%d)" % cap.subscriptions, flush=True)
            while not stop.is_set() and not sub.done():
                stop.wait(1)
                cap.tick()
                if time.monotonic() - max(cap.last_chunk_mono or 0.0, cap.subscribed_mono) > QUIET_S:
                    print("capture: no chunks for %d s, resubscribing" % QUIET_S, flush=True)
                    break
                if time.time() - last_prune > 600:
                    cap.prune()
                    last_prune = time.time()
        except Exception as e:
            print("capture: %s: %s" % (type(e).__name__, str(e)[:200]), flush=True)
            stop.wait(5)
            try:
                cap.tick()      # keep the minutes and status.json going while Yamcs is away
            except Exception as e2:
                print("capture: tick failed: %s" % e2, flush=True)
        finally:
            for closer in (lambda: sub and sub.cancel(), lambda: client and client.close()):
                try:
                    closer()
                except Exception:
                    pass
    cap.shutdown()
    print("capture: stopped; totals %s" % json.dumps(cap.totals, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
