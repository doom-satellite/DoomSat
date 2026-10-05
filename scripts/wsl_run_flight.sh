#!/bin/bash
# Start (or restart) the flight side: Doom payload + Yamcs (fprime-yamcs) + the DoomSat binary.
# Linux, macOS or inside WSL; on Windows call scripts/flight.sh, which forwards here.
# Usage: wsl_run_flight.sh [start|yamcs|stop|status|payload|prmdb]
#   start    payload + F´ + Yamcs
#   yamcs    F´ + Yamcs only, no game (for Yamcs / Open MCT on their own; the Doom channels stay still)
#   payload  restart only the game process (after editing the payload)
#   prmdb    only build the parameter file (flight.sh gds runs this before it starts the binary)
. "$(dirname "$0")/common.sh"
REPO=$DOOMSAT_REPO
# GEOMETRY=on   exact lines, gated on the automap having drawn them (payload/seen_geometry.py)
# ORACLE=L0|L1  the diagnostic ladder. Never on a shareware level, and never scored.
# WAD=, MAP=    which level. A dev flight is WAD=freedoom1.wad.
# DOOMSAT_RELAY=1  Yamcs's TM/TC links behind tools/lossy_relay.py (ground/yamcs/launch.py); off by default.
# DOOMSAT_PRM_DEFAULTS=1  start even if PrmDb.dat cannot be built, on the stock parameter defaults (not safe
#                  for uplinks: see build_prmdb).
# RECORDS=on    the payload writes a record of each episode to $RUN/rec for the ground's science data system to
#               downlink (payload/episode_record.py; ground/sds). Off by default.
mkdir -p "$RUN" "$REPO/out" "$WADS/uplink"   # uplink: where an uplinked WAD lands (LOAD_WAD, README)
stop() {
  local groups; groups=$(payload_groups)   # before the SIGTERM: a payload that dies on it takes its pid with it
  pkill -f "doom_payloa[d].py --fps" 2>/dev/null
  pkill -f "fprime_yamc[s]" 2>/dev/null; pkill -f "yamcs/launc[h].py" 2>/dev/null
  pkill -f "YamcsServe[r]" 2>/dev/null
  pkill -f "bin/DoomSa[t]" 2>/dev/null
  pkill -f "fprime-gd[s] " 2>/dev/null; pkill -f "fprime_gds[.]executables" 2>/dev/null   # flight.sh gds
  sleep 1
  reap_payload "$groups"
  # Yamcs takes up to ~15 s to shut down; a new one started sooner fails, and the flight software with it
  for _ in $(seq 1 40); do pgrep -f "YamcsServe[r]" >/dev/null || break; sleep 0.5; done
}
# The payload closes its game on SIGTERM, but one stuck inside ViZDoom never gets to run that handler (the
# engine holds the GIL): kill it outright rather than leave it holding port 4242.
kill_hung_payload() { pkill -KILL -f "doom_payloa[d].py --fps" 2>/dev/null; }
# The engine ViZDoom spawns ignores SIGTERM (research/reap.py), so killing a hung payload by name, or a payload
# dying without closing its game, leaves the engine running. detach's setsid makes each payload the leader of
# its own process group, and the engine is in that group: these are the groups to reap. A group is only taken
# when its leader is a payload; on macOS (nohup, no leader) there are none and kill_hung_payload is all there is.
payload_groups() {
  local pid
  for pid in $(pgrep -f "doom_payloa[d].py --fps"); do
    [ "$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" = "$pid" ] && echo "$pid"
  done
}
# After the SIGTERM: up to 4 s for the payload to close its game, then whatever is left of each group that is
# a payload or an engine, then any payload outside a group.
reap_payload() {
  local group
  for _ in $(seq 1 8); do pgrep -f "doom_payloa[d].py --fps" >/dev/null || break; sleep 0.5; done
  for group in $1; do pkill -KILL -g "$group" -f "doom_payloa[d].py --fps|/vizdoom/vizdoo[m]" 2>/dev/null; done
  kill_hung_payload
}
# Detached, so it outlives this shell. setsid -f in WSL: anything started with plain nohup inside a
# `wsl bash -c` call dies when that call returns. macOS has no setsid; nohup is enough there.
detach() {
  if command -v setsid >/dev/null; then setsid -f bash -c "$1" < /dev/null
  else nohup bash -c "$1" < /dev/null > /dev/null 2>&1 & fi
}
start_payload() {
  # --skill must match research/levels.yaml run.skill, or the bench and the flight stack are playing
  # different games and their numbers cannot be compared. tests/test_runner.py pins the two together.
  cd "$PROJ" || exit 1
  detach "exec '$PAYLOAD_PY' '$REPO'/payload/doom_payload.py --fps ${FPS:-10} --quality ${QUALITY:-45} --skill ${SKILL:-3} --wad ${WAD:-doom1.wad} --map ${MAP:-E1M1} --geometry ${GEOMETRY:-off} --oracle ${ORACLE:-off} --records ${RECORDS:-off} --map-png '$REPO/out/payload_map.png' > '$RUN/payload.log' 2>&1"
}
# Parameters the flight software loads at boot: $RUN/PrmDb.dat (DoomSatTopology.cpp builds the same path from
# DOOMSAT_HOME). Built from flight/config/PrmDb.json before every start, so the repo is what holds; a PRM_SAVE on
# board lasts until then. @UPLINK@ is this machine's uplink directory: cfdpManager's temp directory lives under
# it, not in /tmp. Without the file the binary boots on the stock defaults (a 992-byte data cap the 1001-byte
# PDU limit cannot always hold, a 64 KiB/s CRC pass, temp files in /tmp), so a failed build stops the start
# unless DOOMSAT_PRM_DEFAULTS=1 says that is wanted.
build_prmdb() {
  cd "$PROJ" || exit 1
  rm -f "$DEPLOY/bin/PrmDb.dat"   # where earlier versions put it: a second file in bin/ breaks fprime-gds's find_app
  if ! { fprime-venv/bin/python "$REPO/tools/prmdb.py" "$REPO/flight/config/PrmDb.json" "$WADS/uplink" \
           > "$RUN/PrmDb.json" 2> "$RUN/prmdb.log" &&
         fprime-venv/bin/fprime-prm-write dat "$RUN/PrmDb.json" -d "$DEPLOY/dict/DoomSatTopologyDictionary.json" \
           -o "$RUN/PrmDb.dat" >> "$RUN/prmdb.log" 2>&1; }; then
    rm -f "$RUN/PrmDb.dat"   # never fly on the last start's file by mistake
    case "${DOOMSAT_PRM_DEFAULTS:-}" in
      1|true|yes|on) echo "PrmDb.dat not built (see $RUN/prmdb.log); flying on parameter defaults (DOOMSAT_PRM_DEFAULTS)" >&2 ;;
      *) echo "PrmDb.dat not built (see $RUN/prmdb.log); not starting. DOOMSAT_PRM_DEFAULTS=1 starts on the stock defaults." >&2
         exit 1 ;;
    esac
  fi
}
start_yamcs() {
  cd "$PROJ" || exit 1
  # cfdpManager's temp files: a receive that ends before its metadata arrives leaves one behind, and F´ never
  # removes it. Nothing on board is running here (stop came first, and CFDP keeps no state across a boot).
  mkdir -p "$WADS/uplink/.cfdp-tmp"
  rm -f "$WADS/uplink/.cfdp-tmp/"*.tmp
  detach "cd '$PROJ' && . fprime-venv/bin/activate && python '$REPO/ground/yamcs/launch.py' --deployment $DEPLOY --app $DEPLOY/bin/DoomSat --skip-browser-open --yamcs-config-dir '$REPO/ground/yamcs' --yamcs-data-dir '$RUN/yamcs-data' --yamcs-realtime-only-channels DoomSat.doom.FRAME_CHUNK > '$RUN/yamcs.log' 2>&1"
}
case "${1:-start}" in
  stop) stop; echo stopped ;;
  payload)
    groups=$(payload_groups)
    pkill -f "doom_payloa[d].py --fps" 2>/dev/null; reap_payload "$groups"
    start_payload; echo "payload restarted" ;;
  status)
    ps aux | grep -E "doom_payloa[d]|fprime_yamc[s]|YamcsServe[r]|bin/DoomSa[t]" | awk '{print $11, $12, $13}' | sort | uniq -c
    curl -s http://localhost:8090/api/instances | grep -c '"name": "fprime-project"' ;;
  prmdb)
    build_prmdb
    if [ -f "$RUN/PrmDb.dat" ]; then echo "built $RUN/PrmDb.dat"; else echo "no PrmDb.dat: on the stock parameter defaults"; fi ;;
  start) stop; build_prmdb; start_payload; start_yamcs; echo "started; logs in $RUN; Yamcs on http://localhost:8090 in ~30 s" ;;
  yamcs) stop; build_prmdb; start_yamcs; echo "F´ + Yamcs started (no game); logs in $RUN; Yamcs on http://localhost:8090 in ~30 s" ;;
  *) echo "usage: $0 [start|yamcs|stop|status|payload|prmdb]"; exit 2 ;;
esac
