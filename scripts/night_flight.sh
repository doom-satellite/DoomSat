#!/bin/bash
# One flight, start to graded, from the Windows side. Charter phase 1: only flight counts.
#
#   scripts/night_flight.sh <out-name> <wad> <map> <seconds> [geometry] [extra pilot args...]
#
#   scripts/night_flight.sh flight-dev  freedoom1.wad E1M1 200 on
#   scripts/night_flight.sh flight-e1m1 doom1.wad     E1M1 200 on
#
# System Two is off: the brief's rule 8 for the night of 23 September. It is passed here rather than
# changed in pilot.py, because the default is a repo-wide choice and this is one night.
set -u
cd "$(dirname "$0")/.." || exit 1
NAME=${1:?out name}
WAD=${2:-freedoom1.wad}
MAP=${3:-E1M1}
SECONDS_TO_FLY=${4:-200}
GEOM=${5:-on}
shift 5 2>/dev/null || true

echo "== flight $NAME: $WAD $MAP, ${SECONDS_TO_FLY}s, geometry=$GEOM"
MSYS_NO_PATHCONV=1 wsl -d ros2 -u root -- /root/doom/payload-venv/bin/python \
    /mnt/c/Users/Kevin/Genai/DoomSat/research/reap.py --keep-younger-than 0
MSYS_NO_PATHCONV=1 wsl -d ros2 -u root -- env WAD="$WAD" MAP="$MAP" GEOMETRY="$GEOM" \
    bash /mnt/c/Users/Kevin/Genai/DoomSat/scripts/wsl_run_flight.sh start

echo "-- waiting for Yamcs"
for i in $(seq 1 60); do
  if MSYS_NO_PATHCONV=1 wsl -d ros2 -u root -- bash -c \
       "curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/api/instances" 2>/dev/null | grep -q 200; then
    echo "-- Yamcs up after ${i}0s"; break
  fi
  sleep 10
done

# One log per flight. The first night this script was used, two flights wrote to the same
# file and the second one's data was gone before it could be graded.
mkdir -p "out/$NAME"
PYTHONUTF8=1 ground/.venv/Scripts/python ground/pilot.py \
    --system-two none --duration "$SECONDS_TO_FLY" --level-budget 180 --out-dir "out/$NAME" "$@" \
    2>&1 | tail -30

MSYS_NO_PATHCONV=1 wsl -d ros2 -u root -- bash /mnt/c/Users/Kevin/Genai/DoomSat/scripts/wsl_run_flight.sh stop

echo "-- payload log, the lines that say how it went"
MSYS_NO_PATHCONV=1 wsl -d ros2 -u root -- bash -c \
    "grep -E 'tic rate|LEVEL FINISHED|episode .* over|freeze|ORACLE' /root/doom/run/payload.log | tail -20"

echo "-- grading"
MSYS_NO_PATHCONV=1 wsl -d ros2 -u root -- bash -c \
    "cd /mnt/c/Users/Kevin/Genai/DoomSat && DOOMSAT_HARNESS=/mnt/c/Users/Kevin/Genai/DoomSat \
     /root/doom/payload-venv/bin/python research/runner.py flight --from-log out/$NAME/decisions.jsonl \
     --wad /root/doom/wads/$WAD --maps $MAP --out research/out/$NAME --grade" 2>&1 | tail -25
