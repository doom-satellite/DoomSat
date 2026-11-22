#!/bin/bash
# Sync the flight sources and build the DoomSat deployment (incremental). Linux, macOS or inside WSL.
set -e
. "$(dirname "$0")/common.sh"
bash "$DOOMSAT_REPO/scripts/wsl_sync.sh"
cd "$PROJ"
. fprime-venv/bin/activate
fprime-util build -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" 2>&1 | grep -v "^\s*$" | grep -i -E "error|warning: unused|FAILED|Doom\.cpp|BUILD_DONE|Installing: .*DoomSat/bin" | grep -v "fprime-gds has unexpected" | head -60
echo "BUILD_EXIT ${PIPESTATUS[0]}"
ls -la $DEPLOY/bin/DoomSat $DEPLOY/dict/DoomSatTopologyDictionary.json
