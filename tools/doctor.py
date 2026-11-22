"""Check what is installed and what is running, one line per piece, with the command that fixes it.

    python tools/doctor.py           installs, keys, and which services answer
    python tools/doctor.py --jev     also makes one real jev call with the key in .env

Uses only the standard library (plus `requests` for --jev), so any Python 3.10+ can run it.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ground"))
ENV_FILES = [str(ROOT / ".env"), str(ROOT / "ground" / ".env")]
WINDOWS = platform.system() == "Windows" or sys.platform == "cygwin"
bad = 0


def line(ok, what, fix=""):
    global bad
    bad += 0 if ok in (True, None) else 1
    mark = {True: " ok ", False: "FAIL", None: " -- "}[ok]
    print(f"[{mark}] {what}" + (f"\n         fix: {fix}" if fix and ok is False else ""))


def env_key(name):
    # the same lookup as providers.load_env_key, without importing requests
    if os.environ.get(name):
        return os.environ[name].strip()
    for f in ENV_FILES:
        if os.path.exists(f):
            for ln in open(f, encoding="utf-8"):
                if ln.strip().startswith(name + "="):
                    return ln.split("=", 1)[1].strip().strip("'\"") or None
    return None


def answers(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status < 500
    except Exception:  # noqa: BLE001
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jev", action="store_true", help="make one real jev call")
    a = p.parse_args()

    print("== keys (.env at the repo root; see .env.example)")
    line((ROOT / ".env").exists() or (ROOT / "ground" / ".env").exists(), ".env present", "cp .env.example .env")
    if env_key("TYPESAFE_API_KEY"):
        line(True, "TYPESAFE_API_KEY (jev, System One)")
    else:   # only jev needs it: the stack flies without it (scripts/play.sh)
        line(None, "TYPESAFE_API_KEY not set: jev cannot fly, but scripts/play.sh (you drive) and "
                   "scripts/play.sh --autopilot (the code rules) need no key")
    claude = shutil.which("claude")
    line(bool(claude) or None, f"claude CLI (System Two, default; uses your Claude Code login): {claude or 'not found'}",
         "")
    if not claude:
        line(bool(env_key("ANTHROPIC_API_KEY")) or None,
             "ANTHROPIC_API_KEY (System Two over the API: --system-two anthropic); or run with --system-two none")

    print("== ground")
    venv = ROOT / "ground" / ".venv"
    line(venv.exists(), "ground/.venv", "scripts/setup_ground.sh python")
    line(shutil.which("node") is not None, f"node {subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip() if shutil.which('node') else ''}".strip(),
         "install Node.js 24 (see README)")
    omct = ROOT / "external" / "openmct-yamcs" / "node_modules" / "openmct" / "dist" / "openmct.js"
    line(omct.exists(), "Open MCT built (external/openmct-yamcs)", "scripts/setup_ground.sh openmct")

    print("== flight (installed under DOOMSAT_HOME)")
    if WINDOWS:
        line(None, "on Windows the flight side lives in WSL: scripts/flight.sh status")
    else:
        home = Path(os.environ.get("DOOMSAT_HOME", Path.home() / "doom"))
        line((home / "payload-venv" / "bin" / "python").exists(), f"ViZDoom venv in {home}", "scripts/flight.sh setup payload")
        line((home / "wads" / "doom1.wad").exists(), "doom1.wad", "scripts/flight.sh setup wads")
        dep = home / "DoomSat" / "build-artifacts" / platform.system() / "DoomSat" / "bin" / "DoomSat"
        line(dep.exists(), "DoomSat F´ binary built", "scripts/flight.sh setup fprime")
        import yamcs_time_patch   # noqa: E402 (tools/, next to this file)
        t = yamcs_time_patch.state(yamcs_time_patch.find_jar())
        if t in ("patched", "unpatched"):
            line(t == "patched", "fprime-yamcs stamps F´ times as Yamcs does (TAI-UTC 37 s, not 38)",
                 "scripts/flight.sh setup fprime  (docs/plans/fprime-yamcs-time.md)")
        elif t == "unknown":
            line(None, "fprime-yamcs is a version tools/yamcs_time_patch.py does not know: check its F´ times "
                       "(docs/plans/fprime-yamcs-time.md)")

    print("== running now (not an error if you have not started them)")
    line(answers("http://localhost:8090/api/instances") or None, "Yamcs     http://localhost:8090   (scripts/flight.sh start | yamcs)")
    line(answers("http://localhost:9000/") or None, "Open MCT  http://localhost:9000   (scripts/start_openmct.sh)")
    line(answers("http://localhost:8070/") or None, "Dashboard http://localhost:8070   (python tools/serve_dashboard.py)")
    line(answers("http://localhost:5000/") or None, "F´ GDS    http://localhost:5000   (scripts/flight.sh gds)")

    if a.jev:
        print("== one jev call")
        key = env_key("TYPESAFE_API_KEY")
        if not key:
            line(False, "no TYPESAFE_API_KEY to call with", "put your key in .env")
        else:
            from providers import TypeSafeSystemOne  # noqa: E402
            import providers
            providers.SYSTEM_ONE_TIMEOUT_S = 15  # a first call is not a reflex
            try:
                r = TypeSafeSystemOne(key).ask({"door": "open", "hallway": "empty"}, {"go": {
                    "type": "choice",
                    "instructions": {"question": "Should the player walk through the door?", "inspect": "`door`, `hallway`"},
                    "criteria": {"Go": {"what": "`door` is open and `hallway` is empty"},
                                 "Wait": {"what": "`door` is closed or `hallway` is not empty"}}}})
                line(True, f"jev answered {r['answers']['go'].get('choice')} in {r['latency_ms']} ms (model {r['model']})")
            except Exception as e:  # noqa: BLE001
                line(False, f"jev call failed: {e}", "check the key (TypeSafe keys can expire) and your network")
    print(f"\n{bad} problem(s)" if bad else "\nall checked pieces are in place")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
