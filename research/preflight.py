"""Everything that has to be true before a run is allowed to start. Charter 5, phase 1.

Three of the traps that cost the most time on 22 September were preflight failures, not pilot bugs:

  1. the payload kept its map and its barrier marks across RESET_GAME, so every comparison run after the
     first started on a level the payload already believed was walled in (EXPLORED_CELLS 662 against 1);
  2. a stale pilot.py stayed alive and went on appending to the same decisions.jsonl, silently merging two
     runs into one log;
  3. nobody recorded which graph, which model and which commit a number came from, so two numbers could
     not be compared afterwards even when both were real.

So: check the honesty suite, find and kill the orphans, prove the payload is fresh, take a lock on the run
directory, and write down what is about to fly.

    python research/preflight.py --run-dir research/out/my-run
    python research/preflight.py --run-dir ... --kill --require-fresh-payload
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import honesty                         # noqa: E402

SPACE_SYSTEM = "/DoomSat_DoomSat/DoomSat/doom"
ORPHAN_PATTERNS = ("pilot.py", "runner.py", "doom_payload.py", "vizdoom")


def git(*a):
    try:
        return subprocess.check_output(["git"] + list(a), cwd=str(ROOT), text=True).strip()
    except Exception:                                          # noqa: BLE001
        return None


# ---------------------------------------------------------------- orphans
def windows_processes():
    if platform.system() != "Windows":
        return []
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
          "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps], text=True, timeout=60)
        data = json.loads(out) if out.strip() else []
    except Exception:                                          # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = [data]
    return [(int(p["ProcessId"]), p["CommandLine"]) for p in data if p.get("CommandLine")]


def wsl_processes(distro):
    if platform.system() != "Windows":
        cmd = ["bash", "-lc", "ps -eo pid,args --no-headers"]
    else:
        cmd = ["wsl", "-d", distro, "--", "bash", "-lc", "ps -eo pid,args --no-headers"]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=60)
    except Exception:                                          # noqa: BLE001
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        if pid.isdigit():
            rows.append((int(pid), args.strip()))
    return rows


def find_orphans(distro, expect=()):
    """Every pilot, runner, payload or game process that is not one this run expects.

    `expect` is a list of substrings -- a run id, usually -- so that starting a run does not report the
    run as its own orphan. Everything else is fair game: the first time this was pointed at the machine
    it found eighteen ViZDoom instances left over from earlier sessions, one of which had been holding a
    directory handle that blocked a folder rename for an hour.
    """
    out = []
    for where, rows in (("windows", windows_processes()), ("wsl", wsl_processes(distro))):
        for pid, cmd in rows:
            if pid == os.getpid() or "preflight.py" in cmd:
                continue
            if any(e and e in cmd for e in expect):
                continue
            if any(p in cmd for p in ORPHAN_PATTERNS):
                out.append((where, pid, cmd[:110]))
    return out


def kill(where, pid, distro):
    try:
        if where == "windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
        elif platform.system() == "Windows":
            subprocess.run(["wsl", "-d", distro, "--", "kill", "-9", str(pid)], capture_output=True, timeout=30)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=30)
        return True
    except Exception:                                          # noqa: BLE001
        return False


# ---------------------------------------------------------------- the payload's memory
def explored_cells(yamcs="http://localhost:8090", instance="fprime-project", timeout=5):
    """EXPLORED_CELLS as Yamcs last saw it, or None if Yamcs is not up."""
    url = ("%s/api/processors/%s/realtime/parameters%s/EXPLORED_CELLS"
           % (yamcs.rstrip("/"), instance, SPACE_SYSTEM))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:                                          # noqa: BLE001
        return None
    v = (data.get("engValue") or {})
    for k in ("uint32Value", "sint32Value", "uint64Value", "floatValue", "doubleValue"):
        if k in v:
            return int(v[k])
    return None


# ---------------------------------------------------------------- the run lock
def publish_honesty(ok, yamcs="http://localhost:8090", instance="fprime-project", timeout=5):
    """Tell the Open MCT displays (docs/OPENMCT.md, the HONESTY indicator) what preflight found.

    Best effort: /DoomGround/HonestyStatus is a display value, not a gate. The gate is this script's exit code.
    """
    url = "%s/api/processors/%s/realtime/parameters/DoomGround/HonestyStatus" % (yamcs.rstrip("/"), instance)
    # the body is the yamcs.protobuf.Value itself; wrapping it in {"value": ...} is a 400 on Yamcs 5.12
    body = json.dumps({"type": "STRING", "stringValue": "PASS" if ok else "FAIL"}).encode()
    req = urllib.request.Request(url, data=body, method="PUT", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as e:
        print("  (HonestyStatus not published to Yamcs: %s)" % e)
        return False


class RunLock:
    """One writer per run directory. Trap 2 was two pilots appending to one log for an hour."""

    def __init__(self, run_dir):
        self.path = Path(run_dir) / "RUN.lock"

    def take(self, force=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not force:
            held = json.loads(self.path.read_text(encoding="utf-8"))
            return False, held
        self.path.write_text(json.dumps({"pid": os.getpid(), "host": platform.node(),
                                         "taken": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        return True, None

    def release(self):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- the report
def record(run_dir, extra=None):
    import hashlib
    sha = lambda p: hashlib.sha1(open(p, "rb").read()).hexdigest()[:12]
    try:
        sys.path.insert(0, str(ROOT / "ground"))
        import graph_config as gc
        graph = gc.load()
        graph_bits = {"graph_version": graph.get("version"), "model": graph.get("model")}
    except Exception as e:                                     # noqa: BLE001
        graph_bits = {"graph_error": str(e)}
    out = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
           "dirty": bool(git("status", "--porcelain")),
           "levels_sha": sha(HERE / "levels.yaml"),
           "knowledge_sha": sha(ROOT / "knowledge" / "doom_rules.yaml"),
           "metrics_sha": sha(HERE / "frozen_metrics.py"),
           **graph_bits, **(extra or {})}
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "preflight.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", default=None, help="take the lock here and write preflight.json")
    ap.add_argument("--kill", action="store_true", help="kill the orphans instead of only listing them")
    ap.add_argument("--force-lock", action="store_true", help="take the lock even if one is held")
    ap.add_argument("--require-fresh-payload", action="store_true",
                    help="fail unless Yamcs reports EXPLORED_CELLS == 1 (charter 2.2)")
    ap.add_argument("--expect", nargs="*", default=[],
                    help="substrings of command lines that belong to this run, e.g. its run id, so that "
                         "starting a run does not report the run as its own orphan")
    ap.add_argument("--distro", default="ros2")
    ap.add_argument("--yamcs", default="http://localhost:8090")
    ap.add_argument("--instance", default="fprime-project")
    a = ap.parse_args(argv)
    bad = []

    print("honesty (charter 2.4)")
    for f in honesty.run():
        print("  %-4s %-36s %s" % ("ok" if f.ok else "FAIL", f.test, f.detail))
        if not f.ok:
            bad.append(f.test)
    for f in honesty.run_canaries():
        if not f.ok:
            print("  FAIL %s -- %s" % (f.test, f.detail))
            bad.append(f.test)
    print("  canaries: %d planted leaks, all caught" % len(honesty.CANARIES) if not bad else "")
    honesty_ok = not bad

    print("\norphans (trap 2: a stale pilot appending to the same log)")
    orphans = find_orphans(a.distro, a.expect)
    if not orphans:
        print("  none")
    for where, pid, cmd in orphans:
        if a.kill:
            print("  killed %-7s %-7d %s" % (where, pid, cmd) if kill(where, pid, a.distro)
                  else "  COULD NOT KILL %s %d %s" % (where, pid, cmd))
        else:
            print("  %-7s %-7d %s" % (where, pid, cmd))
    if orphans and not a.kill:
        bad.append("orphan processes are running: re-run with --kill, or stop them")

    print("\nthe payload's memory (trap 1: the map survived RESET_GAME)")
    cells = explored_cells(a.yamcs, a.instance)
    if cells is None:
        print("  Yamcs did not answer: cannot see the payload from here")
        if a.require_fresh_payload:
            bad.append("--require-fresh-payload but Yamcs is not reachable")
    elif cells <= 1:
        print("  EXPLORED_CELLS = %d: the payload is fresh" % cells)
    else:
        print("  EXPLORED_CELLS = %d: this payload has already walked a level. Restart it with "
              "`scripts/flight.sh payload`" % cells)
        if a.require_fresh_payload:
            bad.append("the payload is not fresh (EXPLORED_CELLS = %d)" % cells)
    if cells is not None:
        publish_honesty(honesty_ok and cells <= 1, a.yamcs, a.instance)

    if a.run_dir:
        lock = RunLock(a.run_dir)
        got, held = lock.take(a.force_lock)
        print("\nrun directory %s" % a.run_dir)
        if got:
            print("  lock taken")
        else:
            print("  LOCK HELD by pid %s on %s since %s" % (held.get("pid"), held.get("host"), held.get("taken")))
            bad.append("the run directory is locked by another run")
        info = record(a.run_dir, {"explored_cells_at_preflight": cells})
        print("  commit %s%s on %s, graph v%s, model %s"
              % ((info.get("commit") or "?")[:9], " (dirty)" if info.get("dirty") else "",
                 info.get("branch"), info.get("graph_version"), info.get("model")))

    print("\n%s" % ("PREFLIGHT FAILED:\n  - " + "\n  - ".join(bad) if bad else "preflight clear"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
