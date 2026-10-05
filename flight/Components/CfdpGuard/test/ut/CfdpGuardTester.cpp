// ======================================================================
// \title  CfdpGuardTester.cpp
// \brief  CfdpGuard against PDUs built with F´'s own CFDP encoder (and one built byte by byte the way Yamcs does)
// ======================================================================

#include "CfdpGuardTester.hpp"

#include <Fw/Com/ComPacket.hpp>
#include <Fw/Types/SerialBuffer.hpp>

#include <cstdlib>
#include <cstring>
#include <vector>

namespace DoomMission {

using namespace Svc::Ccsds;

const char* const CfdpGuardTester::UPLINK = "/home/x/doom/wads/uplink";

namespace {

//! Big-endian FW_PACKET_FILE descriptor, then the PDU: the layout PduTester.cpp's sendMetadataPdu uses
template <typename PDU>
Fw::Buffer wrap(U8* storage, FwSizeType capacity, const PDU& pdu) {
    storage[0] = 0x00;
    storage[1] = static_cast<U8>(Fw::ComPacketType::FW_PACKET_FILE);
    Fw::SerialBuffer sb(storage + 2, capacity - 2);
    EXPECT_EQ(Fw::FW_SERIALIZE_OK, pdu.serializeTo(sb));
    return Fw::Buffer(storage, 2 + sb.getSize());
}

void put(std::vector<U8>& out, U64 value, FwSizeType bytes) {
    for (FwSizeType i = bytes; i > 0; i--) {
        out.push_back(static_cast<U8>((value >> (8 * (i - 1))) & 0xFF));
    }
}

}  // namespace

CfdpGuardTester ::CfdpGuardTester()
    : CfdpGuardGTestBase("CfdpGuardTester", CfdpGuardTester::MAX_HISTORY_SIZE), component("CfdpGuard") {
    setenv("DOOMSAT_HOME", "/home/x/doom/", 1);  // a trailing '/' as a user might write it
    this->initComponents();
    this->connectPorts();
}

CfdpGuardTester ::~CfdpGuardTester() {}

std::string CfdpGuardTester ::path(const char* name) const {
    return std::string(UPLINK) + "/" + name;
}

Fw::Buffer CfdpGuardTester ::metadata(U32 seq, const char* dest, Cfdp::Class::T txm, U32 src, U32 dst) {
    Cfdp::MetadataPdu md;
    md.initialize(Cfdp::PduDirection::DIRECTION_TOWARD_RECEIVER, txm, src, seq, dst, 2704, Fw::String("basic.wad"),
                  Fw::String(dest), Cfdp::ChecksumType::CHECKSUM_TYPE_MODULAR, 1);
    return wrap(this->m_up, sizeof this->m_up, md);
}

Fw::Buffer CfdpGuardTester ::yamcsMetadata(U32 seq, const std::string& dest) {
    std::vector<U8> data;  // the PDU data field
    data.push_back(0x07);  // Metadata directive
    data.push_back(0x00);  // closure / checksum type (modular)
    put(data, 2704, 4);    // file size
    const std::string src = "bucket/basic.wad";
    data.push_back(static_cast<U8>(src.size()));
    data.insert(data.end(), src.begin(), src.end());
    data.push_back(static_cast<U8>(dest.size()));
    data.insert(data.end(), dest.begin(), dest.end());
    std::vector<U8> pdu;
    pdu.push_back(0x20);          // version 1, directive, toward receiver, class 2, no CRC, small file
    put(pdu, data.size(), 2);     // data field length
    pdu.push_back((1 << 4) | 3);  // entity ids in 2 bytes, sequence numbers in 4 (Yamcs's entityIdLength 2)
    put(pdu, GROUND, 2);
    put(pdu, seq, 4);
    put(pdu, BOARD, 2);
    pdu.insert(pdu.end(), data.begin(), data.end());
    this->m_up[0] = 0x00;
    this->m_up[1] = static_cast<U8>(Fw::ComPacketType::FW_PACKET_FILE);
    std::memcpy(this->m_up + 2, pdu.data(), pdu.size());
    return Fw::Buffer(this->m_up, 2 + pdu.size());
}

Fw::Buffer CfdpGuardTester ::fin(U32 seq, Cfdp::ConditionCode cc, Cfdp::FinDeliveryCode dc, Cfdp::FinFileStatus fs,
                                 U32 dst, U32 src) {
    Cfdp::FinPdu f;  // the receiver's FIN: the transaction's source (the ground) in the header, as Engine::sendFin
    f.initialize(Cfdp::PduDirection::DIRECTION_TOWARD_SENDER, Cfdp::Class::CLASS_2, src, seq, dst, cc, dc, fs);
    return wrap(this->m_down, sizeof this->m_down, f);
}

Fw::Buffer CfdpGuardTester ::eof(U32 seq, Cfdp::ConditionCode cc) {
    Cfdp::EofPdu e;
    e.initialize(Cfdp::PduDirection::DIRECTION_TOWARD_RECEIVER, Cfdp::Class::CLASS_2, GROUND, seq, BOARD, cc, 0, 0);
    return wrap(this->m_up, sizeof this->m_up, e);
}

void CfdpGuardTester ::upload(U32 seq, const std::string& dest) {
    Fw::Buffer md = this->metadata(seq, dest.c_str());
    this->invoke_to_uplinkIn(0, md);
    ASSERT_TRUE(this->lastUplinkReadable()) << dest;
}

void CfdpGuardTester ::from_downlinkOut_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) {
    this->m_downlinkPorts.push_back(portNum);
    this->m_announcedAtDownlink.push_back(this->fromPortHistory_fileAnnounceOut->size());
    this->pushFromPortEntry_downlinkOut(fwBuffer);
}

