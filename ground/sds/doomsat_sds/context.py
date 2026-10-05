"""The context an episode was flown in: WAD, map, skill, seed, pilot mode, repo commit, dev or test set.

On main the WAD is in telemetry, because the payload can switch it in flight (LOAD_WAD): the base and patch WAD
file names and the number of switches so far, /DoomSat_DoomSat/DoomSat/doom/WAD_IWAD, WAD_PWAD and WAD_LOADS
(config.CONTEXT_TLM). flown_wad() reads them from the archive, apart from the L1 read, and takes the value in
effect when the episode began. The rest is not in telemetry (LEVEL counts levels started; it is not a map name):
the map is in payload.log ("episode N started on MAP (level L)"), which the payload also prints after a switch,
skill, seed and the other launch options in the running payload's arguments, the pilot mode in the pilot's. So the
context is captured once, when the forward run processes the episode, and kept in the catalog; reprocessing reuses
it.

Each value says where it came from. A flight build from before main has no WAD telemetry and cannot switch WAD,
so there the WAD is the one in the payload's arguments, as it always was, and the source says so. A process that
started after the episode began did not fly it, and then the value is "unknown" rather than a guess, and so is a
pilot that is not running when the context is captured (it may have exited after flying the episode). A
downlinked payload record, where there is one, carries the payload's own account of WAD, map and skill as a
cross-check. WADs are known here by name only.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import config
from .archive import iso
from .config import Settings

LOG_START = re.compile(r"^\[payload\] episode (\d+) started on (\S+) \(level (\d+)\)")
PAYLOAD_DEFAULTS = {"--wad": "doom1.wad", "--map": "E1M1", "--skill": "2", "--seed": "7",   # doom_payload.py argparse
                    "--geometry": "off", "--oracle": "off", "--records": "off"}
WAD_LOOKBACK_MS = 5_000     # the WAD channels repeat about once a second; this far back, when the window has none
# How yamcs-client words the two answers a mission database without the WAD channels gives (NotFound, and
# YamcsError for a 400; yamcs/client/core/context.py). Any other 4xx (401, 403, 429...) is a failure, raised.
REFUSED = re.compile(r"^(400|404) Client Error")


def _boot_time() -> float | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return None


def processes() -> list[dict]:
    """[{pid, argv, start_s}] from /proc (Linux, WSL); empty where there is no /proc."""
    out, boot, hz = [], _boot_time(), os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for d in Path("/proc").glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in argv if a]
        if not argv:
            continue
        ticks = int(stat.rsplit(")", 1)[1].split()[19])          # field 22, after the parenthesised name
        out.append({"pid": int(d.name), "argv": argv, "start_s": boot + ticks / hz if boot else None})
    return out


def _find(procs: list[dict], script: str) -> dict | None:
    """The interpreter running `script` (not a `bash -c` wrapper that merely mentions it); oldest wins.

    A LOAD_WAD check runs the payload script again with --probe for up to 15 s, beside a forked watchdog with the
    same arguments for up to 20 s: neither is the flight payload, whatever its start time."""
    hits = [p for p in procs if not p["argv"][0].endswith("bash") and "--probe" not in p["argv"]
            and any(a == script or a.endswith("/" + script) for a in p["argv"][1:])]
    return min(hits, key=lambda p: p["start_s"] or 0) if hits else None


def _options(argv: list[str]) -> dict:
    """--key value, --key=value and bare flags (true). A flag never swallows the option after it; a repeated
    option keeps its last value, as argparse does."""
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                opts[k] = v
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[a] = argv[i + 1]
                i += 1
            else:
                opts[a] = "true"
        i += 1
    return opts


def map_from_log(log_text: str, number: int) -> tuple[str, int] | None:
    """(map, level) for episode `number` from one payload process's log, or None."""
    hit = None
    for line in log_text.splitlines():
        m = LOG_START.match(line.strip())
        if m and int(m.group(1)) == number:
            hit = (m.group(2), int(m.group(3)))
    return hit


