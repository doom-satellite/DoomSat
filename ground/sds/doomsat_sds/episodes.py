"""Which episodes have finished, and exactly which stretch of the archive belongs to each.

There is no "episode over" message. The payload sends STATUS records; F´ turns edges in them into events
(Doom.cpp:376-395), and those events have gaps:
- a flight's first EpisodeStarted fires before Yamcs is listening, so it is never archived;
- an episode ended by RESET_GAME (the pilot's level budget) has no PlayerDied or LevelFinished;
- EPISODE restarts at 1 when the payload restarts, so the number alone is not an identity.

So an episode is *closed* by PlayerDied or LevelFinished, or else by the next EpisodeStarted (outcome
"reset" when the number goes up by one, "interrupted" otherwise), or by PayloadConnected, which F´ logs when the
payload (re)connects (outcome "interrupted"; a restart that keeps the number 1 -> 1 logs no EpisodeStarted, and a
mere socket drop reconnects too, so that closure is an inference the caller confirms from TIC and EPISODE). On
main the payload can also switch WAD in flight (LOAD_WAD): it rebuilds the game and starts the next episode, and
F´ logs WadLoaded just before that EpisodeStarted. The episode it cut short is still closed by the EpisodeStarted,
at the same time and so with the same id, but its outcome is "wad_switch" rather than "reset".

Its *window* comes from the archived EPISODE and TIC samples rather than from events: the contiguous run of
EPISODE == n that ends at the closing event. Its id is the closing event's time and the number, which is unique,
sortable and stable however often the archive is read.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .archive import from_ms, iso

OUTCOME = {"PlayerDied": "died", "LevelFinished": "level_finished"}
# A run of EPISODE samples is broken by a silence longer than this (a flight or payload restart). Status
# arrives at about 11 Hz; the payload pauses 2 s between episodes, and that pause is between runs anyway.
MAX_SILENCE_MS = 30_000
# The final STATUS of an episode and the event F´ makes from it carry the same time tag; allow for jitter.
END_PAD_MS = 1_500


def episode_id(end_ms: int, number: int) -> str:
    """20261004T001512Z-e0003: when it ended, and its number on board."""
    return from_ms(end_ms).strftime("%Y%m%dT%H%M%SZ") + "-e%04d" % number


@dataclass(frozen=True)
class ClosedEpisode:
    number: int
    end_ms: int                     # time of the closing event (TM time)
    closing: str                    # PlayerDied | LevelFinished | EpisodeStarted
    outcome: str                    # died | level_finished | reset | wad_switch | interrupted
    start_event_ms: int | None      # its EpisodeStarted, when that was archived
    inferred: bool = False          # an inference to confirm against samples (lost predecessor, or a reconnect)

    @property
    def episode_id(self) -> str:
        return episode_id(self.end_ms, self.number)

    def as_conf(self) -> dict:
        d = asdict(self)
        d.update(episode_id=self.episode_id, end_utc=iso(self.end_ms))
        return d

    @classmethod
    def from_conf(cls, conf: dict) -> "ClosedEpisode":
        return cls(int(conf["number"]), int(conf["end_ms"]), conf["closing"], conf["outcome"],
                   None if conf.get("start_event_ms") is None else int(conf["start_event_ms"]),
                   bool(conf.get("inferred", False)))


def _number(event: dict) -> int | None:
    try:
        return int(event["extra"]["episode"])
    except (KeyError, TypeError, ValueError):
        return None


# A death or a finished level is followed by the next EpisodeStarted about 2 s later (the payload's pause), so
# if these events cover this long before a start and show no end for the episode before it, that episode was
# reset. Only an inference: the caller confirms it against the archived samples before acting on it.
INFER_AFTER_MS = 60_000
# F´ logs WadLoaded when it hands on the payload's report of a switch, and the payload sends that report before
# the status that starts the next episode, so the two events are well under a second apart (or share a time tag).
WAD_SWITCH_MS = 10_000


def _switched(switches: list[int], after_ms: int | None, at_ms: int) -> bool:
    """Was there a WadLoaded in (after_ms, at_ms], no more than WAD_SWITCH_MS before at_ms? Judged on times alone,
    so two events that share a time tag count whichever way the archive orders them."""
    return any((after_ms is None or w > after_ms) and at_ms - WAD_SWITCH_MS <= w <= at_ms for w in switches)


def closed_episodes(events: list[dict], scan_start_ms: int | None = None) -> list[ClosedEpisode]:
    """Episodes closed within these events (EpisodeStarted / PlayerDied / LevelFinished, any order in).

    `scan_start_ms` is where the event query began. With it, an EpisodeStarted(n) whose predecessor these
    events never mention (a flight's first episode, whose own start event is always lost) closes n-1 as an
    inferred reset, provided the events reach at least INFER_AFTER_MS further back.

    A reset, inferred or not, whose EpisodeStarted has a WadLoaded since the episode's own start and at most
    WAD_SWITCH_MS before it, is a "wad_switch". Only the outcome changes: the closing event, its time and so the
    episode id stay those of the EpisodeStarted, so an episode cataloged as a reset before WadLoaded was read is
    not found again under another id.
    """
    out: list[ClosedEpisode] = []
    open_n: int | None = None          # the episode in progress, as far as these events show
    open_start: int | None = None
    open_closed = False                # has it already had its PlayerDied / LevelFinished?
    since: int | None = None           # the last start or payload (re)connection: a WadLoaded before it is old news
    switches = sorted(e["t"] for e in events if e["type"].rsplit(".", 1)[-1] == "WadLoaded")
    for e in sorted(events, key=lambda e: (e["t"], e.get("seq") or 0)):
        name = e["type"].rsplit(".", 1)[-1]
        if name == "PayloadConnected":
            if open_n is not None and not open_closed:
                out.append(ClosedEpisode(open_n, e["t"], "PayloadConnected", "interrupted", open_start, inferred=True))
            open_n, open_start, open_closed = None, None, False     # whatever comes next belongs to a new start
            since = e["t"]
            continue
        if name == "WadLoaded":
            continue                                      # it carries no episode number; `switches` has its time
        n = _number(e)
        if n is None:
            continue
        if name in OUTCOME:
            if open_n == n and open_closed:
                continue                                  # a repeat of the same edge
            out.append(ClosedEpisode(n, e["t"], name, OUTCOME[name], open_start if open_n == n else None))
            open_n, open_closed = n, True
        elif name == "EpisodeStarted":
            if n == open_n and not open_closed:
                continue                                  # F´ restarted mid-episode and re-announced it
            if open_n is not None and not open_closed:
                outcome = "reset" if n == open_n + 1 else "interrupted"
                if outcome == "reset" and _switched(switches, open_start, e["t"]):
                    outcome = "wad_switch"
                out.append(ClosedEpisode(open_n, e["t"], "EpisodeStarted", outcome, open_start))
            elif open_n is None and n > 1 and scan_start_ms is not None and e["t"] - scan_start_ms >= INFER_AFTER_MS:
                # Most often a flight's first episode, whose start is never archived, and a demonstration's first
                # LOAD_WAD is likely to end exactly that one.
                outcome = "wad_switch" if _switched(switches, since, e["t"]) else "reset"
                out.append(ClosedEpisode(n - 1, e["t"], "EpisodeStarted", outcome, None, inferred=True))
            open_n, open_start, open_closed = n, e["t"], False
            since = e["t"]
    return out


def _runs(episode: list[tuple], tic: list[tuple]) -> list[tuple[int, int, int]]:
    """(value, first_ms, last_ms) runs of EPISODE, broken by long silences and by TIC going backwards."""
    tic_at = {t: v for t, _, v in tic}
    runs: list[list[int]] = []
    prev_t = prev_tic = None
    for t, _, v in episode:
        cur_tic = tic_at.get(t)
        restart = (prev_t is not None and t - prev_t > MAX_SILENCE_MS) or (
            cur_tic is not None and prev_tic is not None and cur_tic < prev_tic)
        if runs and runs[-1][0] == v and not restart:
            runs[-1][2] = t
        else:
            runs.append([v, t, t])
        prev_t = t
        if cur_tic is not None:
            prev_tic = cur_tic
    return [tuple(r) for r in runs]


def locate(episode: list[tuple], tic: list[tuple], ep: ClosedEpisode) -> tuple[int, int] | None:
    """The window [first_ms, last_ms] of episode `ep` in these EPISODE and TIC samples, or None.

    It is the last run with EPISODE == ep.number that starts before the closing event and ends no later than
    END_PAD_MS after it. For a reset the closing event is the next episode's start, so the run has already
    ended; for a death the final STATUS and the event share a time tag.
    """
    candidates = [r for r in _runs(episode, tic)
                  if r[0] == ep.number and r[1] <= ep.end_ms and r[2] <= ep.end_ms + END_PAD_MS]
    if not candidates:
        return None
    _, first, last = candidates[-1]
    return first, last