bool CfdpGuardTester ::lastUplinkReadable() {
    const Fw::Buffer& b = this->fromPortHistory_uplinkOut->at(this->fromPortHistory_uplinkOut->size() - 1).fwBuffer;
    const U8* d = b.getData();
    return b.getSize() >= 2 && d[0] == 0x00 && d[1] == static_cast<U8>(Fw::ComPacketType::FW_PACKET_FILE);
}

// ----------------------------------------------------------------------
// Tests
// ----------------------------------------------------------------------

void CfdpGuardTester ::testAnUploadToTheUplinkDirectoryPassesUnchanged() {
    Fw::Buffer md = this->metadata(7, this->path("basic.wad.1791094305599.part").c_str());
    std::vector<U8> before(md.getData(), md.getData() + md.getSize());
    this->invoke_to_uplinkIn(0, md);
    ASSERT_from_uplinkOut_SIZE(1);
    const Fw::Buffer& out = this->fromPortHistory_uplinkOut->at(0).fwBuffer;
    ASSERT_EQ(out.getData(), md.getData());  // the same buffer: the router finds its context by the pointer
    ASSERT_EQ(out.getSize(), md.getSize());
    ASSERT_EQ(0, std::memcmp(out.getData(), before.data(), before.size()));
    ASSERT_EVENTS_SIZE(0);
    ASSERT_TLM_UPLOADS_ACCEPTED(0, 1);
    // The nonce is the ground's habit, not the rule: NAME.wad.part lands too
    this->upload(8, this->path("x.wad.part"));
    ASSERT_TLM_UPLOADS_ACCEPTED(1, 2);
}

