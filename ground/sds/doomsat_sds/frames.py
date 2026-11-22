"""Reassembling the FRAME_CHUNK stream into images, and counting what arrived whole, late or never.

F´ splits each image the payload sends into chunks of up to 960 bytes, one telemetry packet per chunk:
FrameChunk {seq U32, index U16, count U16, length U16, data [960] U8} (Doom.fpp). Yamcs hands the value over
as a dict with `data` as 960 bytes, of which the first `length` are real. The payload's map PNG travels on the
same channel with the top bit of `seq` set; everything else is a 320x240 JPEG of the screen.

Counts kept per kind ("frame" and "map"):
    complete     every chunk arrived within `timeout_s` of the first
    incomplete   a sequence number seen, but not all its chunks in time (checked on a timer, not only when the
                 next chunk happens to arrive, so a stall at the end of a flight is still counted)
    missing      sequence numbers never seen at all (gaps between the highest seen and the next)
    duplicates   a chunk that had already arrived
    late         a chunk for a sequence number already given up on as incomplete
    cut          incomplete only because the subscription began part-way through it (first chunk seen was not
                 chunk 0, within a second of subscribing): the ground's doing, not the link's
The sequence counters restart when the payload restarts; a big step backwards is taken as that, not as gaps.

Pure: no I/O and no clock of its own (the caller passes monotonic time), so tests drive it directly.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

MAP_BIT = 0x80000000
RESTART_STEP = 1000          # seq going back by more than this is a payload restart


def kind_of(seq: int) -> str:
    return "map" if seq & MAP_BIT else "frame"


class FrameAssembler:
    def __init__(self, on_image: Callable[[str, int, bytes, int], None] | None = None, timeout_s: float = 3.0):
        self.on_image = on_image          # (kind, seq, data, t_ms of the first chunk)
        self.timeout_s = timeout_s
        self.partial: dict[int, dict] = {}
        self.done: dict[int, float] = {}  # recently completed seq -> when, to spot late duplicates
        self.dead: dict[int, float] = {}  # recently given-up seq -> when, so a straggler is not a new partial
        self.max_seen: dict[str, int] = {}
        self.counts: Counter = Counter()
        self.grace_until = float("-inf")

    def subscribed(self, now: float) -> None:
        """A (re)subscription starts now; an image already half-sent is not the link's fault."""
        self.grace_until = now + 1.0

    def add(self, chunk: dict, t_ms: int, now: float) -> None:
        seq, index, count = int(chunk["seq"]), int(chunk["index"]), int(chunk["count"])
        data = bytes(chunk["data"])[: int(chunk["length"])]
        kind = kind_of(seq)
        self.counts["chunks"] += 1
        self.counts["bytes"] += len(data)
        if seq in self.done:
            self.counts[kind + "_duplicate_chunks"] += 1
            return
        if seq in self.dead:
            self.counts[kind + "_late_chunks"] += 1
            return
        p = self.partial.get(seq)
        if p is not None and now - p["t0"] > self.timeout_s:
            # Too late, whenever the timer last ran: the image is incomplete, and this chunk a straggler.
            self.counts[kind + ("s_cut" if p["cut"] else "s_incomplete")] += 1
            del self.partial[seq]
            self.dead[seq] = now
            self.counts[kind + "_late_chunks"] += 1
            return
        if p is None:
            self._note_seq(kind, seq & ~MAP_BIT)
            p = self.partial[seq] = {"count": count, "parts": {}, "t0": now, "t_ms": t_ms,
                                     "cut": index != 0 and now < self.grace_until}
        if index in p["parts"]:
            self.counts[kind + "_duplicate_chunks"] += 1
        p["parts"][index] = data
        if len(p["parts"]) >= p["count"]:
            del self.partial[seq]
            self.done[seq] = now
            self.counts[kind + "s_complete"] += 1
            if self.on_image:
                self.on_image(kind, seq & ~MAP_BIT, b"".join(p["parts"][i] for i in sorted(p["parts"])), p["t_ms"])

    def _note_seq(self, kind: str, n: int) -> None:
        top = self.max_seen.get(kind)
        if top is None or n < top - RESTART_STEP:
            if top is not None:
                self.counts[kind + "_seq_restarts"] += 1
            self.max_seen[kind] = n
        elif n > top:
            self.counts[kind + "s_missing"] += n - top - 1
            self.max_seen[kind] = n

    def expire(self, now: float, everything: bool = False) -> None:
        """Count partials older than the timeout (or all of them, at shutdown) as incomplete."""
        for seq in [s for s, p in self.partial.items() if everything or now - p["t0"] > self.timeout_s]:
            self.counts[kind_of(seq) + ("s_cut" if self.partial[seq]["cut"] else "s_incomplete")] += 1
            del self.partial[seq]
            self.dead[seq] = now
        for table in (self.done, self.dead):
            for seq in [s for s, t in table.items() if now - t > 4 * self.timeout_s]:
                del table[seq]

    def take_counts(self) -> dict:
        """The counts since the last call, then start again from zero."""
        out = dict(self.counts)
        self.counts.clear()
        return out
