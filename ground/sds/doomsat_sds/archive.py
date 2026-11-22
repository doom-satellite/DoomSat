"""Reading the Yamcs archive, and the same reads replayed from a recorded file.

Everything above this module works on plain Python values: times are integer milliseconds since the epoch
(UTC), parameter series are lists of (generation_ms, reception_ms, value), events and commands are dicts.
`YamcsArchive` turns yamcs-client objects into those; `RecordedArchive` serves the same shapes from a JSON
file, which is how the tests run with no network. `record()` writes such a file from a live Yamcs.

Things about this archive that cost time to find (docs/plans/sds-airflow.md, "What the code says"):
- One `stream_parameter_values` call is a single server-side replay; a `list_parameter_values` per name is one
  replay each. Same values, five times slower.
- Event filters: lowercase `or`/`and` silently match nothing on Yamcs 5.12.8. A regex on `type` works.
- `list_command_history(command=...)` builds a URL Yamcs 5.12.8 does not route; list without a name instead.
- A long replay can die inside Yamcs and leave its HTTP response open for ever. Seen live 30 s into a 2.5-hour
  streamParameterValues: "Channel did not become writable in 10 seconds", then IllegalReferenceCountExceptions,
  then nothing; yamcs-client sets no read timeout, so the reader waited for ever. Parameters are therefore read in
  CHUNK_MS pieces, every request carries a read timeout, and a chunk that times out or fails is asked for once more
  before the error is raised.
- Values: F´ bools arrive as the strings "True"/"False" (an XTCE enumeration), F32 as Python floats widened from
  float32, enums as their labels. `value()` normalises the first two.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Iterable, Protocol

from . import config

Series = list  # list[tuple[int, int, object]]: generation ms, reception ms, value


def to_ms(t: dt.datetime) -> int:
    return int(round(t.timestamp() * 1000))


def from_ms(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def iso(ms: int) -> str:
    """2026-10-04T00:47:23.473Z: millisecond precision, always UTC."""
    return from_ms(ms).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def value(v):
    """One archive value as the products store it: bools as bools, float32 at the precision it has."""
    if v == "True":
        return True
    if v == "False":
        return False
    if isinstance(v, float):
        # A float32 widened to double prints as 592.05224609375; nine significant digits round-trip any
        # float32 exactly and keep the products small and byte-stable.
        return float("%.9g" % v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, dt.datetime):
        return iso(to_ms(v))
    return v


class Archive(Protocol):
    def events(self, start_ms: int, stop_ms: int, types: Iterable[str] | None = None) -> list[dict]: ...
    def parameters(self, names: Iterable[str], start_ms: int, stop_ms: int) -> dict[str, Series]: ...
    def commands(self, start_ms: int, stop_ms: int) -> list[dict]: ...


def _event_dict(e) -> dict:
    return {"t": to_ms(e.generation_time), "rt": to_ms(e.reception_time) if e.reception_time else None,
            "source": e.source, "type": e.event_type, "message": e.message,
            "extra": {k: str(v) for k, v in (e.extra or {}).items()}, "seq": e.sequence_number}


def _command_dict(c) -> dict:
    acks = {k[len("Acknowledge_"):-len("_Status")]: str(v) for k, v in (c.attributes or {}).items()
            if k.startswith("Acknowledge_") and k.endswith("_Status")}
    return {"t": to_ms(c.generation_time), "id": c.id, "name": c.name, "origin": c.origin,
            "user": c.username, "args": {k: value(v) for k, v in (c.assignments or {}).items()},
            "comment": c.comment, "acks": acks}


CHUNK_MS = 10 * 60 * 1000       # parameter reads go to Yamcs in replays of at most this long
READ_TIMEOUT_S = 60             # no byte for this long and the request is given up on


def with_read_timeout(session, read_s: float = READ_TIMEOUT_S):
    """Give every request on a requests.Session a (connect, read) timeout unless the call names its own."""
    plain = session.request

    def request(method, url, **kw):
        kw.setdefault("timeout", (10, read_s))
        return plain(method, url, **kw)

    session.request = request
    return session


class YamcsArchive:
    """The live archive through yamcs-client (imported here, so nothing else needs it)."""

    def __init__(self, address: str = "localhost:8090", instance: str = config.INSTANCE,
                 read_s: float = READ_TIMEOUT_S):
        from yamcs.client import YamcsClient
        self.client = YamcsClient(address)
        with_read_timeout(self.client.ctx.session, read_s)
        self.archive = self.client.get_archive(instance)
        self.instance = instance

    def server_id(self) -> str:
        return self.client.get_server_info().id

    def events(self, start_ms, stop_ms, types=None):
        flt = None
        if types:
            flt = 'type =~ "^(%s)$"' % "|".join(t.replace(".", "\\.") for t in types)
        out = [_event_dict(e) for e in self.archive.list_events(
            source=config.EVENT_SOURCE, filter=flt, start=from_ms(start_ms), stop=from_ms(stop_ms), page_size=1000)]
        return sorted(out, key=lambda e: (e["t"], e["seq"] or 0))

    def parameters(self, names, start_ms, stop_ms):
        names = list(names)
        out: dict[str, Series] = {n: [] for n in names}
        if stop_ms <= start_ms:
            return out
        wanted = {config.qualified(n): n for n in names}
        t = start_ms
        while t < stop_ms:
            u = min(t + CHUNK_MS, stop_ms)
            for attempt in (1, 2):
                got: dict[str, Series] = {n: [] for n in names}
                try:
                    for pdata in self.archive.stream_parameter_values(list(wanted), start=from_ms(t), stop=from_ms(u)):
                        for p in pdata.parameters:
                            n = wanted.get(p.name)
                            if n is not None:
                                got[n].append((to_ms(p.generation_time),
                                               to_ms(p.reception_time) if p.reception_time else None,
                                               value(p.eng_value)))
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print("archive: parameters %s..%s failed (%s: %s); asking once more"
                          % (iso(t), iso(u), type(e).__name__, e))
            # A sample on a boundary between chunks would come back twice; the outer bounds are left to Yamcs,
            # as they were when the whole window was one replay.
            lo, hi = (None if t == start_ms else t), (None if u == stop_ms else u)
            for n in names:
                out[n].extend(s for s in got[n] if (lo is None or s[0] >= lo) and (hi is None or s[0] < hi))
            t = u
        for n in out:
            out[n].sort(key=lambda s: s[0])
        return out

    def commands(self, start_ms, stop_ms):
        out = [_command_dict(c) for c in self.archive.list_command_history(
            start=from_ms(start_ms), stop=from_ms(stop_ms), page_size=1000)]
        return sorted(out, key=lambda c: (c["t"], c["id"]))


class RecordedArchive:
    """The same three reads, served from a file `record()` wrote. Windows are applied exactly as Yamcs does:
    start inclusive, stop exclusive, on generation time."""

    def __init__(self, path: str | Path):
        raw = Path(path).read_bytes()
        self.data = json.loads(gzip.decompress(raw) if str(path).endswith(".gz") else raw)

    def events(self, start_ms, stop_ms, types=None):
        types = set(types) if types else None
        return [e for e in self.data["events"]
                if start_ms <= e["t"] < stop_ms and (types is None or e["type"] in types)]

    def parameters(self, names, start_ms, stop_ms):
        series = self.data["parameters"]
        return {n: [tuple(s) for s in series.get(n, []) if start_ms <= s[0] < stop_ms] for n in names}

    def commands(self, start_ms, stop_ms):
        return [c for c in self.data["commands"] if start_ms <= c["t"] < stop_ms]

    def server_id(self) -> str:
        return self.data.get("server_id", "vm")


def link_parameters(server_id: str) -> tuple[str, ...]:
    """Yamcs's own count of TM frames received on the main downlink, archived at 1 Hz."""
    return (f"/yamcs/{server_id}/links/UDP_TM_IN/dataInCount",)


