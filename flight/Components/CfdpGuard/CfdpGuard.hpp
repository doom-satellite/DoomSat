// ======================================================================
// \title  CfdpGuard.hpp
// \brief  The CFDP uplink's gatekeeper: confines uploads to the uplink directory and commits a WAD on board
//         when cfdpManager's own FIN says it arrived whole (see CfdpGuard.fpp).
// ======================================================================

#ifndef DoomMission_CfdpGuard_HPP
#define DoomMission_CfdpGuard_HPP

#include "DoomMission/Components/CfdpGuard/CfdpGuardComponentAc.hpp"

#include <Os/Mutex.hpp>
#include <Svc/Ccsds/CfdpManager/Types/PduBase.hpp>

namespace DoomMission {

class CfdpGuard final : public CfdpGuardComponentBase {
  public:
    //! Transactions remembered at once, from their first Metadata until newer ones need the room. cfdpManager does
    //! not stop at MaxSimultaneousRx (5): in F´ v4.3.0 the check in Engine::startRxTransaction is commented out, so
    //! any of the channel's 50 transactions (CFDP_NUM_TRANSACTIONS_PER_CHANNEL) can be a receive. When the table is
    //! full, a transaction that has ended goes first, then one no FIN can settle, and only then the oldest upload
    //! still waiting for its FIN (UploadForgotten: its FIN will not put it in place, and COMMIT_WAD can). The ground
    //! sends one upload at a time (Yamcs maxNumPendingUploads: 1), so that last case needs nine at once.
    static constexpr FwSizeType MAX_UPLOADS = 8;
    //! cfdpManager's LocalEid parameter (flight/config/PrmDb.json; tests/test_cfdp_guard.py keeps the two equal).
    //! Only a class 2 upload addressed to it can end with the receiver's FIN.
    static constexpr Svc::Ccsds::Cfdp::EntityId LOCAL_EID = 42;

    explicit CfdpGuard(const char* const compName);
    ~CfdpGuard();

  private:
    // Up: decides each Metadata; every buffer goes on to cfdpManager, a refused Metadata with its descriptor changed
    void uplinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) override;
    // Down: watches for the receiver's FIN of an upload it let through; every buffer goes on unchanged
    void downlinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) override;

    //! What to do with one Metadata PDU (the view behind the descriptor); false to refuse it
    bool admitMetadata(const Fw::Buffer& pdu);
    //! Settle the upload a FIN belongs to (the view behind the descriptor)
    void settleFin(const Fw::Buffer& pdu);
    //! An EOF from the sender (the view behind the descriptor): a cancel ends its transaction with no FIN
    void noteEof(const Fw::Buffer& pdu);

    //! One transaction whose Metadata was let through. Kept after it ends, until the room is needed, so that a late
    //! copy of its Metadata, or a repeat of its FIN, changes nothing.
    struct Upload {
        bool used;
        bool awaitsFin;  //!< class 2 and addressed to cfdpManager: the receiver's FIN settles it
        bool ended;      //!< its FIN came, or the sender cancelled it
        Svc::Ccsds::Cfdp::EntityId srcEid;
        Svc::Ccsds::Cfdp::TransactionSeq seq;
        Svc::Ccsds::Cfdp::EntityId destEid;
        U64 order;        //!< when it was recorded: among equals, the oldest goes first when the table is full
        Fw::String dest;  //!< the destination as cfdpManager reads it (MetadataPdu::getDestFilename)
    };
    //! The slot a new transaction takes (with m_lock held). Sets `forgotten` and `old` when that pushes out an
    //! upload still waiting for its FIN.
    FwSizeType takeSlot(bool& forgotten, Upload& old);
    //! A transaction whose Metadata was refused, so its resends are refused without another event
    struct Refused {
        bool used;
        Svc::Ccsds::Cfdp::EntityId srcEid;
        Svc::Ccsds::Cfdp::TransactionSeq seq;
    };
    //! Record a refused transaction (takes m_lock); true the first time, false for a resend of its Metadata
    bool firstRefusal(Svc::Ccsds::Cfdp::EntityId src, Svc::Ccsds::Cfdp::TransactionSeq seq);

    Os::Mutex m_lock;  //!< the tables: uplinkIn runs on the radio's receive thread, downlinkIn on cfdpManager's
    Upload m_uploads[MAX_UPLOADS];
    Refused m_refused[MAX_UPLOADS];
    FwSizeType m_refusedNext;
    U64 m_order;
    U32 m_accepted;
    U32 m_refusedCount;
    U32 m_committed;
};

}  // namespace DoomMission

#endif