void CfdpGuardTester ::testItsFinCommitsItOnce() {
    const std::string dest = this->path("basic.wad.1791094305599.part");
    Fw::Buffer md = this->metadata(7, dest.c_str());
    this->invoke_to_uplinkIn(0, md);
    Fw::Buffer f = this->fin(7);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_downlinkOut_SIZE(1);
    ASSERT_from_downlinkOut(0, f);  // passed on unchanged, on the same port
    ASSERT_EQ(1u, this->m_announcedAtDownlink.at(0));  // announced, so renamed, before the FIN went on
    ASSERT_from_fileAnnounceOut_SIZE(1);
    ASSERT_from_fileAnnounceOut(0, Fw::String(dest.c_str()));
    ASSERT_EVENTS_UploadCommitted_SIZE(1);
    ASSERT_EVENTS_UploadCommitted(0, dest.c_str(), GROUND, 7);
    ASSERT_TLM_UPLOADS_COMMITTED(0, 1);
    // F´ repeats its FIN until the ground ACKs it: the repeats commit nothing more
    for (int i = 0; i < 3; i++) {
        Fw::Buffer again = this->fin(7);
        this->invoke_to_downlinkIn(0, again);
    }
    ASSERT_from_downlinkOut_SIZE(4);
    ASSERT_from_fileAnnounceOut_SIZE(1);
}

void CfdpGuardTester ::testEveryOtherDestinationIsRefused() {
    const std::vector<std::string> bad = {
        "/root/.bashrc",
        "/tmp/doomsat_cfdp_escape.bin",
        "/tmp/x.wad.1.part",
        this->path("../escape_dotdot.part"),
        this->path("../x.wad.1.part"),
        this->path("sub/x.wad.1.part"),
        this->path(".cfdp-tmp/x.wad.1.part"),
        this->path("notawad.bin"),
        this->path("x.wad"),
        this->path("x.wad.1.tmp"),
        this->path("two words.wad.1.part"),
        std::string(UPLINK) + "x/x.wad.1.part",
        std::string(UPLINK) + ".wad.1.part",   // the directory's name as a prefix: beside it, not in it
        std::string(UPLINK) + "x.wad.1.part",
        std::string(UPLINK) + "/",
        "home/x/doom/wads/uplink/x.wad.1.part",
    };
    U32 seq = 100;
    for (const std::string& dest : bad) {
        Fw::Buffer md = this->metadata(seq++, dest.c_str());
        this->invoke_to_uplinkIn(0, md);
        ASSERT_FALSE(this->lastUplinkReadable()) << dest;  // cfdpManager hands it back unread
    }
    ASSERT_from_uplinkOut_SIZE(bad.size());
    ASSERT_EVENTS_UploadRefused_SIZE(bad.size());
    ASSERT_EVENTS_UploadRefused(0, "/root/.bashrc", GROUND, 100);
    ASSERT_TLM_UPLOADS_REFUSED_SIZE(bad.size());
    ASSERT_TLM_UPLOADS_ACCEPTED_SIZE(0);
    // ... and their FINs (cfdpManager's NAK_LIMIT_REACHED for the Metadata it never got) commit nothing
    Fw::Buffer f = this->fin(100, Cfdp::ConditionCode::CONDITION_CODE_NAK_LIMIT_REACHED,
                             Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_INCOMPLETE,
                             Cfdp::FinFileStatus::FIN_FILE_STATUS_DISCARDED);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut_SIZE(0);
}

void CfdpGuardTester ::testARefusedUploadIsReportedOnce() {
    // The sender resends a Metadata each time the receiver NAKs for it: refused every time, reported once
    for (int i = 0; i < 10; i++) {
        Fw::Buffer md = this->metadata(9, "/etc/hostname");
        this->invoke_to_uplinkIn(0, md);
        ASSERT_FALSE(this->lastUplinkReadable());
    }
    ASSERT_EVENTS_UploadRefused_SIZE(1);
    ASSERT_TLM_UPLOADS_REFUSED(0, 1);
    // Another transaction refused in between, from this source and from another: each is reported once
    Fw::Buffer other = this->metadata(10, "/etc/shadow");
    this->invoke_to_uplinkIn(0, other);
    Fw::Buffer elsewhere = this->metadata(9, "/etc/hostname", Cfdp::Class::CLASS_2, GROUND + 1);
    this->invoke_to_uplinkIn(0, elsewhere);
    Fw::Buffer again = this->metadata(9, "/etc/hostname");
    this->invoke_to_uplinkIn(0, again);
    ASSERT_EVENTS_UploadRefused_SIZE(3);
}

