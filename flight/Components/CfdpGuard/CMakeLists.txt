register_fprime_library(
    AUTOCODER_INPUTS
        "${CMAKE_CURRENT_LIST_DIR}/CfdpGuard.fpp"
    SOURCES
        "${CMAKE_CURRENT_LIST_DIR}/CfdpGuard.cpp"
    DEPENDS
        Svc_Ccsds_CfdpManager_Types
)

# GTest unit test (scripts/flight.sh ut). Built only in a unit-test build (fprime-util generate --ut).
register_fprime_ut(
    AUTOCODER_INPUTS
        "${CMAKE_CURRENT_LIST_DIR}/CfdpGuard.fpp"
    SOURCES
        "${CMAKE_CURRENT_LIST_DIR}/test/ut/CfdpGuardTestMain.cpp"
        "${CMAKE_CURRENT_LIST_DIR}/test/ut/CfdpGuardTester.cpp"
    UT_AUTO_HELPERS
)
