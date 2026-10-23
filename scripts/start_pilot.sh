#!/bin/bash
# Run the ground pilot (jev plays; --system-one code or manual flies without it, see scripts/play.sh).
# Extra arguments are passed to pilot.py (see --help).
cd "$(dirname "$0")/../ground" || exit 1
export PYTHONUTF8=1   # Sonnet writes arrows and dashes; the Windows console codepage cannot print them
PY=.venv/bin/python; [ -x "$PY" ] || PY=.venv/Scripts/python
exec "$PY" pilot.py "$@"
