// ======================================================================
// \title  Doom.cpp
// \brief  Payload interface implementation (see Doom.hpp)
// ======================================================================

#include "DoomMission/Components/Doom/Doom.hpp"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include "CFDP/Checksum/Checksum.hpp"
#include "Fw/Com/ComPacket.hpp"
#include "Fw/Types/String.hpp"
#include "Os/File.hpp"
#include "Os/FileSystem.hpp"

namespace DoomMission {

// ----------------------------------------------------------------------
// Big-endian field readers for the payload protocol
// ----------------------------------------------------------------------
namespace {
U16 rdU16(const U8*& p) { U16 v = static_cast<U16>((p[0] << 8) | p[1]); p += 2; return v; }
I16 rdI16(const U8*& p) { return static_cast<I16>(rdU16(p)); }
U32 rdU32(const U8*& p) { U32 v = (static_cast<U32>(p[0]) << 24) | (static_cast<U32>(p[1]) << 16) | (static_cast<U32>(p[2]) << 8) | p[3]; p += 4; return v; }
F32 rdF32(const U8*& p) { U32 u = rdU32(p); F32 f; std::memcpy(&f, &u, sizeof f); return f; }
U8 rdU8(const U8*& p) { return *p++; }
void wrF32(U8* p, F32 f) { U32 u; std::memcpy(&u, &f, sizeof u); p[0] = static_cast<U8>(u >> 24); p[1] = static_cast<U8>(u >> 16); p[2] = static_cast<U8>(u >> 8); p[3] = static_cast<U8>(u); }
constexpr U16 STATUS_CORE_LEN = 120;  // struct.calcsize of the payload STATUS_FMT
constexpr U8 MAX_CANDIDATES = 8;      // charter 3.3: the ground scores at most this many targets
constexpr U16 CAND_LEN = 20;          // kind U8, x F32, y F32, pathUnits U16, novelty U8, flags U8,
                                      // threatClass U8, threatCount U8, opening U16, depth U16, away U8
constexpr U16 THREAT_LEN = 2;         // threatClass U8, threatCount U8
constexpr U16 DOOR_LEN = 4;           // doorPresses U16, doorOpens U16
constexpr U16 STATUS_LEN = STATUS_CORE_LEN + 1 + CAND_LEN * MAX_CANDIDATES + THREAT_LEN + DOOR_LEN;
constexpr U8 WAD_ARG_MAX = 40;        // LOAD_WAD's string sizes in Doom.fpp, and the WadName array size
constexpr U8 WAD_TEXT_MAX = 120;      // longest WAD report text kept (the reason; WadLoadFailed's size)
// The payload's WAD report (kind 3): what it did with a LOAD_WAD, or, with result REPORT, what it is
// running when the link comes up.
enum WadResult : U8 { WAD_REPORT = 0, WAD_LOADED = 1, WAD_FAILED = 2 };
// An uplinked WAD arrives as NAME.wad.<nonce>.part (or NAME.wad.part). The length of NAME.wad's path, or 0 for
// any other file. The nonce is the last dot-free segment, so a NAME that itself contains ".wad." survives.
FwSizeType wadDestLength(const char* path, FwSizeType len) {
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
}  // namespace

Doom ::Doom(const char* const compName)
    : DoomComponentBase(compName), m_sock(-1), m_retryTicks(0), m_rx(new U8[RX_CAPACITY]), m_rxLen(0),
      m_framesSent(0), m_chunksSent(0), m_cmdsReceived(0), m_lastEpisode(0), m_wasDead(false), m_wasDone(false), m_lastLevel(0), m_lastKeys(0),
      m_lastIntentId(0), m_watchdogTrips(0), m_ticks(0), m_wadKnown(false), m_wadLoads(0) {}

Doom ::~Doom() {
    this->dropPayload();
    delete[] this->m_rx;
}

// ----------------------------------------------------------------------
// Rate group tick
// ----------------------------------------------------------------------

void Doom ::run_handler(FwIndexType portNum, U32 context) {
    if (this->m_sock < 0) {
        if (this->m_retryTicks++ % 20 == 0) {  // once a second at 20 Hz
            this->connectPayload();
        }
    }
    if (this->m_sock >= 0) {
        this->drainSocket();
    }
    this->tlmWrite_PAYLOAD_LINK(this->m_sock >= 0);
    this->tlmWrite_FRAMES_SENT(this->m_framesSent);
    this->tlmWrite_CHUNKS_SENT(this->m_chunksSent);
    this->tlmWrite_CMDS_RECEIVED(this->m_cmdsReceived);
    // The payload reports its WAD once, when the link comes up, which can be before the ground is
    // listening: repeat the last report once a second so a ground that joins late still learns it.
    if (this->m_wadKnown && this->m_ticks++ % 20 == 0) {
        this->writeWadTlm();
    }
}

// ----------------------------------------------------------------------
// Uplinked WADs: NAME.wad.<anything>.part becomes NAME.wad once its checksum is verified: by FileUplink
// (fileAnnounce, native build) or by COMMIT_WAD against the size and checksum the ground sent (CFDP build)
// ----------------------------------------------------------------------

void Doom ::fileAnnounce_handler(FwIndexType portNum, Fw::StringBase& file_name) {
    (void)this->placeWad(file_name);  // anything else FileUplink receives (a sequence, a parameter file) is left alone
}

void Doom ::COMMIT_WAD_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, const Fw::CmdStringArg& part, U32 fileSize,
                                  U32 checksum) {
    this->m_cmdsReceived++;
    // A bare name: the directory is always the uplink directory, never one the command names
    if (std::strchr(part.toChar(), '/') != nullptr || wadDestLength(part.toChar(), part.length()) == 0) {
        this->log_WARNING_HI_WadUplinkFailed(part);
        this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::VALIDATION_ERROR);
        return;
    }
    // $DOOMSAT_HOME/wads/uplink, as the run script creates it and the payload looks in it
    const char* const home = std::getenv("DOOMSAT_HOME");
    const char* const user = std::getenv("HOME");
    Fw::String path;
    if (home != nullptr && home[0] != '\0') {
        path.format("%s/wads/uplink/%s", home, part.toChar());
    } else {
        path.format("%s/doom/wads/uplink/%s", (user != nullptr) ? user : "", part.toChar());
    }
    // cfdpManager writes the file in place and announces nothing, so check here that it is the whole file the
    // ground sent before it gets its real name: a commit before the last byte has landed, or after a damaged
    // Class 1 upload, leaves a .part that LOAD_WAD will not use. A file that is not there at all (already
    // committed by an earlier COMMIT_WAD whose answer was lost, or not yet created) is WadUplinkFailed, as before.
    // A commit between the last byte and cfdpManager's CRC pass does rename the whole file, and that transfer
    // then ends with a file-size error, so the ground waits for the FIN.
    FwSizeType haveSize = 0;
    U32 haveSum = 0;
    if (!fileSum(path.toChar(), haveSize, haveSum)) {
        this->log_WARNING_HI_WadUplinkFailed(path);
        this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::EXECUTION_ERROR);
        return;
    }
    if (haveSize != fileSize || haveSum != checksum) {
        this->log_WARNING_HI_WadCommitRefused(path, static_cast<U64>(haveSize), haveSum, fileSize, checksum);
        this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::EXECUTION_ERROR);
        return;
    }
    const bool ok = this->placeWad(path);
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

