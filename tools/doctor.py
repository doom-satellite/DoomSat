"""Check what is installed and what is running, one line per piece, with the command that fixes it.

    python tools/doctor.py           installs, keys, and which services answer
    python tools/doctor.py --jev     also makes one real jev call with the key in .env
    python tools/doctor.py --rune    also makes one real Rune call (a local llama.cpp server, RUNE_BASE_URL) and
                                     checks the prompt the server renders for it (docs/JETSON-RUNBOOK.md)

Uses only the standard library (plus `requests` for --jev and --rune), so any Python 3.10+ can run it.
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


# One small question for the real model calls: a door and a hallway.
SAMPLE_STATE = {"door": "open", "hallway": "empty"}
SAMPLE_QUESTION = {
    "type": "choice",
    "instructions": {"question": "Should the player walk through the door?", "inspect": "`door`, `hallway`"},
    "criteria": {"Go": {"what": "`door` is open and `hallway` is empty"},
                 "Wait": {"what": "`door` is closed or `hallway` is not empty"}}}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jev", action="store_true", help="make one real jev call")
    p.add_argument("--rune", action="store_true", help="make one real Rune call and check its rendered prompt")
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

    print("== running now (not an error if you have not started them)")
    line(answers("http://localhost:8090/api/instances") or None, "Yamcs     http://localhost:8090   (scripts/flight.sh start | yamcs)")
    line(answers("http://localhost:9000/") or None, "Open MCT  http://localhost:9000   (scripts/start_openmct.sh)")
    line(answers("http://localhost:8070/") or None, "Dashboard http://localhost:8070   (python tools/serve_dashboard.py)")
    line(answers("http://localhost:5000/") or None, "F´ GDS    http://localhost:5000   (scripts/flight.sh gds)")

    # Optional local models: the Jetson setup in docs/JETSON-RUNBOOK.md. llama-server answers /health with 200
    # once its model is loaded, so a server still loading (503) or not started shows as not running.
    rune = (env_key("RUNE_BASE_URL") or "http://localhost:8080").rstrip("/")
    two = (env_key("SYSTEM_TWO_BASE_URL") or "http://localhost:8081/v1").rstrip("/")
    line(answers(rune.removesuffix("/v1") + "/health") or None, f"Rune      {rune}   (System One: --system-one rune; llama.cpp)")
    line(answers(two.removesuffix("/v1") + "/health") or None, f"Gemma 4   {two}   (System Two: --system-two openai --system-two-model gemma-4-31b)")

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
                r = TypeSafeSystemOne(key).ask(SAMPLE_STATE, {"go": SAMPLE_QUESTION})
                line(True, f"jev answered {r['answers']['go'].get('choice')} in {r['latency_ms']} ms (model {r['model']})")
            except Exception as e:  # noqa: BLE001
                line(False, f"jev call failed: {e}", "check the key (TypeSafe keys can expire) and your network")
    if a.rune:
        print("== one Rune call")
        from providers import RUNE_TEMPERATURE, RuneSystemOne  # noqa: E402
        one = RuneSystemOne(rune, None, float(env_key("RUNE_TEMPERATURE") or RUNE_TEMPERATURE), timeout=30)  # a first call is not a reflex
        try:
            r = one.ask(SAMPLE_STATE, {"go": SAMPLE_QUESTION})
            ans = r["answers"]["go"]
            probs = {k: round(v, 3) for k, v in ans["probabilities"].items()}
            line(True, f"Rune answered {ans['choice']} {probs} in {r['latency_ms']} ms (model {r['model']})")
            tail = one.rendered_prompt(SAMPLE_STATE, SAMPLE_QUESTION)[-80:]
            line("<|channel>thought\n<channel|>" in tail,
                 "the server's chat template ends the prompt with the empty thought block (thinking off)",
                 f"the rendered prompt ends {tail!r}; Rune was trained with <|channel>thought\\n<channel|> there. "
                 "Update llama.cpp (git pull, rebuild) or check the chat template")
        except Exception as e:  # noqa: BLE001
            line(False, f"Rune call failed: {e}", f"start the server (sudo systemctl start rune-server) and check RUNE_BASE_URL ({rune})")
    print(f"\n{bad} problem(s)" if bad else "\nall checked pieces are in place")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
