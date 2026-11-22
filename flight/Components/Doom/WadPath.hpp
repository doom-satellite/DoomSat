// ======================================================================
// \title  WadPath.hpp
// \brief  Where uplinked WADs land on board and what their names look like, shared by the Doom component
//         (COMMIT_WAD, fileAnnounce) and CfdpGuard (which uploads it lets through), so the two cannot disagree.
//
// An uplinked WAD arrives as $DOOMSAT_HOME/wads/uplink/NAME.wad[.<nonce>].part and becomes NAME.wad in the
// same directory once it is known whole. $DOOMSAT_HOME defaults to $HOME/doom, as scripts/common.sh sets it.
// ======================================================================

#ifndef DoomMission_WadPath_HPP
#define DoomMission_WadPath_HPP

#include <Fw/FPrimeBasicTypes.hpp>
#include <Fw/Types/String.hpp>

#include <cstdlib>
#include <cstring>

namespace DoomMission {
namespace WadPath {

//! The uplink directory, with no trailing '/'
inline void uplinkDir(Fw::String& out) {
    const char* const home = std::getenv("DOOMSAT_HOME");
    const char* const user = std::getenv("HOME");
    if (home != nullptr && home[0] != '\0') {
        FwSizeType len = static_cast<FwSizeType>(std::strlen(home));
        while (len > 1 && home[len - 1] == '/') {
            len--;
        }
        out.format("%.*s/wads/uplink", static_cast<int>(len), home);
    } else {
        out.format("%s/doom/wads/uplink", (user != nullptr) ? user : "");
    }
}

//! NAME.wad.<nonce>.part (or NAME.wad.part): the length of NAME.wad's path, or 0 for any other file. The nonce is
//! the last dot-free segment, so a NAME that itself contains ".wad." survives.
inline FwSizeType destLength(const char* path, FwSizeType len) {
    constexpr FwSizeType PART_LEN = 5;  // ".part"
    if (len <= PART_LEN || std::strcmp(path + len - PART_LEN, ".part") != 0) {
        return 0;
    }
    const char* const slash = std::strrchr(path, '/');
    const FwSizeType base = (slash != nullptr) ? static_cast<FwSizeType>(slash + 1 - path) : 0;
    auto isWad = [&](FwSizeType end) { return end >= base + 5 && std::strncmp(path + end - 4, ".wad", 4) == 0; };
    FwSizeType end = len - PART_LEN;
    if (!isWad(end)) {
        while (end > base && path[end - 1] != '.') {
            end--;
        }
        if (end == base) {
            return 0;
        }
        end--;  // the '.' before the nonce
    }
    return isWad(end) ? end : 0;
}

//! A file directly in the uplink directory whose name uses only the payload's characters (letters, digits and
//! _ . + -) and has the NAME.wad[.<nonce>].part shape: the only uploads that may land on board.
inline bool isUplinkPart(const char* path) {
    Fw::String dir;
    uplinkDir(dir);
    const FwSizeType dirLen = dir.length();
    const FwSizeType len = static_cast<FwSizeType>(std::strlen(path));
    if (len <= dirLen + 1 || std::strncmp(path, dir.toChar(), dirLen) != 0 || path[dirLen] != '/') {
        return false;
    }
    for (const char* c = path + dirLen + 1; *c != '\0'; c++) {
        const bool ok = (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9') ||
                        *c == '_' || *c == '.' || *c == '+' || *c == '-';
        if (!ok) {
            return false;  // '/' included: nothing below the uplink directory
        }
    }
    return destLength(path, len) > 0;
}

}  // namespace WadPath
}  // namespace DoomMission

#endif