bool Doom ::fileSum(const char* path, FwSizeType& size, U32& checksum) {
    // One pass over the file on this component's thread. The build is unoptimised: about 150 ms for a 28.8 MB WAD
    // from the page cache, under 200 ms for the largest file Yamcs can uplink (a 32 MiB bucket upload). The doom
    // queue (60 deep, run ticks at 20 Hz plus commands) fills in about 3 s, and a full queue is an FW_ASSERT in
    // run_handlerBase.
    Os::File file;
    if (file.open(path, Os::File::OPEN_READ) != Os::File::OP_OK) {
        return false;
    }
    CFDP::Checksum sum;
    U8 buffer[4096];  // a multiple of 4, so every read but the last starts on a word
    FwSizeType total = 0;
    while (true) {
        FwSizeType got = sizeof(buffer);
        if (file.read(buffer, got) != Os::File::OP_OK) {
            return false;
        }
        if (got == 0) {
            break;
        }
        if (total + got > 0xFFFFFFFFu) {
            return false;  // larger than any size COMMIT_WAD can name
        }
        sum.update(buffer, static_cast<U32>(total), static_cast<U32>(got));
        total += got;
    }
    size = total;
    checksum = sum.getValue();
    return true;
}

bool Doom ::placeWad(const Fw::StringBase& file_name) {
    const char* const path = file_name.toChar();
    const FwSizeType destLen = wadDestLength(path, file_name.length());
    if (destLen == 0) {
        return false;
    }
    // A fresh .part name for every uplink, renamed over NAME.wad in the same directory: rename(2) is atomic,
    // and neither uplink service ever writes into an older file (FileUplink opens without truncating and would
    // leave an older file's tail; cfdpManager truncates and would destroy it).
    Fw::String dest;
    dest.format("%.*s", static_cast<int>(destLen), path);
    if (Os::FileSystem::rename(path, dest.toChar()) == Os::FileSystem::OP_OK) {
        this->log_ACTIVITY_HI_WadUplinked(dest);
        return true;
    }
    this->log_WARNING_HI_WadUplinkFailed(file_name);
    return false;
}

