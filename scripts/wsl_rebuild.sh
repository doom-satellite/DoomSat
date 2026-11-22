#!/bin/bash
# Full regenerate + build of the F´ project (needed after settings.ini / config changes).
set -e
. "$(dirname "$0")/common.sh"
bash "$DOOMSAT_REPO/scripts/wsl_sync.sh"
cd "$PROJ"
. fprime-venv/bin/activate
rm -rf build-fprime-automatic-native build-artifacts
fprime-util generate 2>&1 | grep -i -E "error|FPRIME_CONFIG|config" | grep -v "fprime-gds has unexpected" | head -10
fprime-util build -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" 2>&1 | grep -v "^\s*$" | grep -i -E "error|FAILED|Installing: .*DoomSat/bin" | grep -v "fprime-gds has unexpected" | head -40
echo "BUILD_EXIT ${PIPESTATUS[0]}"
grep -rh "FW_COM_BUFFER_MAX_SIZE\b" build-fprime-automatic-native/config/FppConstantsAc.hpp 2>/dev/null | head -2 || true
grep -rn "FPRIME_CONFIG_DIR" build-fprime-automatic-native/CMakeCache.txt | head -2
ls -la $DEPLOY/bin/DoomSat