void CfdpGuardTester ::testTheDestinationIsTheStringCfdpManagerOpens() {
    // MetadataPdu ends the name at an embedded NUL, and so does every open: the guard judges that string
    std::string escape = this->path("ok.wad.1.part");
    escape += std::string(1, '\0') + "/../../../../etc/x";
    Fw::Buffer md = this->yamcsMetadata(11, escape);
    this->invoke_to_uplinkIn(0, md);
    ASSERT_TRUE(this->lastUplinkReadable());
    Fw::Buffer f = this->fin(11);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut(0, Fw::String(this->path("ok.wad.1.part").c_str()));

    std::string hidden = std::string("/etc/passwd") + std::string(1, '\0') + this->path("ok.wad.2.part");
    Fw::Buffer md2 = this->yamcsMetadata(12, hidden);
    this->invoke_to_uplinkIn(0, md2);
    ASSERT_FALSE(this->lastUplinkReadable());
    ASSERT_EVENTS_UploadRefused(0, "/etc/passwd", GROUND, 12);
}

void CfdpGuardTester ::testYamcsWidthIdsMatchTheBoardsFin() {
    // Yamcs writes ids in 2 and 4 bytes; F´ answers in the fewest: the guard matches decoded values
    const std::string dest = this->path("cig.wad.1791072224846.part");
    Fw::Buffer md = this->yamcsMetadata(299, dest);
    this->invoke_to_uplinkIn(0, md);
    ASSERT_TRUE(this->lastUplinkReadable());
    Fw::Buffer f = this->fin(299);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut_SIZE(1);
    ASSERT_from_fileAnnounceOut(0, Fw::String(dest.c_str()));
}

void CfdpGuardTester ::testFailedFinsCommitNothing() {
    struct Case {
        Cfdp::ConditionCode cc;
        Cfdp::FinDeliveryCode dc;
        Cfdp::FinFileStatus fs;
    };
    const Case cases[] = {
        // what cfdpManager sends after a checksum failure, the NAK limit, a size error, inactivity, a cancel
        {Cfdp::ConditionCode::CONDITION_CODE_FILE_CHECKSUM_FAILURE, Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_INCOMPLETE,
         Cfdp::FinFileStatus::FIN_FILE_STATUS_DISCARDED},
        {Cfdp::ConditionCode::CONDITION_CODE_NAK_LIMIT_REACHED, Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_INCOMPLETE,
         Cfdp::FinFileStatus::FIN_FILE_STATUS_DISCARDED},
        {Cfdp::ConditionCode::CONDITION_CODE_FILE_SIZE_ERROR, Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_INCOMPLETE,
         Cfdp::FinFileStatus::FIN_FILE_STATUS_DISCARDED},
        {Cfdp::ConditionCode::CONDITION_CODE_INACTIVITY_DETECTED, Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
         Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED},
        {Cfdp::ConditionCode::CONDITION_CODE_CANCEL_REQUEST_RECEIVED,
         Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_INCOMPLETE, Cfdp::FinFileStatus::FIN_FILE_STATUS_DISCARDED},
        // and no error, but not retained, or not complete: never seen from cfdpManager, still not a commit
        {Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR, Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
         Cfdp::FinFileStatus::FIN_FILE_STATUS_DISCARDED},
        {Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR, Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_INCOMPLETE,
         Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED},
    };
    U32 seq = 200;
    for (const Case& c : cases) {
        Fw::Buffer md = this->metadata(seq, this->path("x.wad.1.part").c_str());
        this->invoke_to_uplinkIn(0, md);
        Fw::Buffer bad = this->fin(seq, c.cc, c.dc, c.fs);
        this->invoke_to_downlinkIn(0, bad);
        // the upload is settled: a later NO_ERROR FIN for it (impossible from cfdpManager) commits nothing
        Fw::Buffer good = this->fin(seq);
        this->invoke_to_downlinkIn(0, good);
        seq++;
    }
    ASSERT_from_fileAnnounceOut_SIZE(0);
    ASSERT_EVENTS_UploadNotCommitted_SIZE(sizeof(cases) / sizeof(cases[0]));
    ASSERT_from_downlinkOut_SIZE(2 * sizeof(cases) / sizeof(cases[0]));
}

