#!/bin/bash
# The flight side from one command. Linux, macOS, inside WSL, or from Git Bash on Windows (it forwards
# into the WSL distro named by DOOMSAT_WSL_DISTRO, as DOOMSAT_WSL_USER; both can go in .env).
#
#   scripts/flight.sh setup [payload|fprime|wads]   install (scripts/setup_flight.sh)
#   scripts/flight.sh start                         payload + F´ + Yamcs (:8090)
#   scripts/flight.sh yamcs                         F´ + Yamcs, no game
#   scripts/flight.sh payload                       restart only the game
#   scripts/flight.sh stop | status | check         stop everything / processes / telemetry health
#   scripts/flight.sh build | rebuild               after editing flight/ (incremental / full)
#   scripts/flight.sh ut                            the flight components' unit tests (GTest; CfdpGuard and Doom)
#   scripts/flight.sh gds                           F´ on its own with the stock F´ GDS (:5000), no Yamcs
#
# WAD=, MAP=, GEOMETRY=, ORACLE=, FPS=, QUALITY=, SKILL=, RECORDS= pass through to the payload.
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    PASS=()
    [ -n "${DOOMSAT_HOME:-}" ] && PASS+=("DOOMSAT_HOME=$DOOMSAT_HOME")   # a WSL path, if set by hand
    . "$HERE/common.sh"
    for v in WAD MAP GEOMETRY ORACLE FPS QUALITY SKILL RECORDS DOOMSAT_RELAY DOOMSAT_RELAY_TM_PORT DOOMSAT_RELAY_TC_PORT DOOMSAT_PRM_DEFAULTS; do [ -n "${!v:-}" ] && PASS+=("$v=${!v}"); done
    WSL=(-d "${DOOMSAT_WSL_DISTRO:-Ubuntu}")
    [ -n "${DOOMSAT_WSL_USER:-}" ] && WSL+=(-u "$DOOMSAT_WSL_USER")
    REPO_WIN="$(cd "$HERE/.." && pwd -W)"
    exec env MSYS_NO_PATHCONV=1 wsl "${WSL[@]}" --exec \
      env "${PASS[@]}" bash -c 'cd "$(wslpath "$0")" && exec bash scripts/flight.sh "$@"' "$REPO_WIN" "$@" ;;
esac

case "${1:-start}" in
  setup)   shift; exec bash "$HERE/setup_flight.sh" "$@" ;;
  build)   exec bash "$HERE/wsl_build.sh" ;;
  rebuild) exec bash "$HERE/wsl_rebuild.sh" ;;
  ut)      exec bash "$HERE/wsl_ut.sh" ;;
  check)   exec bash "$HERE/wsl_check.sh" ;;
  gds)
    . "$HERE/common.sh"
    bash "$HERE/wsl_run_flight.sh" stop >/dev/null
    bash "$HERE/wsl_run_flight.sh" prmdb >/dev/null || exit 1
    cd "$PROJ" && . fprime-venv/bin/activate && exec fprime-gds -d "$DEPLOY" --app "$DEPLOY/bin/DoomSat" --gui-addr 0.0.0.0 ;;
  *)       exec bash "$HERE/wsl_run_flight.sh" "${1:-start}" ;;
esac