def level_set(repo: Path, wad: str | None, map_name: str | None, pwad: str | None = None,
              wad_loads: int | None = 0) -> str:
    """dev, test or other, from research/levels.yaml (read only; it is the harness's file); unknown without both.

    A flight that switched WAD in flight or flies a patch WAD is a demonstration, never dev or test (README.md:
    flights on an uplinked WAD are never benched or graded), and a name cannot tell an uplinked file from an
    installed one: LOAD_WAD looks in the uplink directory first. So those are "other" whatever levels.yaml says."""
    if pwad or (wad_loads or 0) > 0:
        return "other"
    if not wad or not map_name:
        return "unknown"
    try:
        import yaml
        levels = yaml.safe_load((repo / "research" / "levels.yaml").read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    for name in ("dev", "test"):
        s = levels.get(name) or {}
        if wad and os.path.basename(wad) == s.get("wad") and map_name in (s.get("maps") or []):
            return name
    return "other"


def repo_commit(repo: Path) -> str | None:
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return (head + ("+dirty" if dirty else "")) or None


def wad_name(v) -> str | None:
    """A WAD_IWAD or WAD_PWAD sample as a file name. The channel is 40 ASCII bytes, zero-padded, and
    archive.value() turns bytes into hex; all zeros (no patch WAD) is None."""
    if v is None:
        return None
    try:
        raw = bytes(v) if isinstance(v, (bytes, bytearray)) else bytes.fromhex(v)
    except (TypeError, ValueError):
        return str(v) or None                           # already text
    return os.path.basename(raw.rstrip(b"\0").decode("ascii", "replace")) or None


def _in_effect(series: list, start_ms: int, last_ms: int) -> tuple[tuple | None, str | None]:
    """The sample in effect at start_ms and how it was chosen: the first one in [start_ms, last_ms], else the last
    one before start_ms (no older than WAD_LOOKBACK_MS).

    The WAD cannot change inside an episode's window: a LOAD_WAD or a payload restart starts a new episode, and the
    payload reports its WAD before that episode's first status. F' writes that report once and otherwise repeats
    the last one once a second, so if the one write is lost, the last sample before the window is a repeat of the
    old WAD while every sample inside it is the new one. Never a later one: a switch writes the new WAD's sample
    before the next episode's EpisodeStarted, so it lies between this episode's window and its closure."""
    inside = [s for s in series if start_ms <= s[0] <= last_ms]
    if inside:
        return inside[0], "the first sample during the episode"
    before = [s for s in series if start_ms - WAD_LOOKBACK_MS <= s[0] < start_ms]
    return (before[-1], "the last sample before the episode's first status") if before else (None, None)


def _count(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def flown_wad(archive, window, end_ms: int | None = None) -> dict:
    """The WAD in effect when the episode began, from telemetry. `window` is its [first_ms, last_ms] from
    locate, `end_ms` its closing event's time; no sample after either is used.

    {"wad", "pwad", "wad_loads", "t_ms", "how"} when the archive has the samples, or {"missing": why} when it has
    none or refuses the names: both mean a flight build or mission database from before main. Any other failure
    (Yamcs down, slow or failing) is raised, so the task is retried rather than a guess cataloged for good.
    """
    first, last = window
    if end_ms is not None:
        last = min(last, end_ms)
    try:
        s = archive.parameters(list(config.CONTEXT_TLM), first - WAD_LOOKBACK_MS, last + 1)
    except Exception as e:
        if not REFUSED.match(str(e)):
            raise
        return {"missing": "the archive refused the names %s (%s: %s), as a mission database from before main "
                           "does" % ("/".join(config.CONTEXT_TLM), type(e).__name__, str(e)[:120])}
    base, how = _in_effect(s.get("WAD_IWAD") or [], first, last)
    if base is None or wad_name(base[2]) is None:
        return {"missing": "the archive has no WAD_IWAD sample from %d s before the episode to its end, as on a "
                           "flight build from before main" % (WAD_LOOKBACK_MS // 1000)}
    patch = _in_effect(s.get("WAD_PWAD") or [], first, last)[0]
    loads = _in_effect(s.get("WAD_LOADS") or [], first, last)[0]
    return {"wad": wad_name(base[2]), "pwad": wad_name(patch[2]) if patch else None,
            "wad_loads": _count(loads[2]) if loads else None, "t_ms": base[0], "how": how}


def capture(settings: Settings, number: int, start_ms: int, procs: list[dict] | None = None,
            log_text: str | None = None, flown: dict | None = None) -> dict:
    """The context of episode `number`, which began at TM time `start_ms`, as far as the live system shows.

    `flown` is flown_wad()'s answer for the episode. Without WAD samples in it, the WAD is the payload's --wad and
    --pwad, which is right only on a flight build from before main (it cannot switch WAD)."""
    procs = processes() if procs is None else procs
    started = start_ms / 1000 + 5        # TM time runs ~1 s ahead of the process clock; allow a margin
    ctx: dict = {"sources": {}}
    tlm = flown if flown and "wad" in flown else None
    missing = (flown or {}).get("missing", "the WAD telemetry was not read")
    if tlm:
        ctx["wad"], ctx["pwad"], ctx["wad_loads"] = tlm["wad"], tlm["pwad"], tlm["wad_loads"]
        ctx["sources"]["wad"] = "telemetry %s: %s (%s)" % (", ".join(config.CONTEXT_TLM), tlm["how"], iso(tlm["t_ms"]))

    payload = _find(procs, "doom_payload.py")
    if payload and payload["start_s"] is not None and payload["start_s"] <= started:
        opts = _options(payload["argv"])
        for key in ("--skill", "--seed", "--geometry", "--oracle", "--records"):
            ctx[key[2:]] = opts.get(key, PAYLOAD_DEFAULTS.get(key))
        ctx["wad_launch"] = opts.get("--wad", PAYLOAD_DEFAULTS["--wad"])
        ctx["sources"]["payload"] = "arguments of the running payload (pid %d)" % payload["pid"]
        if not tlm:
            ctx["wad"] = os.path.basename(ctx["wad_launch"])
            ctx["pwad"] = os.path.basename(opts["--pwad"]) if opts.get("--pwad") else None
            ctx["wad_loads"] = 0
            ctx["sources"]["wad"] = "payload --wad and --pwad (pid %d), because %s" % (payload["pid"], missing)
        if log_text is None:
            try:
                log_text = (settings.run_dir / "payload.log").read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
        hit = map_from_log(log_text, number)
        if hit:
            ctx["map"], ctx["level"] = hit
            ctx["sources"]["map"] = "payload.log"
        elif opts.get("--map", PAYLOAD_DEFAULTS["--map"]) and number == 1 and ctx["wad_loads"] == 0:
            # After a switch the map is the one LOAD_WAD named, not --map; payload.log is then the only source.
            ctx["map"] = opts.get("--map", PAYLOAD_DEFAULTS["--map"])
            ctx["sources"]["map"] = "payload --map (episode 1)"
        else:
            ctx["map"] = None
            ctx["sources"]["map"] = "unknown"
    else:
        for key in ("skill", "seed", "geometry", "oracle", "records", "map", "wad_launch"):
            ctx[key] = None
        ctx["sources"]["payload"] = "unknown: no payload process that was running when the episode began"
        if not tlm:
            ctx["wad"] = ctx["pwad"] = ctx["wad_loads"] = None
            ctx["sources"]["wad"] = ctx["sources"]["payload"] + ", and " + missing

    pilot = _find(procs, "pilot.py")
    if pilot and pilot["start_s"] is not None and pilot["start_s"] <= started:
        o = _options(pilot["argv"])
        ctx["pilot_mode"] = "system-one=%s system-two=%s" % (o.get("--system-one", "typesafe"),
                                                             o.get("--system-two", "claude-cli"))
        ctx["sources"]["pilot"] = "arguments of the running pilot (pid %d)" % pilot["pid"]
    elif pilot:
        ctx["pilot_mode"] = None
        ctx["sources"]["pilot"] = "unknown: the running pilot started after the episode began"
    else:
        ctx["pilot_mode"] = None
        ctx["sources"]["pilot"] = ("unknown: no pilot process when the context was captured (it may have exited "
                                   "after flying the episode, or a person drove from the dashboard)")

    for key in ("skill", "seed"):
        if ctx.get(key) is not None:
            try:
                ctx[key] = int(ctx[key])
            except ValueError:
                pass
    ctx["repo_commit"] = repo_commit(settings.repo)
    ctx["level_set"] = level_set(settings.repo, ctx.get("wad"), ctx.get("map"), ctx.get("pwad"), ctx.get("wad_loads"))
    return ctx