void CfdpGuardTester ::testATruncatedFinCountsForNothing() {
    // FinPdu starts out reading NO_ERROR / COMPLETE / RETAINED: a FIN that does not decode must not commit
    Fw::Buffer md = this->metadata(13, this->path("t.wad.1.part").c_str());
    this->invoke_to_uplinkIn(0, md);
    Fw::Buffer f = this->fin(13);
    Fw::Buffer cut(f.getData(), f.getSize() - 1);  // the flags byte is gone
    this->invoke_to_downlinkIn(0, cut);
    ASSERT_from_fileAnnounceOut_SIZE(0);
    ASSERT_from_downlinkOut_SIZE(1);
    Fw::Buffer whole = this->fin(13);  // the upload is still followed
    this->invoke_to_downlinkIn(0, whole);
    ASSERT_from_fileAnnounceOut_SIZE(1);
}

void CfdpGuardTester ::testOnlyUploadsLetThroughAreCommitted() {
    Fw::Buffer md = this->metadata(14, this->path("a.wad.1.part").c_str());
    this->invoke_to_uplinkIn(0, md);
    Fw::Buffer other = this->fin(15);  // another transaction
    this->invoke_to_downlinkIn(0, other);
    Fw::Buffer otherBoard = this->fin(14, Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR,
                                      Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
                                      Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED, BOARD + 1);
    this->invoke_to_downlinkIn(0, otherBoard);
    Cfdp::FinPdu towardReceiver;  // not a receiver's FIN at all
    towardReceiver.initialize(Cfdp::PduDirection::DIRECTION_TOWARD_RECEIVER, Cfdp::Class::CLASS_2, GROUND, 14, BOARD,
                              Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR,
                              Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
                              Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED);
    Fw::Buffer wrong = wrap(this->m_down, sizeof this->m_down, towardReceiver);
    this->invoke_to_downlinkIn(0, wrong);
    ASSERT_from_fileAnnounceOut_SIZE(0);
    ASSERT_from_downlinkOut_SIZE(3);
}

void CfdpGuardTester ::testClass1IsLetThroughButNeverCommitted() {
    // Class 1 has no FIN: it may land in the uplink directory, and only COMMIT_WAD (with its checks) puts it in place
    Fw::Buffer md = this->metadata(16, this->path("c1.wad.1.part").c_str(), Cfdp::Class::CLASS_1);
    this->invoke_to_uplinkIn(0, md);
    ASSERT_TRUE(this->lastUplinkReadable());
    // cfdpManager routes a Metadata by its transaction, not its class bit: a class 2 one naming another file for the
    // same transaction would be ignored by cfdpManager, so the guard refuses it rather than follow it
    Fw::Buffer other = this->metadata(16, this->path("c2.wad.1.part").c_str());
    this->invoke_to_uplinkIn(0, other);
    ASSERT_FALSE(this->lastUplinkReadable());
    Fw::Buffer f = this->fin(16);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut_SIZE(0);
    Fw::Buffer away = this->metadata(17, "/tmp/c1.bin", Cfdp::Class::CLASS_1);  // and confined like class 2
    this->invoke_to_uplinkIn(0, away);
    ASSERT_FALSE(this->lastUplinkReadable());
}