// ----------------------------------------------------------------------
// Commands: each becomes one small uplink record to the payload
// ----------------------------------------------------------------------

void Doom ::CONTROL_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, I8 move, I8 strafe, F32 turn, bool fire, bool use,
                               const DoomMission::Weapon& weapon) {
    this->m_cmdsReceived++;
    U8 body[9];
    body[0] = static_cast<U8>(move);
    body[1] = static_cast<U8>(strafe);
    wrF32(&body[2], turn);
    body[6] = fire ? 1 : 0;
    body[7] = use ? 1 : 0;
    body[8] = static_cast<U8>(weapon.e);
    const bool ok = this->sendToPayload(0x10, body, sizeof body);
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

void Doom ::INTENT_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, U16 intentId, U32 basedOnTic,
                              const DoomMission::IntentMode& mode, F32 targetX, F32 targetY, bool hasTarget,
                              const DoomMission::Stance& stance, const DoomMission::FirePolicy& firePolicy,
                              U8 fireTargetId, U8 weapon, bool useAtTarget, U16 ttlMs) {
    this->m_cmdsReceived++;
    U8 body[23];
    body[0] = static_cast<U8>(intentId >> 8);
    body[1] = static_cast<U8>(intentId);
    body[2] = static_cast<U8>(basedOnTic >> 24);
    body[3] = static_cast<U8>(basedOnTic >> 16);
    body[4] = static_cast<U8>(basedOnTic >> 8);
    body[5] = static_cast<U8>(basedOnTic);
    body[6] = static_cast<U8>(mode.e);
    wrF32(&body[7], targetX);
    wrF32(&body[11], targetY);
    body[15] = hasTarget ? 1 : 0;
    body[16] = static_cast<U8>(stance.e);
    body[17] = static_cast<U8>(firePolicy.e);
    body[18] = fireTargetId;
    body[19] = weapon;
    body[20] = useAtTarget ? 1 : 0;
    body[21] = static_cast<U8>(ttlMs >> 8);
    body[22] = static_cast<U8>(ttlMs);
    const bool ok = this->sendToPayload(0x15, body, sizeof body);
    if (ok) {
        this->m_lastIntentId = intentId;
        this->log_ACTIVITY_LO_IntentSet(intentId, mode, ttlMs);
    }
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

void Doom ::SET_GOAL_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, const DoomMission::Goal& goal) {
    this->m_cmdsReceived++;
    const U8 body = static_cast<U8>(goal.e);
    const bool ok = this->sendToPayload(0x11, &body, 1);
    if (ok) {
        this->log_ACTIVITY_LO_GoalSet(goal);
    }
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

void Doom ::RESET_GAME_cmdHandler(FwOpcodeType opCode, U32 cmdSeq) {
    this->m_cmdsReceived++;
    const bool ok = this->sendToPayload(0x12, nullptr, 0);
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

void Doom ::EXPLORE_HINT_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, I16 bearing, U8 ttl) {
    this->m_cmdsReceived++;
    const U8 body[3] = {static_cast<U8>(static_cast<U16>(bearing) >> 8), static_cast<U8>(bearing), ttl};
    const bool ok = this->sendToPayload(0x14, body, sizeof body);
    if (ok) {
        this->log_ACTIVITY_LO_ExploreHint(bearing, ttl);
    }
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

void Doom ::FRAME_RATE_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, U8 hz, U8 quality) {
    this->m_cmdsReceived++;
    const U8 body[2] = {hz, quality};
    const bool ok = this->sendToPayload(0x13, body, sizeof body);
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

void Doom ::LOAD_WAD_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, const Fw::CmdStringArg& iwad,
                                const Fw::CmdStringArg& pwad, const Fw::CmdStringArg& map) {
    this->m_cmdsReceived++;
    // The three names as they came, each a length byte and its text. The payload checks them: it is the
    // one that knows where WADs live, and it answers with a WAD report (kind 3) either way.
    U8 body[3 * (1 + WAD_ARG_MAX)];
    U16 n = 0;
    const Fw::StringBase* const names[3] = {&iwad, &pwad, &map};
    for (const Fw::StringBase* name : names) {
        const FwSizeType len = name->length();
        const U8 take = static_cast<U8>(len < WAD_ARG_MAX ? len : WAD_ARG_MAX);
        body[n++] = take;
        std::memcpy(&body[n], name->toChar(), take);
        n = static_cast<U16>(n + take);
    }
    const bool ok = this->sendToPayload(0x16, body, n);
    this->cmdResponse_out(opCode, cmdSeq, ok ? Fw::CmdResponse::OK : Fw::CmdResponse::EXECUTION_ERROR);
}

// ----------------------------------------------------------------------
// Payload link
// ----------------------------------------------------------------------

void Doom ::connectPayload() {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return;
    }
    sockaddr_in addr;
    std::memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons(PAYLOAD_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0) {
        ::close(fd);
        return;
    }
    int one = 1;
    (void)::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    (void)::fcntl(fd, F_SETFL, ::fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    this->m_sock = fd;
    this->m_rxLen = 0;
    this->log_ACTIVITY_HI_PayloadConnected();
}

void Doom ::dropPayload() {
    if (this->m_sock >= 0) {
        ::close(this->m_sock);
        this->m_sock = -1;
        this->m_rxLen = 0;
        this->log_WARNING_HI_PayloadLost();
    }
}

bool Doom ::sendToPayload(U8 kind, const U8* body, U16 length) {
    if (this->m_sock < 0) {
        return false;
    }
    U8 msg[4 + 128];
    FW_ASSERT(length <= sizeof msg - 4, length);
    msg[0] = 'D';
    msg[1] = kind;
    msg[2] = static_cast<U8>(length >> 8);
    msg[3] = static_cast<U8>(length);
    if (length > 0) {
        std::memcpy(&msg[4], body, length);
    }
    const ssize_t n = ::send(this->m_sock, msg, 4 + length, MSG_NOSIGNAL);
    if (n != static_cast<ssize_t>(4 + length)) {
        this->dropPayload();
        return false;
    }
    return true;
}

void Doom ::drainSocket() {
    for (;;) {
        if (this->m_rxLen >= RX_CAPACITY) {
            this->m_rxLen = 0;  // hopeless backlog: resync
        }
        const ssize_t n = ::recv(this->m_sock, this->m_rx + this->m_rxLen, RX_CAPACITY - this->m_rxLen, 0);
        if (n == 0) {
            this->dropPayload();
            return;
        }
        if (n < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                this->dropPayload();
            }
            break;
        }
        this->m_rxLen += static_cast<U32>(n);
    }
    // Parse complete records
    U32 pos = 0;
    while (this->m_rxLen - pos >= 4) {
        const U8* h = this->m_rx + pos;
        if (h[0] != 'D') {
            pos++;  // resync byte by byte
            continue;
        }
        const U8 kind = h[1];
        const U16 length = static_cast<U16>((h[2] << 8) | h[3]);
        if (this->m_rxLen - pos < 4u + length) {
            break;
        }
        this->handleMessage(kind, h + 4, length);
        pos += 4u + length;
    }
    if (pos > 0) {
        std::memmove(this->m_rx, this->m_rx + pos, this->m_rxLen - pos);
        this->m_rxLen -= pos;
    }
}

