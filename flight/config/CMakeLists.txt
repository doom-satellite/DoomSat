# DoomMission configuration overrides: only the files listed here replace their framework defaults
# (lib/fprime/default/config). Everything else stays stock.
register_fprime_config(
    CONFIGURATION_OVERRIDES
        "${CMAKE_CURRENT_LIST_DIR}/FpConstants.fpp"
        "${CMAKE_CURRENT_LIST_DIR}/CfdpCfg.fpp"
        "${CMAKE_CURRENT_LIST_DIR}/CfdpCfg.hpp"
    INTERFACE
)