void CfdpGuardTester ::testUnreadableMetadataIsRefused() {
    // The sender resends it each time the receiver NAKs for it: refused every time, reported once
    for (int i = 0; i < 2; i++) {
        Fw::Buffer md = this->metadata(18, this->path("u.wad.1.part").c_str());
        Fw::Buffer cut(md.getData(), md.getSize() - 4);  // the destination runs past the end
        this->invoke_to_uplinkIn(0, cut);
        ASSERT_FALSE(this->lastUplinkReadable());
    }
    ASSERT_from_uplinkOut_SIZE(2);
    ASSERT_EVENTS_MetadataUnreadable_SIZE(1);
    ASSERT_TLM_UPLOADS_REFUSED_SIZE(1);
    ASSERT_TLM_UPLOADS_REFUSED(0, 1);
    // Another transaction is reported on its own
    Fw::Buffer other = this->metadata(19, this->path("u.wad.1.part").c_str());
    Fw::Buffer otherCut(other.getData(), other.getSize() - 4);
    this->invoke_to_uplinkIn(0, otherCut);
    ASSERT_FALSE(this->lastUplinkReadable());
    ASSERT_EVENTS_MetadataUnreadable_SIZE(2);
    ASSERT_TLM_UPLOADS_REFUSED(1, 2);
}

void CfdpGuardTester ::testAResendKeepsTheFirstDestination() {
    const std::string first = this->path("r.wad.1.part");
    Fw::Buffer md = this->metadata(19, first.c_str());
    this->invoke_to_uplinkIn(0, md);
    Fw::Buffer same = this->metadata(19, first.c_str());
    this->invoke_to_uplinkIn(0, same);
    ASSERT_TRUE(this->lastUplinkReadable());
    Fw::Buffer moved = this->metadata(19, this->path("s.wad.1.part").c_str());
    this->invoke_to_uplinkIn(0, moved);
    ASSERT_FALSE(this->lastUplinkReadable());
    ASSERT_TLM_UPLOADS_ACCEPTED_SIZE(1);  // one upload, however often its Metadata comes
    Fw::Buffer f = this->fin(19);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut(0, Fw::String(first.c_str()));
}

