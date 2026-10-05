#!/bin/bash
# The science data system (ground/sds/) from one command: Apache Airflow 3 and the frame capture service.
# Linux, macOS, or inside WSL; from Git Bash on Windows it forwards into WSL the way flight.sh does.
#
#   scripts/sds.sh setup             Airflow 3.3.2 in $DOOMSAT_HOME/sds/venv (uv + the official constraints)
#   scripts/sds.sh start             api-server (127.0.0.1:8080), scheduler, dag-processor, triggerer, capture
#   scripts/sds.sh stop | status
#   scripts/sds.sh airflow ARGS...   the Airflow CLI with the SDS environment (e.g. dags list-runs sds_forward)
#   scripts/sds.sh catalog ARGS...   the product catalog (python -m doomsat_sds.catalog --help)
#
# DOOMSAT_SDS_RECORDS=on scripts/sds.sh start  also requests each episode's record from the payload with
# cfdpManager's SendFile and reads it from Yamcs's bucket cfdpDown once the CFDP downlink has finished (Phase D; the
# flight must run with RECORDS=on). Off by default, and then the SDS sends no commands at all.
#
# Everything it creates lives under $DOOMSAT_HOME/sds (the venv, Airflow's home and SQLite database, the
# catalog, the products, captured frames, logs); nothing goes in the repo. It runs next to scripts/flight.sh
# and never starts, stops or commands the flight side: with the SDS stopped, a flight is exactly as before.
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    PASS=()
    [ -n "${DOOMSAT_HOME:-}" ] && PASS+=("DOOMSAT_HOME=$DOOMSAT_HOME")   # a WSL path, if set by hand
    . "$HERE/common.sh"
    for v in SDS_PORT DOOMSAT_YAMCS DOOMSAT_SDS_RECORDS; do [ -n "${!v:-}" ] && PASS+=("$v=${!v}"); done
    WSL=(-d "${DOOMSAT_WSL_DISTRO:-Ubuntu}")
    [ -n "${DOOMSAT_WSL_USER:-}" ] && WSL+=(-u "$DOOMSAT_WSL_USER")
    REPO_WIN="$(cd "$HERE/.." && pwd -W)"
    exec env MSYS_NO_PATHCONV=1 wsl "${WSL[@]}" --exec \
      env "${PASS[@]}" bash -c 'cd "$(wslpath "$0")" && exec bash scripts/sds.sh "$@"' "$REPO_WIN" "$@" ;;
esac
. "$HERE/common.sh"

AIRFLOW_VERSION=3.3.2
YAMCS_CLIENT_VERSION=2.1.0          # the version ground/.venv uses (ground/requirements.txt)
PILLOW_VERSION=12.3.0              # not in the Airflow constraints, so pinned here
SDS_HOME="$DOOMSAT_HOME/sds"
VENV="$SDS_HOME/venv"
SDS_PORT="${SDS_PORT:-8080}"
COMPONENTS="api-server scheduler dag-processor triggerer"

# One environment for every process, so the CLI, the four components and the task processes agree.
sds_env() {
  export AIRFLOW_HOME="$SDS_HOME/airflow"
  export AIRFLOW__CORE__DAGS_FOLDER="$DOOMSAT_REPO/ground/sds/dags"
  export AIRFLOW__CORE__LOAD_EXAMPLES=False
  export AIRFLOW__CORE__EXECUTOR=LocalExecutor
  export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///$SDS_HOME/airflow/airflow.db"
  # LocalExecutor forks `parallelism` workers up front; the default 32 is 1.4 GB on a box that also runs Yamcs.
  export AIRFLOW__CORE__PARALLELISM=4
  export AIRFLOW__CORE__MAX_ACTIVE_TASKS_PER_DAG=4
  export AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=False
  export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=30
  export AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL=30
  export AIRFLOW__DAG_PROCESSOR__PARSING_PROCESSES=1
  # Local only, one person: no login, and nothing listens beyond this machine.
  export AIRFLOW__API__HOST=127.0.0.1
  export AIRFLOW__API__PORT="$SDS_PORT"
  export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True
  # Tasks report back here; Airflow assumes :8080 whatever [api] port says.
  export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:$SDS_PORT/execution/"
  export PYTHONPATH="$DOOMSAT_REPO/ground/sds${PYTHONPATH:+:$PYTHONPATH}"
  export DOOMSAT_SDS_HOME="$SDS_HOME"
  export DOOMSAT_YAMCS="${DOOMSAT_YAMCS:-localhost:8090}"
  export PYTHON_YAMCS_CLIENT_UTC=1
  # Phase D: ask the payload for its episode records with SendFile (the SDS's only command). Off by default, and
  # only for episodes whose payload ran with --records on (the episode's context says so). The wait for the file
  # reads Yamcs's CFDP transfer list, so it needs no Airflow connection.
  export DOOMSAT_SDS_RECORDS="${DOOMSAT_SDS_RECORDS:-off}"
}

