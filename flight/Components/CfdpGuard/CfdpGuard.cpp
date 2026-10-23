// ======================================================================
// \title  CfdpGuard.cpp
// \brief  The CFDP uplink's gatekeeper (see CfdpGuard.fpp)
//
// Both paths read the buffer the way CfdpManager::dataIn_handler does: a big-endian FW_PACKET_FILE descriptor,
// then the PDU, typed with peekPduType and decoded with F´'s own MetadataPdu / EofPdu / FinPdu. The guard therefore
// sees a Metadata exactly when cfdpManager would, and the destination it checks is the string cfdpManager would open.
// ======================================================================

#include "DoomMission/Components/CfdpGuard/CfdpGuard.hpp"

#include "DoomMission/Components/Doom/WadPath.hpp"
#include "Fw/Com/ComPacket.hpp"
#include "Fw/Types/SerialBuffer.hpp"

namespace DoomMission {

namespace {

using namespace Svc::Ccsds::Cfdp;

constexpr FwSizeType DESCRIPTOR_SIZE = sizeof(FwPacketDescriptorType);
static_assert(DESCRIPTOR_SIZE == 2, "the descriptor is rewritten as two bytes below");

//! The PDU behind the FW_PACKET_FILE descriptor, as CfdpManager::dataIn_handler cuts it; empty when cfdpManager
//! would not pass the buffer to its engine at all
Fw::Buffer pduView(Fw::Buffer& fwBuffer) {
    if (fwBuffer.getSize() < DESCRIPTOR_SIZE) {
        return Fw::Buffer();
    }
    FwPacketDescriptorType packetType = 0;
    const Fw::SerializeStatus status = fwBuffer.getDeserializer().deserializeTo(packetType);
    if (status != Fw::FW_SERIALIZE_OK || packetType != Fw::ComPacketType::FW_PACKET_FILE) {
        return Fw::Buffer();
    }
    return Fw::Buffer(fwBuffer.getData() + DESCRIPTOR_SIZE, fwBuffer.getSize() - DESCRIPTOR_SIZE,
                      fwBuffer.getContext());
}

//! A deserializer over exactly the view's bytes, as Engine::receivePdu builds it
Fw::SerialBuffer serialOver(const Fw::Buffer& pdu) {
    Fw::SerialBuffer sb(pdu.getData(), pdu.getSize());
    sb.setBuffLen(pdu.getSize());
    return sb;
}

}  // namespace

CfdpGuard ::CfdpGuard(const char* const compName)
    : CfdpGuardComponentBase(compName), m_refusedNext(0), m_order(0), m_accepted(0), m_refusedCount(0),
      m_committed(0) {
    for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
        this->m_uploads[i].used = false;
        this->m_uploads[i].awaitsFin = false;
        this->m_uploads[i].ended = false;
        this->m_refused[i].used = false;
    }
}

CfdpGuard ::~CfdpGuard() {}

// ----------------------------------------------------------------------
// Up: router -> cfdpManager
// ----------------------------------------------------------------------

void CfdpGuard ::uplinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) {
    const Fw::Buffer pdu = pduView(fwBuffer);
    const PduTypeEnum::T type = (pdu.getSize() > 0) ? peekPduType(pdu) : PduTypeEnum::NONE;
    if (type == PduTypeEnum::METADATA && !this->admitMetadata(pdu)) {
        // cfdpManager hands back a buffer whose descriptor is not FW_PACKET_FILE without reading it, on its own
        // thread, through the dataInReturn path the router already has (CfdpManager::dataIn_handler).
        U8* const data = fwBuffer.getData();
        data[0] = 0;
        data[1] = static_cast<U8>(Fw::ComPacketType::FW_PACKET_UNKNOWN);
    } else if (type == PduTypeEnum::END_OF_FILE) {
        this->noteEof(pdu);
    }
    this->uplinkOut_out(0, fwBuffer);
}

