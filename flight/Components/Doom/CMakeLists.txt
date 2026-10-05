register_fprime_library(
    AUTOCODER_INPUTS
        "${CMAKE_CURRENT_LIST_DIR}/Doom.fpp"
    SOURCES
        "${CMAKE_CURRENT_LIST_DIR}/Doom.cpp"
    DEPENDS
        CFDP_Checksum
)

# GTest unit test (scripts/flight.sh ut): COMMIT_WAD, fileAnnounce and the WAD reports. Built only in a unit-test
# build (fprime-util generate --ut).
register_fprime_ut(
    AUTOCODER_INPUTS
        "${CMAKE_CURRENT_LIST_DIR}/Doom.fpp"
    SOURCES
        "${CMAKE_CURRENT_LIST_DIR}/test/ut/DoomTestMain.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/test/ut/DoomTester.cpp"
    UT_AUTO_HELPERS
)