say() { printf '\n== %s\n' "$*"; }
installed() { [ -x "$VENV/bin/airflow" ] || { echo "the SDS is not installed: scripts/sds.sh setup"; exit 1; }; }

setup() {
  command -v uv >/dev/null 2>&1 || {
    echo "missing: uv (https://docs.astral.sh/uv/: curl -LsSf https://astral.sh/uv/install.sh | sh)"; exit 1; }
  PY=python3
  PYV="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python $PYV: Airflow 3 needs 3.10+"; exit 1; }
  CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-$AIRFLOW_VERSION/constraints-$PYV.txt"
  mkdir -p "$SDS_HOME"
  say "Airflow $AIRFLOW_VERSION (Python $PYV) in $VENV"
  [ -x "$VENV/bin/python" ] || uv venv -q --python "$PY" "$VENV" || { echo "uv venv failed"; exit 1; }
  # Airflow and its providers come from the constraints file; yamcs-client and Pillow are not in it and are pinned here.
  uv pip install -q --python "$VENV/bin/python" --constraint "$CONSTRAINTS" \
    "apache-airflow==$AIRFLOW_VERSION" "yamcs-client==$YAMCS_CLIENT_VERSION" "pillow==$PILLOW_VERSION" requests \
    || { echo "install failed (is $CONSTRAINTS reachable?)"; exit 1; }
  sds_env
  mkdir -p "$AIRFLOW_HOME" "$SDS_HOME/logs" "$SDS_HOME/run"
  say "Airflow database ($AIRFLOW_HOME/airflow.db)"
  "$VENV/bin/airflow" db migrate > "$SDS_HOME/logs/migrate.log" 2>&1 || { tail -20 "$SDS_HOME/logs/migrate.log"; exit 1; }
  "$VENV/bin/python" -c 'import airflow, doomsat_sds, yamcs.client, PIL; print("airflow", airflow.__version__, "| doomsat_sds", doomsat_sds.__version__)' \
    || { echo "the venv is incomplete: run scripts/sds.sh setup again"; exit 1; }
  say "done. Next: scripts/sds.sh start (next to scripts/flight.sh start)"
}

# Detached, so it outlives this shell (the same reason as wsl_run_flight.sh). With setsid the component is the
# leader of its own session, and everything it starts stays in that session, including Airflow's task processes,
# which move to process groups of their own. The pid file holds the leader's pid, and a second file its start time,
# so a pid left over from a paused or rebooted machine is never mistaken for an SDS process.
detach() {  # name command
  local pidfile="$SDS_HOME/run/$1.pid"
  rm -f "$pidfile" "$SDS_HOME/run/$1.started"
  if command -v setsid >/dev/null; then
    setsid -f bash -c "echo \$\$ > '$pidfile'; exec $2" < /dev/null > /dev/null 2>&1
  else
    nohup bash -c "echo \$\$ > '$pidfile'; exec $2" < /dev/null > /dev/null 2>&1 &
  fi
  for _ in $(seq 50); do [ -s "$pidfile" ] && break; sleep 0.1; done
  [ -s "$pidfile" ] && ps -o lstart= -p "$(cat "$pidfile")" > "$SDS_HOME/run/$1.started" 2>/dev/null
}

# The component's pid, only if that process is still the one this script started.
sds_pid() {  # name
  local pid started
  pid="$(cat "$SDS_HOME/run/$1.pid" 2>/dev/null)" || return 1
  started="$(cat "$SDS_HOME/run/$1.started" 2>/dev/null)" || return 1
  [ -n "$pid" ] && [ -n "$started" ] && [ "$(ps -o lstart= -p "$pid" 2>/dev/null)" = "$started" ] && echo "$pid"
}