FwSizeType CfdpGuard ::takeSlot(bool& forgotten, Upload& old) {
    // A free slot; else the oldest transaction that has ended, then the oldest no FIN can settle (class 1, or
    // addressed elsewhere), and only then the oldest upload still waiting for its FIN
    FwSizeType best = MAX_UPLOADS;
    U8 bestRank = 0;
    for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
        const Upload& u = this->m_uploads[i];
        if (!u.used) {
            return i;
        }
        const U8 rank = u.ended ? 3 : (u.awaitsFin ? 1 : 2);
        if (best == MAX_UPLOADS || rank > bestRank || (rank == bestRank && u.order < this->m_uploads[best].order)) {
            best = i;
            bestRank = rank;
        }
    }
    if (bestRank == 1) {
        forgotten = true;
        old = this->m_uploads[best];
    }
    return best;
}

bool CfdpGuard ::admitMetadata(const Fw::Buffer& pdu) {
    MetadataPdu md;
    Fw::SerialBuffer sb = serialOver(pdu);
    const Fw::SerializeStatus status = md.deserializeFrom(sb);
    if (status != Fw::FW_SERIALIZE_OK) {
        // cfdpManager could not read it either (FailMetadataPduDeserialization) and would open nothing. Reported
        // once per transaction when its header still decodes, like a refused destination; a header that does not
        // decode is reported each time, within the event's throttle
        PduHeader header;
        Fw::SerialBuffer hb = serialOver(pdu);
        if (header.fromSerialBuffer(hb) != Fw::FW_SERIALIZE_OK ||
            this->firstRefusal(header.getSourceEid(), header.getTransactionSeq())) {
            this->log_WARNING_LO_MetadataUnreadable(static_cast<I32>(status));
            this->tlmWrite_UPLOADS_REFUSED(++this->m_refusedCount);
        }
        return false;
    }
    const EntityId src = md.getSourceEid();
    const TransactionSeq seq = md.getTransactionSeq();
    const Fw::String& dest = md.getDestFilename();
    bool admit = WadPath::isUplinkPart(dest.toChar());

    bool resend = false;     // a Metadata for a transaction already let through
    bool forgotten = false;  // an upload still waiting for its FIN had to make room
    Upload old;
    if (admit) {
        // Every Metadata let through is remembered, whatever its class or destination entity, and kept after its
        // transaction ends. cfdpManager routes a Metadata by (source, sequence number) alone, whatever its class
        // bit, and keeps the first destination it takes for a transaction (r2RecvMd runs once; after the FIN it
        // drops Metadata). So a second Metadata for a known transaction is let through only if it names the same
        // file: then the destination the guard holds is the one cfdpManager checksummed.
        Os::ScopeLock lock(this->m_lock);
        for (FwSizeType i = 0; i < MAX_UPLOADS && !resend; i++) {
            const Upload& u = this->m_uploads[i];
            if (u.used && u.srcEid == src && u.seq == seq) {
                resend = true;
                admit = (u.dest == dest);
            }
        }
        if (!resend) {
            Upload& u = this->m_uploads[this->takeSlot(forgotten, old)];
            u.used = true;
            // Class 1 sends no FIN, so only COMMIT_WAD, with its own checks, puts a class 1 file in place; and
            // cfdpManager starts no receive for a Metadata addressed to another entity
            u.awaitsFin = (md.getTxmMode() == Class::CLASS_2) && (md.getDestEid() == LOCAL_EID);
            u.ended = false;
            u.srcEid = src;
            u.seq = seq;
            u.destEid = md.getDestEid();
            u.order = this->m_order++;
            u.dest = dest;
        }
    }

    if (!admit) {
        // Once per transaction: the sender resends a Metadata each time the receiver asks for it
        if (this->firstRefusal(src, seq)) {
            this->log_WARNING_HI_UploadRefused(dest, src, seq);
            this->tlmWrite_UPLOADS_REFUSED(++this->m_refusedCount);
        }
        return false;
    }
    if (resend) {
        return true;
    }
    if (forgotten) {
        this->log_WARNING_LO_UploadForgotten(old.dest, old.srcEid, old.seq);
    }
    this->tlmWrite_UPLOADS_ACCEPTED(++this->m_accepted);
    return true;
}

bool CfdpGuard ::firstRefusal(EntityId src, TransactionSeq seq) {
    Os::ScopeLock lock(this->m_lock);
    for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
        const Refused& r = this->m_refused[i];
        if (r.used && r.srcEid == src && r.seq == seq) {
            return false;
        }
    }
    Refused& r = this->m_refused[this->m_refusedNext];
    this->m_refusedNext = (this->m_refusedNext + 1) % MAX_UPLOADS;
    r.used = true;
    r.srcEid = src;
    r.seq = seq;
    return true;
}