def record(archive: YamcsArchive, start_ms: int, stop_ms: int, path: str | Path) -> dict:
    """Write everything the forward pipeline reads for [start, stop) to a file RecordedArchive can serve."""
    sid = archive.server_id()
    names = list(config.SCIENCE) + list(config.LINK) + list(link_parameters(sid))
    events = archive.events(start_ms, stop_ms)
    data = {"recorded_from": archive.client.address if hasattr(archive.client, "address") else "yamcs",
            "server_id": sid, "start_ms": start_ms, "stop_ms": stop_ms,
            "events": [e for e in events if e["type"].startswith(config.EVENT_PREFIX)],
            "parameters": {n: [list(s) for s in v] for n, v in archive.parameters(names, start_ms, stop_ms).items()},
            "commands": archive.commands(start_ms, stop_ms)}
    blob = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    Path(path).write_bytes(gzip.compress(blob, mtime=0) if str(path).endswith(".gz") else blob)
    return {k: len(v) if isinstance(v, list) else v for k, v in data.items() if k != "parameters"}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Record a window of the Yamcs archive as a test fixture.")
    p.add_argument("--start", required=True, help="ISO UTC, e.g. 2026-10-04T00:42:00Z")
    p.add_argument("--stop", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--yamcs", default=config.Settings.from_env().yamcs)
    a = p.parse_args()
    parse = lambda s: to_ms(dt.datetime.fromisoformat(s.replace("Z", "+00:00")))
    print(record(YamcsArchive(a.yamcs), parse(a.start), parse(a.stop), a.out))