alive() { sds_pid "$1" >/dev/null; }

# Is this pid the leader of its own session (setsid), so the whole session can be signalled?
leader() { [ "$(ps -o sid= -p "$1" 2>/dev/null | tr -d ' ')" = "$1" ]; }

stop_one() {  # name seconds
  local pid session=
  pid="$(sds_pid "$1")" || { rm -f "$SDS_HOME/run/$1.pid" "$SDS_HOME/run/$1.started"; return 0; }
  leader "$pid" && session=1       # decided once: the session outlives its leader, and so may its tasks
  if [ -n "$session" ]; then pkill -TERM -s "$pid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
  for _ in $(seq $(( $2 * 2 ))); do
    if [ -n "$session" ]; then pgrep -s "$pid" >/dev/null || break; else kill -0 "$pid" 2>/dev/null || break; fi
    sleep 0.5
  done
  if [ -n "$session" ]; then pkill -KILL -s "$pid" 2>/dev/null; else kill -KILL "$pid" 2>/dev/null; fi
  rm -f "$SDS_HOME/run/$1.pid" "$SDS_HOME/run/$1.started"
}

# The scheduler and triggerer first, while the API server is still up, so running tasks can finish and report;
# the API server last. Only processes this script started are signalled: nothing is matched by name.
stop() {
  stop_one scheduler 90
  stop_one triggerer 30
  stop_one dag-processor 15
  stop_one capture 15
  stop_one api-server 15
}

start() {
  installed; sds_env
  stop
  mkdir -p "$SDS_HOME/logs" "$SDS_HOME/run"
  "$VENV/bin/airflow" db migrate > "$SDS_HOME/logs/migrate.log" 2>&1 || { tail -20 "$SDS_HOME/logs/migrate.log"; exit 1; }
  for c in $COMPONENTS; do
    # --skip-serve-logs: the log servers would listen on every interface; one machine shares the log files anyway
    case "$c" in scheduler|triggerer) extra=--skip-serve-logs ;; *) extra= ;; esac
    detach "$c" "'$VENV/bin/airflow' $c $extra > '$SDS_HOME/logs/$c.log' 2>&1"
  done
  detach capture "'$VENV/bin/python' -u -m doomsat_sds.capture > '$SDS_HOME/logs/capture.log' 2>&1"
  for _ in $(seq 60); do
    curl -sf "http://127.0.0.1:$SDS_PORT/api/v2/monitor/health" >/dev/null 2>&1 && break
    sleep 1
  done
  status
  echo "Airflow UI and API: http://127.0.0.1:$SDS_PORT   logs: $SDS_HOME/logs   products: $SDS_HOME/products"
}

status() {
  installed; sds_env
  for name in $COMPONENTS capture; do
    if alive "$name"; then printf '[ ok ] %-14s pid %s\n' "$name" "$(sds_pid "$name")"
    else printf '[ -- ] %-14s not running\n' "$name"; fi
  done
  health="$(curl -sf "http://127.0.0.1:$SDS_PORT/api/v2/monitor/health" 2>/dev/null)"
  if [ -n "$health" ]; then
    printf '%s' "$health" | "$VENV/bin/python" -c '
import json, sys
h = json.load(sys.stdin)
print("health:", ", ".join("%s %s" % (k, (v or {}).get("status")) for k, v in sorted(h.items())))'
  else
    echo "health: the API server is not answering on :$SDS_PORT"
  fi
  "$VENV/bin/python" -m doomsat_sds.catalog summary 2>/dev/null || true
  [ -f "$SDS_HOME/capture/stats.jsonl" ] && echo "capture, last minute: $(tail -1 "$SDS_HOME/capture/stats.jsonl")"
  return 0
}

case "${1:-status}" in
  setup)   setup ;;
  start)   start ;;
  stop)    installed; stop; echo stopped ;;
  status)  status ;;
  airflow) installed; sds_env; shift; exec "$VENV/bin/airflow" "$@" ;;
  catalog) installed; sds_env; shift; exec "$VENV/bin/python" -m doomsat_sds.catalog "$@" ;;
  *) echo "usage: $0 [setup|start|stop|status|airflow ARGS|catalog ARGS]"; exit 2 ;;
esac