// ----------------------------------------------------------------------
// Down: cfdpManager -> com queue
// ----------------------------------------------------------------------

void CfdpGuard ::downlinkIn_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) {
    const Fw::Buffer pdu = pduView(fwBuffer);
    // File data, most of what a downlink sends, is told apart by one bit and passes straight on
    if (pdu.getSize() > 0 && peekPduType(pdu) == PduTypeEnum::FINISHED) {
        this->settleFin(pdu);  // before the FIN leaves: the file is in place by the time the ground hears of it
    }
    this->downlinkOut_out(portNum, fwBuffer);
}

void CfdpGuard ::settleFin(const Fw::Buffer& pdu) {
    FinPdu fin;
    Fw::SerialBuffer sb = serialOver(pdu);
    // FinPdu starts out reading NO_ERROR / COMPLETE / RETAINED, so a FIN that did not decode must never count
    if (fin.deserializeFrom(sb) != Fw::FW_SERIALIZE_OK || fin.getDirection() != PduDirection::DIRECTION_TOWARD_SENDER) {
        return;
    }
    Fw::String dest;
    {
        Os::ScopeLock lock(this->m_lock);
        FwSizeType slot = MAX_UPLOADS;
        for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
            const Upload& u = this->m_uploads[i];
            if (u.used && u.awaitsFin && !u.ended && u.srcEid == fin.getSourceEid() &&
                u.seq == fin.getTransactionSeq() && u.destEid == fin.getDestEid()) {
                slot = i;
            }
        }
        if (slot == MAX_UPLOADS) {
            // Not a class 2 upload the guard let through, or one already settled: F´ repeats its FIN until the
            // ground ACKs it, and the repeats commit nothing
            return;
        }
        dest = this->m_uploads[slot].dest;
        this->m_uploads[slot].ended = true;
    }
    // The receiver sends NO_ERROR with RETAINED only after its checksum over the whole file matched, with the file
    // at the Metadata's destination (TransactionRx r2CalcCrcChunk); any other FIN leaves the file as it is
    const ConditionCode cc = fin.getConditionCode();
    const FinFileStatus fs = fin.getFileStatus();
    if (cc == ConditionCode::CONDITION_CODE_NO_ERROR && fs == FinFileStatus::FIN_FILE_STATUS_RETAINED &&
        fin.getDeliveryCode() == FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE) {
        // Counted as announced: Doom's WadUplinked or WadUplinkFailed says how its rename went
        this->log_ACTIVITY_HI_UploadCommitted(dest, fin.getSourceEid(), fin.getTransactionSeq());
        this->tlmWrite_UPLOADS_COMMITTED(++this->m_committed);
        if (this->isConnected_fileAnnounceOut_OutputPort(0)) {
            this->fileAnnounceOut_out(0, dest);
        }
    } else {
        this->log_WARNING_LO_UploadNotCommitted(dest, fin.getSourceEid(), fin.getTransactionSeq(),
                                                static_cast<U8>(cc), static_cast<U8>(fs));
    }
}

void CfdpGuard ::noteEof(const Fw::Buffer& pdu) {
    EofPdu eof;
    Fw::SerialBuffer sb = serialOver(pdu);
    // EofPdu also starts out reading NO_ERROR, so one that did not decode is ignored like a normal EOF
    if (eof.deserializeFrom(sb) != Fw::FW_SERIALIZE_OK || eof.getDirection() != PduDirection::DIRECTION_TOWARD_RECEIVER ||
        eof.getConditionCode() == ConditionCode::CONDITION_CODE_NO_ERROR) {
        return;
    }
    // The sender cancelled: cfdpManager ends the transaction with no FIN (r2Reset), so nothing will settle it, and
    // its record can make room first
    Os::ScopeLock lock(this->m_lock);
    for (FwSizeType i = 0; i < MAX_UPLOADS; i++) {
        Upload& u = this->m_uploads[i];
        if (u.used && u.srcEid == eof.getSourceEid() && u.seq == eof.getTransactionSeq()) {
            u.ended = true;
        }
    }
}

}  // namespace DoomMission
