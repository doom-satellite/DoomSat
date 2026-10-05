register_fprime_library(
    AUTOCODER_INPUTS
        "${CMAKE_CURRENT_LIST_DIR}/Doom.fpp"
    SOURCES
        "${CMAKE_CURRENT_LIST_DIR}/Doom.cpp"
    DEPENDS
        CFDP_Checksum
)