void Doom ::handleMessage(U8 kind, const U8* body, U16 length) {
    switch (kind) {
        case 1:
            this->handleStatus(body, length);
            break;
        case 2:
            this->handleFrame(body, length);
            break;
        case 3:
            this->handleWad(body, length);
            break;
        default:
            this->log_WARNING_LO_BadPayloadMessage(kind);
            break;
    }
}

void Doom ::handleStatus(const U8* body, U16 length) {
    if (length < STATUS_LEN) {
        this->log_WARNING_LO_BadPayloadMessage(1);
        return;
    }
    const U8* p = body;
    this->tlmWrite_HEALTH(rdI16(p));
    this->tlmWrite_ARMOR(rdI16(p));
    this->tlmWrite_SHELLS(rdI16(p));
    this->tlmWrite_BULLETS(rdI16(p));
    const U8 weapon = rdU8(p);
    this->tlmWrite_WEAPON(DoomMission::Weapon(static_cast<DoomMission::Weapon::T>(weapon > 3 ? 3 : weapon)));
    this->tlmWrite_OWN_SHOTGUN(rdU8(p) != 0);
    this->tlmWrite_KILLS(rdU16(p));
    this->tlmWrite_POS_X(rdF32(p));
    this->tlmWrite_POS_Y(rdF32(p));
    this->tlmWrite_ANGLE(rdF32(p));
    this->tlmWrite_ENEMY_COUNT(rdU8(p));
    this->tlmWrite_ENEMY_BEARING(rdF32(p));
    this->tlmWrite_ENEMY_DIST(rdU16(p));
    this->tlmWrite_CLEAR_FWD(rdU16(p));
    this->tlmWrite_CLEAR_FL(rdU16(p));
    this->tlmWrite_CLEAR_FR(rdU16(p));
    this->tlmWrite_CLEAR_LEFT(rdU16(p));
    this->tlmWrite_CLEAR_RIGHT(rdU16(p));
    this->tlmWrite_CLEAR_BACK(rdU16(p));
    this->tlmWrite_CLEAR_MAP_FWD(rdU16(p));
    this->tlmWrite_NEW_FWD(rdU8(p));
    this->tlmWrite_NEW_LEFT(rdU8(p));
    this->tlmWrite_NEW_RIGHT(rdU8(p));
    this->tlmWrite_NEW_BACK(rdU8(p));
    const U8 ahead = rdU8(p);
    this->tlmWrite_AHEAD_KIND(DoomMission::AheadKind(static_cast<DoomMission::AheadKind::T>(ahead > 6 ? 0 : ahead)));
    this->tlmWrite_AHEAD_DIST(rdU16(p));
    this->tlmWrite_EXIT_BEARING(rdF32(p));
    this->tlmWrite_EXIT_DIST(rdU16(p));
    this->tlmWrite_KEY_BEARING(rdF32(p));
    this->tlmWrite_KEY_DIST(rdU16(p));
    this->tlmWrite_HEALTH_ITEM_DIST(rdU16(p));
    this->tlmWrite_AMMO_ITEM_DIST(rdU16(p));
    this->tlmWrite_ARMOR_ITEM_DIST(rdU16(p));
    this->tlmWrite_HEALTH_BEARING(rdF32(p));
    this->tlmWrite_AMMO_BEARING(rdF32(p));
    this->tlmWrite_ARMOR_BEARING(rdF32(p));
    this->tlmWrite_STUCK(rdU8(p) != 0);
    this->tlmWrite_DOOR_AHEAD(rdU8(p) != 0);
    const U8 goal = rdU8(p);
    this->tlmWrite_GOAL(DoomMission::Goal(static_cast<DoomMission::Goal::T>(goal > 7 ? 7 : goal)));
    const U32 tic = rdU32(p);
    this->tlmWrite_TIC(tic);
    const U16 episode = rdU16(p);
    this->tlmWrite_EPISODE(episode);
    const bool dead = rdU8(p) != 0;
    const bool done = rdU8(p) != 0;
    this->tlmWrite_DEAD(dead);
    this->tlmWrite_LEVEL_DONE(done);
    this->tlmWrite_EXPLORED_CELLS(rdU16(p));
    const U8 level = rdU8(p);
    this->tlmWrite_LEVEL(level);
    const U8 keys = rdU8(p);
    this->tlmWrite_KEYS(keys);
    this->tlmWrite_HINT_ACTIVE(rdU8(p) != 0);
    this->tlmWrite_HINT_REL(rdI16(p));
    this->tlmWrite_CLEAR_AL(rdU16(p));
    this->tlmWrite_CLEAR_AR(rdU16(p));
    this->tlmWrite_CLEAR_BL(rdU16(p));
    this->tlmWrite_CLEAR_BR(rdU16(p));
    this->tlmWrite_NEW_AL(rdU8(p));
    this->tlmWrite_NEW_AR(rdU8(p));
    this->tlmWrite_NEW_BL(rdU8(p));
    this->tlmWrite_NEW_BR(rdU8(p));
    this->tlmWrite_DOOR_FWD(rdU8(p));
    this->tlmWrite_DOOR_AL(rdU8(p));
    this->tlmWrite_DOOR_LEFT(rdU8(p));
    this->tlmWrite_DOOR_BL(rdU8(p));
    this->tlmWrite_DOOR_BACK(rdU8(p));
    this->tlmWrite_DOOR_BR(rdU8(p));
    this->tlmWrite_DOOR_RIGHT(rdU8(p));
    this->tlmWrite_DOOR_AR(rdU8(p));
    // Charter 3.3: the candidate targets the onboard world model offers for the ground to score. Slots
    // past the count are sent as zeros rather than left stale, so a candidate that has gone away cannot
    // be scored a second time.
    const U8 candCount = rdU8(p);
    this->tlmWrite_CAND_COUNT(candCount);
    for (U8 i = 0; i < MAX_CANDIDATES; i++) {
        const U8 kind = rdU8(p);
        DoomMission::Candidate c;
        c.set_kind(DoomMission::CandKind(static_cast<DoomMission::CandKind::T>(kind > 6 ? 0 : kind)));
        c.set_x(rdF32(p));
        c.set_y(rdF32(p));
        c.set_pathUnits(rdU16(p));
        c.set_novelty(rdU8(p));
        c.set_flags(rdU8(p));
        c.set_threatClass(rdU8(p));
        c.set_threatCount(rdU8(p));
        c.set_opening(rdU16(p));
        c.set_depth(rdU16(p));
        c.set_away(rdU8(p));
        switch (i) {
            case 0: this->tlmWrite_CAND0(c); break;
            case 1: this->tlmWrite_CAND1(c); break;
            case 2: this->tlmWrite_CAND2(c); break;
            case 3: this->tlmWrite_CAND3(c); break;
            case 4: this->tlmWrite_CAND4(c); break;
            case 5: this->tlmWrite_CAND5(c); break;
            case 6: this->tlmWrite_CAND6(c); break;
            default: this->tlmWrite_CAND7(c); break;
        }
    }
    this->tlmWrite_THREAT_CLASS(rdU8(p));
    this->tlmWrite_THREAT_COUNT(rdU8(p));
    this->tlmWrite_DOOR_PRESSES(rdU16(p));
    this->tlmWrite_DOOR_OPENS(rdU16(p));
    this->tlmWrite_INTENT_ID(this->m_lastIntentId);
    this->tlmWrite_WATCHDOG_TRIPS(this->m_watchdogTrips);
    if (level != this->m_lastLevel) {
        this->m_lastLevel = level;
        this->log_ACTIVITY_HI_LevelStarted(level);
    }
    if (keys != this->m_lastKeys) {
        this->m_lastKeys = keys;
        this->log_ACTIVITY_HI_KeyPickedUp(keys);
    }
    if (episode != this->m_lastEpisode) {
        this->m_lastEpisode = episode;
        this->log_ACTIVITY_HI_EpisodeStarted(episode);
    }
    if (dead && !this->m_wasDead) {
        this->log_WARNING_LO_PlayerDied(episode, tic);
    }
    if (done && !this->m_wasDone) {
        this->log_ACTIVITY_HI_LevelFinished(episode, tic);
    }
    this->m_wasDead = dead;
    this->m_wasDone = done;
}