void CfdpGuardTester ::testOtherTrafficPassesUntouched() {
    U8 bytes[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
    // file data, EOF and a FIN the guard has no upload for, both ways; a buffer that is not a CFDP PDU at all
    Cfdp::FileDataPdu fd;
    fd.initialize(Cfdp::PduDirection::DIRECTION_TOWARD_RECEIVER, Cfdp::Class::CLASS_2, GROUND, 20, BOARD, 0, 16, bytes);
    Cfdp::EofPdu eof;
    eof.initialize(Cfdp::PduDirection::DIRECTION_TOWARD_RECEIVER, Cfdp::Class::CLASS_2, GROUND, 20, BOARD,
                   Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR, 0x1234, 16);
    U8 store[3][128];
    Fw::Buffer ups[] = {wrap(store[0], 128, fd), wrap(store[1], 128, eof)};
    for (Fw::Buffer& b : ups) {
        std::vector<U8> before(b.getData(), b.getData() + b.getSize());
        this->invoke_to_uplinkIn(0, b);
        ASSERT_EQ(0, std::memcmp(b.getData(), before.data(), before.size()));
    }
    U8 telem[4] = {0x00, 0x01, 0xAA, 0xBB};
    Fw::Buffer notFile(telem, sizeof telem);
    this->invoke_to_uplinkIn(0, notFile);
    ASSERT_EQ(0x01, telem[1]);
    ASSERT_from_uplinkOut_SIZE(3);
    Fw::Buffer down = wrap(store[2], 128, fd);
    this->invoke_to_downlinkIn(1, down);
    ASSERT_from_downlinkOut_SIZE(1);
    ASSERT_EQ(std::vector<FwIndexType>{1}, this->m_downlinkPorts);
    ASSERT_EVENTS_SIZE(0);
}

void CfdpGuardTester ::testTheOldestUploadMakesRoom() {
    for (U32 seq = 1; seq <= CfdpGuard::MAX_UPLOADS + 1; seq++) {
        this->upload(seq, this->path(("n" + std::to_string(seq) + ".wad.1.part").c_str()));
    }
    ASSERT_EVENTS_UploadForgotten_SIZE(1);
    ASSERT_EVENTS_UploadForgotten(0, this->path("n1.wad.1.part").c_str(), GROUND, 1);
    Fw::Buffer first = this->fin(1);
    this->invoke_to_downlinkIn(0, first);
    ASSERT_from_fileAnnounceOut_SIZE(0);
    Fw::Buffer last = this->fin(CfdpGuard::MAX_UPLOADS + 1);
    this->invoke_to_downlinkIn(0, last);
    ASSERT_from_fileAnnounceOut_SIZE(1);
    // Upload 9 has ended and holds the slot upload 1 had: a new one takes that slot without pushing anyone out,
    // and the next one pushes out the oldest still waiting (upload 2), not whoever holds the first slot
    this->clearHistory();
    this->upload(10, this->path("n10.wad.1.part"));
    ASSERT_EVENTS_UploadForgotten_SIZE(0);
    this->upload(11, this->path("n11.wad.1.part"));
    ASSERT_EVENTS_UploadForgotten_SIZE(1);
    ASSERT_EVENTS_UploadForgotten(0, this->path("n2.wad.1.part").c_str(), GROUND, 2);
    Fw::Buffer f10 = this->fin(10);
    this->invoke_to_downlinkIn(0, f10);
    ASSERT_from_fileAnnounceOut_SIZE(1);
    ASSERT_from_fileAnnounceOut(0, Fw::String(this->path("n10.wad.1.part").c_str()));
}

void CfdpGuardTester ::testTheSourceIsPartOfTheTransaction() {
    // cfdpManager keys a transaction by (source, sequence number): two sources may use the same number at once
    const std::string a = this->path("a.wad.1.part");
    const std::string b = this->path("b.wad.1.part");
    this->upload(30, a);
    Fw::Buffer otherFin = this->fin(30, Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR,
                                    Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
                                    Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED, BOARD, GROUND + 1);
    this->invoke_to_downlinkIn(0, otherFin);
    ASSERT_from_fileAnnounceOut_SIZE(0);  // another source's transaction: not this upload
    Fw::Buffer md = this->metadata(30, b.c_str(), Cfdp::Class::CLASS_2, GROUND + 1);
    this->invoke_to_uplinkIn(0, md);
    ASSERT_TRUE(this->lastUplinkReadable());  // not a resend of (GROUND, 30) with another file: its own upload
    ASSERT_TLM_UPLOADS_ACCEPTED_SIZE(2);
    Fw::Buffer finB = this->fin(30, Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR,
                                Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
                                Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED, BOARD, GROUND + 1);
    this->invoke_to_downlinkIn(0, finB);
    Fw::Buffer finA = this->fin(30);
    this->invoke_to_downlinkIn(0, finA);
    ASSERT_from_fileAnnounceOut_SIZE(2);
    ASSERT_from_fileAnnounceOut(0, Fw::String(b.c_str()));
    ASSERT_from_fileAnnounceOut(1, Fw::String(a.c_str()));
}

void CfdpGuardTester ::testALateMetadataAfterTheFinChangesNothing() {
    // When the first Metadata is lost, the receiver NAKs for it every ack_timer and the sender answers each NAK: on a
    // slow link a copy can come after the FIN. cfdpManager drops it; so must the guard, and F´'s repeated FIN (its
    // ACK lost) must not commit the file a second time
    const std::string a = this->path("late.wad.1.part");
    this->upload(40, a);
    Fw::Buffer f = this->fin(40);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut_SIZE(1);
    Fw::Buffer late = this->metadata(40, a.c_str());
    this->invoke_to_uplinkIn(0, late);
    ASSERT_TRUE(this->lastUplinkReadable());  // the same file: let through, as cfdpManager would ignore it anyway
    ASSERT_TLM_UPLOADS_ACCEPTED_SIZE(1);
    Fw::Buffer repeat = this->fin(40);
    this->invoke_to_downlinkIn(0, repeat);
    ASSERT_from_fileAnnounceOut_SIZE(1);
    ASSERT_EVENTS_UploadCommitted_SIZE(1);
    // ... and a Metadata naming another file for that transaction never becomes something a FIN commits
    Fw::Buffer moved = this->metadata(40, this->path("other.wad.1.part").c_str());
    this->invoke_to_uplinkIn(0, moved);
    ASSERT_FALSE(this->lastUplinkReadable());
    Fw::Buffer again = this->fin(40);
    this->invoke_to_downlinkIn(0, again);
    ASSERT_from_fileAnnounceOut_SIZE(1);
}

void CfdpGuardTester ::testACancelledUploadMakesRoomFirst() {
    // A cancel from the sender ends the transaction with no FIN; its record goes before any upload still arriving
    this->upload(50, this->path("cancelled.wad.1.part"));
    Fw::Buffer e = this->eof(50, Cfdp::ConditionCode::CONDITION_CODE_CANCEL_REQUEST_RECEIVED);
    std::vector<U8> before(e.getData(), e.getData() + e.getSize());
    this->invoke_to_uplinkIn(0, e);
    ASSERT_EQ(0, std::memcmp(e.getData(), before.data(), before.size()));  // passed on unchanged
    ASSERT_TRUE(this->lastUplinkReadable());
    for (U32 seq = 51; seq < 51 + CfdpGuard::MAX_UPLOADS; seq++) {
        this->upload(seq, this->path(("k" + std::to_string(seq) + ".wad.1.part").c_str()));
    }
    ASSERT_EVENTS_UploadForgotten_SIZE(0);
    // A normal EOF ends nothing: the next upload pushes out the oldest still waiting for its FIN
    Fw::Buffer normal = this->eof(51, Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR);
    this->invoke_to_uplinkIn(0, normal);
    this->upload(60, this->path("k60.wad.1.part"));
    ASSERT_EVENTS_UploadForgotten_SIZE(1);
    ASSERT_EVENTS_UploadForgotten(0, this->path("k51.wad.1.part").c_str(), GROUND, 51);
}

void CfdpGuardTester ::testMetadataForAnotherEntityNeverPushesOutAnUpload() {
    // cfdpManager starts no receive for a Metadata addressed elsewhere, and class 1 has no FIN: neither may push out an
    // upload still waiting for its FIN
    const std::string live = this->path("live.wad.1.part");
    this->upload(70, live);
    for (U32 seq = 71; seq < 71 + 2 * CfdpGuard::MAX_UPLOADS; seq++) {
        const std::string dest = this->path(("e" + std::to_string(seq) + ".wad.1.part").c_str());
        Fw::Buffer md = (seq % 2) ? this->metadata(seq, dest.c_str(), Cfdp::Class::CLASS_2, GROUND, BOARD + 1)
                                  : this->metadata(seq, dest.c_str(), Cfdp::Class::CLASS_1);
        this->invoke_to_uplinkIn(0, md);
        ASSERT_TRUE(this->lastUplinkReadable());
    }
    ASSERT_EVENTS_UploadForgotten_SIZE(0);
    Fw::Buffer f = this->fin(70);
    this->invoke_to_downlinkIn(0, f);
    ASSERT_from_fileAnnounceOut_SIZE(1);
    ASSERT_from_fileAnnounceOut(0, Fw::String(live.c_str()));
}

}  // namespace DoomMission
