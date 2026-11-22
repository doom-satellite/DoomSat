// ======================================================================
// \title  Doom.hpp
// \brief  Payload interface: the Doom game process seen as an instrument.
//
// Polled by a rate group. Reads STATUS and FRAME records from the payload socket, publishes
// the status as telemetry channels and every JPEG frame as a run of FrameChunk telemetry
// records sent straight to the com queue (so no chunk is lost to TlmChan sampling). Forwards
// CONTROL / SET_GOAL / RESET / FRAME_RATE / LOAD_WAD commands to the payload, and turns its WAD
// reports into the WAD_* channels and the WadLoaded / WadLoadFailed events.
// ======================================================================

#ifndef DoomMission_Doom_HPP
#define DoomMission_Doom_HPP

#include "DoomMission/Components/Doom/DoomComponentAc.hpp"

#include <Os/Mutex.hpp>

namespace DoomMission {

class Doom final : public DoomComponentBase {
    friend class DoomTester;  //!< the unit tests (test/ut) hand it WAD reports directly

  public:
    Doom(const char* const compName);
    ~Doom();

    static constexpr U16 PAYLOAD_PORT = 4242;
    static constexpr U32 CHUNK_DATA = 960;               //!< must match ChunkBytes size in Doom.fpp
    static constexpr U32 MAX_FRAME = CHUNK_DATA * 64;    //!< frames beyond this are dropped
    static constexpr U32 RX_CAPACITY = 256 * 1024;

  private:
    // Rate group tick: connect if needed, drain the socket, downlink what arrived
    void run_handler(FwIndexType portNum, U32 context) override;
    // An uplinked WAD known to be whole (cfdpGuard on the receiver's FIN, or FileUplink): renamed into place
    void fileAnnounce_handler(FwIndexType portNum, Fw::StringBase& file_name) override;

    void CONTROL_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, I8 move, I8 strafe, F32 turn, bool fire, bool use,
                            const DoomMission::Weapon& weapon) override;
    void INTENT_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, U16 intentId, U32 basedOnTic,
                           const DoomMission::IntentMode& mode, F32 targetX, F32 targetY, bool hasTarget,
                           const DoomMission::Stance& stance, const DoomMission::FirePolicy& firePolicy,
                           U8 fireTargetId, U8 weapon, bool useAtTarget, U16 ttlMs) override;
    void SET_GOAL_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, const DoomMission::Goal& goal) override;
    void RESET_GAME_cmdHandler(FwOpcodeType opCode, U32 cmdSeq) override;
    void EXPLORE_HINT_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, I16 bearing, U8 ttl) override;
    void FRAME_RATE_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, U8 hz, U8 quality) override;
    void LOAD_WAD_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, const Fw::CmdStringArg& iwad,
                             const Fw::CmdStringArg& pwad, const Fw::CmdStringArg& map) override;
    void COMMIT_WAD_cmdHandler(FwOpcodeType opCode, U32 cmdSeq, const Fw::CmdStringArg& part, U32 fileSize,
                               U32 checksum) override;
    // Rename an uplinked NAME.wad.<nonce>.part to NAME.wad (WadUplinked); false if it is not one (no event) or the
    // rename fails (WadUplinkFailed, unless logFailure is false)
    bool placeWad(const Fw::StringBase& path, bool logFailure = true);
    // Whether the uplinked NAME.wad.<nonce>.part `path` is the upload last put in place as NAME.wad (since start, while
    // the record still holds NAME.wad), and NAME.wad still has this size and CFDP checksum; `dest` is NAME.wad's path
    bool placedFrom(const Fw::StringBase& path, U32 fileSize, U32 checksum, Fw::String& dest);
    // Size and CFDP modular checksum of a file on board; false if it cannot be read
    static bool fileSum(const char* path, FwSizeType& size, U32& checksum);

    // Payload link
    void connectPayload();
    void dropPayload();
    bool sendToPayload(U8 kind, const U8* body, U16 length);
    void drainSocket();
    void handleMessage(U8 kind, const U8* body, U16 length);
    void handleStatus(const U8* body, U16 length);
    void handleFrame(const U8* body, U16 length);
    void handleWad(const U8* body, U16 length);
    void writeWadTlm();

    int m_sock;
    U32 m_retryTicks;
    U8* m_rx;            //!< receive accumulation buffer (heap, RX_CAPACITY)
    U32 m_rxLen;
    U32 m_framesSent;
    U32 m_chunksSent;
    U32 m_cmdsReceived;
    U16 m_lastEpisode;
    bool m_wasDead;
    U8 m_lastLevel;
    U8 m_lastKeys;
    bool m_wasDone;
    U16 m_lastIntentId;   //!< the intent the executor is carrying out, echoed back in telemetry
    U16 m_watchdogTrips;  //!< times an onboard invariant had to pull the player out of a freeze
    U32 m_ticks;                  //!< rate group ticks, for the once-a-second WAD channels
    bool m_wadKnown;              //!< a WAD report has arrived since boot
    DoomMission::WadName m_wadIwad;  //!< the level file the payload last reported running
    DoomMission::WadName m_wadPwad;
    U16 m_wadLoads;

    //! Which upload each NAME.wad was last put in place from, so that a COMMIT_WAD sent again is answered for that
    //! upload only (a size and checksum alone would also match an older NAME.wad with its words in another order).
    //! placeWad runs on cfdpManager's thread for cfdpGuard's commits and on this component's for COMMIT_WAD.
    //! It holds PLACED_MAX names: a new name takes the next slot in turn (m_placedNext), so a repeat for a name whose
    //! record it displaced answers WadUplinkFailed, as after a restart.
    static constexpr FwSizeType PLACED_MAX = 8;
    struct Placed {
        bool used;
        Fw::String dest;  //!< NAME.wad's path
        Fw::String part;  //!< the NAME.wad.<nonce>.part path it was renamed from
    };
    Os::Mutex m_placedLock;
    Placed m_placed[PLACED_MAX];
    FwSizeType m_placedNext;
};

}  // namespace DoomMission

#endif