void Doom ::handleFrame(const U8* body, U16 length) {
    if (length < 4) {
        return;
    }
    const U8* p = body;
    const U32 seq = rdU32(p);
    const U32 jpegLen = static_cast<U32>(length) - 4;
    if (jpegLen > MAX_FRAME) {
        this->log_WARNING_LO_FrameTooLarge(jpegLen);
        return;
    }
    const U16 count = static_cast<U16>((jpegLen + CHUNK_DATA - 1) / CHUNK_DATA);
    const FwChanIdType chanId = static_cast<FwChanIdType>(this->getIdBase() + CHANNELID_FRAME_CHUNK);
    for (U16 index = 0; index < count; index++) {
        const U32 offset = static_cast<U32>(index) * CHUNK_DATA;
        const U32 n = (jpegLen - offset < CHUNK_DATA) ? (jpegLen - offset) : CHUNK_DATA;
        DoomMission::ChunkBytes bytes;
        for (U32 i = 0; i < CHUNK_DATA; i++) {
            bytes[i] = (i < n) ? p[offset + i] : 0;
        }
        DoomMission::FrameChunk chunk(seq, index, count, static_cast<U16>(n), bytes);
        // One telemetry record per chunk, exactly as Svc::TlmChan would frame a single channel update
        Fw::ComBuffer buf;
        Fw::SerializeStatus status = buf.serializeFrom(static_cast<FwPacketDescriptorType>(Fw::ComPacketType::FW_PACKET_TELEM));
        FW_ASSERT(status == Fw::FW_SERIALIZE_OK, static_cast<FwAssertArgType>(status));
        status = buf.serializeFrom(chanId);
        FW_ASSERT(status == Fw::FW_SERIALIZE_OK, static_cast<FwAssertArgType>(status));
        Fw::Time now = this->getTime();
        status = buf.serializeFrom(now);
        FW_ASSERT(status == Fw::FW_SERIALIZE_OK, static_cast<FwAssertArgType>(status));
        status = buf.serializeFrom(chunk);
        FW_ASSERT(status == Fw::FW_SERIALIZE_OK, static_cast<FwAssertArgType>(status));
        this->frameOut_out(0, buf, 0);
        this->m_chunksSent++;
    }
    this->m_framesSent++;
    this->tlmWrite_FRAME_BYTES(jpegLen);
}

