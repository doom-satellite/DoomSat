#!/bin/bash
# Install the flight side: ViZDoom (the payload), the F´ project with the DoomSat deployment, fprime-yamcs
# (which brings Yamcs 5.12.8 and its own Java) and the WADs. Linux, macOS, or inside WSL on Windows.
# Safe to re-run: every step skips what is already there.
#
#   scripts/setup_flight.sh            everything
#   scripts/setup_flight.sh payload    ViZDoom venv only       (Doom on its own)
#   scripts/setup_flight.sh fprime     F´ + fprime-yamcs + build (F´ and Yamcs on their own)
#   scripts/setup_flight.sh wads       shareware doom1.wad + Freedoom
#
# Installs into $DOOMSAT_HOME (default ~/doom). Needs python3 (3.10+) with venv, git, a C++ compiler,
# curl, and `ar` (binutils) for the shareware WAD. See the README for the per-OS package commands.
set -e
. "$(dirname "$0")/common.sh"
STEP="${1:-all}"
mkdir -p "$DOOMSAT_HOME" "$WADS" "$RUN"
say() { printf '\n== %s\n' "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
need python3 "python 3.10+"; need git git; need curl curl
python3 -c 'import venv, ensurepip' 2>/dev/null || { echo "missing: python3 venv (Ubuntu: sudo apt install python3-venv)"; exit 1; }

payload() {
  say "payload: ViZDoom 1.3.0 in $DOOMSAT_HOME/payload-venv"
  [ -x "$PAYLOAD_PY" ] || python3 -m venv "$DOOMSAT_HOME/payload-venv"
  "$PAYLOAD_PY" -m pip install -q --upgrade pip
  "$PAYLOAD_PY" -m pip install -q vizdoom==1.3.0 numpy pillow pyyaml requests   # requests: the bench asks jev
  "$PAYLOAD_PY" -c 'import vizdoom; print("vizdoom", vizdoom.__version__)'
}

fprime() {
  need c++ "a C++ compiler: Ubuntu build-essential, macOS xcode-select --install"
  if [ ! -d "$PROJ/lib/fprime" ]; then
    say "F´ v4.3.0 project in $PROJ"
    python3 -m venv "$DOOMSAT_HOME/bootstrap-venv"
    "$DOOMSAT_HOME/bootstrap-venv/bin/pip" install -q fprime-bootstrap==1.6.0
    printf "DoomSat\nDoomMission\n" | "$DOOMSAT_HOME/bootstrap-venv/bin/fprime-bootstrap" project --path "$DOOMSAT_HOME" --tag v4.3.0
  fi
  cd "$PROJ"
  . fprime-venv/bin/activate
  say "fprime-yamcs 0.2.1 (Yamcs 5.12.8 + JRE) and fprime-xtce with the !binary annotation (PR #8)"
  pip install -q fprime-yamcs==0.2.1
  # it stamps F´ packets 1 s in the future (38 s where Yamcs adds TAI-UTC, 37): patch that one constant
  python "$DOOMSAT_REPO/tools/yamcs_time_patch.py" || say "fprime-yamcs not patched: F´ times in Yamcs stay 1 s ahead"
  pip install -q "fprime-xtce @ git+https://github.com/FarkasJoseph/fprime-xtce@feature/binary-annotation-combined"
  if [ ! -d "$PROJ/DoomSat" ]; then
    say "the DoomSat deployment"
    printf "DoomSat\n\n1\nyes\n" | fprime-util new --deployment
  fi
  say "build (a first build takes a few minutes)"
  bash "$DOOMSAT_REPO/scripts/wsl_rebuild.sh"
}

wads() {
  say "WADs in $WADS"
  [ -f "$WADS/doom1.wad" ] || bash "$DOOMSAT_REPO/tools/get_doom1.sh"
  bash "$DOOMSAT_REPO/tools/get_freedoom.sh" "$WADS"
}

case "$STEP" in
  payload) payload ;;
  fprime)  fprime ;;
  wads)    wads ;;
  all)     payload; wads; fprime ;;
  *) echo "usage: $0 [all|payload|fprime|wads]"; exit 2 ;;
esac
say "done ($STEP). Next: scripts/flight.sh start"
