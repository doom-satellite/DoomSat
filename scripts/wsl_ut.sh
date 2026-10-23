#!/bin/bash
# The flight components' unit tests (GTest), in a unit-test build of the F´ project beside the flight build.
# Linux, macOS or inside WSL; on Windows call scripts/flight.sh ut, which forwards here.
# The first run configures the unit-test build (about a minute and a half); later runs take seconds.
set -e
. "$(dirname "$0")/common.sh"
bash "$DOOMSAT_REPO/scripts/wsl_sync.sh"
cd "$PROJ"
. fprime-venv/bin/activate
UT=build-fprime-automatic-native-ut
# fprime-util check reads its list of targets before it refreshes the cache, so a unit test registered since the
# last configure would not be built on this run: refresh it in place first (quick when nothing changed). A second
# `fprime-util generate --ut` would only refuse, because the build directory exists.
if [ ! -f "$UT/CMakeCache.txt" ]; then
  fprime-util generate --ut 2>&1 | grep -v "fprime-gds has unexpected" | tail -3
else
  cmake --build "$UT" --target refresh_cache 2>&1 | tail -1
fi
rc=0
for c in CfdpGuard Doom; do
  (cd "DoomMission/Components/$c" &&
   fprime-util check -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" 2>&1 | grep -v "fprime-gds has unexpected"
   exit "${PIPESTATUS[0]}") || rc=1
done
exit $rc
