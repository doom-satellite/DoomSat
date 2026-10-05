#!/bin/bash
# Copy the flight-side sources from the Windows repo into the F´ project in WSL.
set -e
. "$(dirname "$0")/common.sh"
SRC=$DOOMSAT_REPO/flight
DST=$PROJ
mkdir -p $DST/DoomMission/Components/Doom $DST/DoomMission/config
cp $SRC/Components/Doom/Doom.fpp $SRC/Components/Doom/Doom.hpp $SRC/Components/Doom/Doom.cpp $SRC/Components/Doom/CMakeLists.txt $DST/DoomMission/Components/Doom/
# The topology header goes too: the one fprime-util new wrote fits only the topology it was made with, and
# the other branch's sync (feature/wad-uplink has FileHandling, this one has CFDP) replaces it.
cp $SRC/DoomSat/Top/topology.fpp $SRC/DoomSat/Top/instances.fpp $SRC/DoomSat/Top/DoomSatTopology.cpp $SRC/DoomSat/Top/DoomSatTopologyDefs.hpp $DST/DoomSat/Top/
cp $SRC/config/FpConstants.fpp $SRC/config/CfdpCfg.fpp $SRC/config/CfdpCfg.hpp $SRC/config/CMakeLists.txt $DST/DoomMission/config/
grep -q "/Doom/" $DST/DoomMission/Components/CMakeLists.txt || echo 'add_fprime_subdirectory("${CMAKE_CURRENT_LIST_DIR}/Doom/")' >> $DST/DoomMission/Components/CMakeLists.txt
# An `if`, not `grep || { ...; } > .cm && mv`: that parses as `(grep || ...) && mv`, so every sync after the
# first ran the mv on a file it never wrote and set -e stopped the build there.
if ! grep -q "/config" $DST/DoomMission/CMakeLists.txt; then
  { echo 'add_fprime_subdirectory("${CMAKE_CURRENT_LIST_DIR}/config/")'; cat $DST/DoomMission/CMakeLists.txt; } > $DST/.cm && mv $DST/.cm $DST/DoomMission/CMakeLists.txt
fi
# Rate group rename (20 Hz base clock) must also apply to the health ping entries
sed -i.bak 's/rateGroup_0_5Hz/rateGroup_20Hz/g' $DST/DoomSat/Top/DoomSatTopologyDefs.hpp && rm -f $DST/DoomSat/Top/DoomSatTopologyDefs.hpp.bak
# Undo the earlier (wrong) whole-directory config override attempt if present
sed -i.bak '/^config_directory/d' $DST/settings.ini && rm -f $DST/settings.ini.bak
rm -rf $DST/config
echo SYNCED