void Doom ::handleWad(const U8* body, U16 length) {
    // result U8, loads U16, then five texts, each a length byte and its bytes: the IWAD and PWAD the game
    // is running now, the name the request asked for ("basic.wad over freedoom2.wad"), the map, and why
    // a load failed
    enum { IWAD, PWAD, NAME, MAP, REASON, TEXTS };
    if (length < 3) {
        this->log_WARNING_LO_BadPayloadMessage(3);
        return;
    }
    const U8* p = body;
    const U8* const end = body + length;
    const U8 result = rdU8(p);
    const U16 loads = rdU16(p);
    char text[TEXTS][WAD_TEXT_MAX + 1];
    FwSizeType textLen[TEXTS];
    for (U32 i = 0; i < TEXTS; i++) {
        if (p >= end || static_cast<FwSizeType>(*p) > static_cast<FwSizeType>(end - p - 1)) {
            this->log_WARNING_LO_BadPayloadMessage(3);
            return;
        }
        const U8 len = *p++;
        textLen[i] = len < WAD_TEXT_MAX ? len : WAD_TEXT_MAX;
        std::memcpy(text[i], p, textLen[i]);
        text[i][textLen[i]] = '\0';
        p += len;
    }
    for (FwSizeType i = 0; i < DoomMission::WadName::SIZE; i++) {
        this->m_wadIwad[i] = static_cast<U8>(i < textLen[IWAD] ? text[IWAD][i] : 0);
        this->m_wadPwad[i] = static_cast<U8>(i < textLen[PWAD] ? text[PWAD][i] : 0);
    }
    this->m_wadLoads = loads;
    this->m_wadKnown = true;
    this->writeWadTlm();
    if (result == WAD_LOADED) {
        this->m_lastLevel = 0;  // the new WAD starts at level 1: announce it even if the old one was on 1 too
        this->log_ACTIVITY_HI_WadLoaded(Fw::String(text[NAME]), Fw::String(text[MAP]));
    } else if (result == WAD_FAILED) {
        this->log_WARNING_HI_WadLoadFailed(Fw::String(text[NAME]), Fw::String(text[REASON]));
    }
}

void Doom ::writeWadTlm() {
    this->tlmWrite_WAD_IWAD(this->m_wadIwad);
    this->tlmWrite_WAD_PWAD(this->m_wadPwad);
    this->tlmWrite_WAD_LOADS(this->m_wadLoads);
}

}  // namespace DoomMission
