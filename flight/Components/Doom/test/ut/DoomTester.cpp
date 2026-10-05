// ======================================================================
// \title  DoomTester.cpp
// \brief  COMMIT_WAD and fileAnnounce against real files in a scratch $DOOMSAT_HOME, and the payload's WAD reports
// ======================================================================

#include "DoomTester.hpp"

#include "CFDP/Checksum/Checksum.hpp"

#include <algorithm>
#include <ftw.h>
#include <sys/stat.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>

namespace DoomMission {

const char* const DoomTester::PART = "basic.wad.1700000000000.part";

namespace {

// The payload's WAD report results (payload/wad_uplink.py REPORT, LOADED, FAILED, ALREADY)
constexpr U8 REPORT = 0;
constexpr U8 LOADED = 1;
constexpr U8 FAILED = 2;
constexpr U8 ALREADY = 3;

int removeOne(const char* path, const struct stat*, int, struct FTW*) {
    return std::remove(path);
}

void text(std::vector<U8>& out, const char* s) {
    const size_t n = std::strlen(s);
    out.push_back(static_cast<U8>(n));
    out.insert(out.end(), s, s + n);
}

}  // namespace

DoomTester ::DoomTester() : DoomGTestBase("DoomTester", DoomTester::MAX_HISTORY_SIZE), component("Doom") {
    char scratch[] = "/tmp/doom-ut.XXXXXX";
    const char* const made = ::mkdtemp(scratch);
    EXPECT_NE(nullptr, made);
    this->m_home = (made != nullptr) ? made : "/tmp/doom-ut";
    setenv("DOOMSAT_HOME", (this->m_home + "/").c_str(), 1);  // a trailing '/' as a user might write it
    EXPECT_EQ(0, ::mkdir((this->m_home + "/wads").c_str(), 0755));
    EXPECT_EQ(0, ::mkdir((this->m_home + "/wads/uplink").c_str(), 0755));
    for (U32 i = 0; i < 2704; i++) {
        this->m_wad.push_back(static_cast<U8>((i * 7 + 3) % 256));
    }
    this->initComponents();
    this->connectPorts();
}

DoomTester ::~DoomTester() {
    this->component.deinit();  // its message queue
    (void)::nftw(this->m_home.c_str(), removeOne, 8, FTW_DEPTH | FTW_PHYS);
}

std::string DoomTester ::uplink(const std::string& name) const {
    return this->m_home + "/wads/uplink/" + name;
}

void DoomTester ::write(const std::string& path, const std::vector<U8>& bytes) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    f.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    ASSERT_TRUE(f.good()) << path;
}

bool DoomTester ::exists(const std::string& path) const {
    struct stat st;
    return ::stat(path.c_str(), &st) == 0;
}

std::vector<U8> DoomTester ::read(const std::string& path) const {
    std::ifstream f(path, std::ios::binary);
    return std::vector<U8>(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
}

U32 DoomTester ::checksum(const std::vector<U8>& bytes) {
    CFDP::Checksum sum;
    sum.update(bytes.data(), 0, static_cast<U32>(bytes.size()));
    return sum.getValue();
}

void DoomTester ::commit(const char* part, const std::vector<U8>& sent, U32 cmdSeq) {
    this->sendCmd_COMMIT_WAD(0, cmdSeq, Fw::CmdStringArg(part), static_cast<U32>(sent.size()), checksum(sent));
    ASSERT_EQ(Fw::QueuedComponentBase::MSG_DISPATCH_OK, this->component.doDispatch());
}

void DoomTester ::assertAnswer(U32 cmdSeq, Fw::CmdResponse response) {
    ASSERT_CMD_RESPONSE_SIZE(1);
    ASSERT_CMD_RESPONSE(0, DoomComponentBase::OPCODE_COMMIT_WAD, cmdSeq, response);
}

void DoomTester ::report(U8 result, U16 loads, const char* name, const char* map, const char* reason) {
    std::vector<U8> body = {result, static_cast<U8>(loads >> 8), static_cast<U8>(loads)};
    text(body, "freedoom2.wad");  // the IWAD the game runs now
    text(body, "basic.wad");      // the PWAD over it
    text(body, name);
    text(body, map);
    text(body, reason);
    this->component.handleMessage(3, body.data(), static_cast<U16>(body.size()));
}

// ----------------------------------------------------------------------
// COMMIT_WAD
// ----------------------------------------------------------------------

void DoomTester ::testACommitPutsTheCheckedFileInPlace() {
    this->write(this->uplink(PART), this->m_wad);
    this->commit(PART, this->m_wad, 1);
    this->assertAnswer(1, Fw::CmdResponse::OK);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinked(0, this->uplink("basic.wad").c_str());
    EXPECT_FALSE(this->exists(this->uplink(PART)));
    EXPECT_EQ(this->m_wad, this->read(this->uplink("basic.wad")));
}

void DoomTester ::testACommitSentAgainAnswersAsTheFirstDid() {
    // The first commit worked and its WadUplinked was lost on the way down, so the ground asks again
    this->write(this->uplink(PART), this->m_wad);
    this->commit(PART, this->m_wad, 1);
    this->assertAnswer(1, Fw::CmdResponse::OK);
    this->clearHistory();
    for (U32 cmdSeq = 2; cmdSeq <= 3; cmdSeq++) {
        this->commit(PART, this->m_wad, cmdSeq);
        this->assertAnswer(cmdSeq, Fw::CmdResponse::OK);
        ASSERT_EVENTS_SIZE(1);
        ASSERT_EVENTS_WadUplinked(0, this->uplink("basic.wad").c_str());
        this->clearHistory();
    }
    EXPECT_EQ(this->m_wad, this->read(this->uplink("basic.wad")));
}

void DoomTester ::testACommitAfterTheGuardsAnswersAsTheGuardDid() {
    // cfdpGuard committed the file at its FIN (fileAnnounce), and its WadUplinked was lost: COMMIT_WAD is the fallback
    this->write(this->uplink(PART), this->m_wad);
    Fw::String announced(this->uplink(PART).c_str());
    this->invoke_to_fileAnnounce(0, announced);
    ASSERT_EVENTS_WadUplinked_SIZE(1);
    this->clearHistory();
    this->commit(PART, this->m_wad, 4);
    this->assertAnswer(4, Fw::CmdResponse::OK);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinked(0, this->uplink("basic.wad").c_str());
}

void DoomTester ::testAnOlderFileOfTheSameNameIsNotTakenForThisOne() {
    // The upload never arrived, and NAME.wad is an older upload: other bytes, or another size
    std::vector<U8> otherBytes(this->m_wad);
    otherBytes[100] = static_cast<U8>(otherBytes[100] + 1);
    std::vector<U8> otherSize(this->m_wad);  // four zero bytes more: another size, the same modular checksum
    otherSize.insert(otherSize.end(), 4, 0);
    ASSERT_EQ(checksum(this->m_wad), checksum(otherSize));
    for (const std::vector<U8>* older : {&otherBytes, &otherSize}) {
        this->write(this->uplink("basic.wad"), *older);
        this->commit(PART, this->m_wad, 5);
        this->assertAnswer(5, Fw::CmdResponse::EXECUTION_ERROR);
        ASSERT_EVENTS_SIZE(1);
        ASSERT_EVENTS_WadUplinkFailed(0, this->uplink(PART).c_str());
        EXPECT_EQ(*older, this->read(this->uplink("basic.wad")));
        this->clearHistory();
    }
}

void DoomTester ::testARenameThatFailsIsOneFailure() {
    // The .part checks out but cannot be renamed (here NAME.wad is a directory): one WadUplinkFailed, the .part kept
    this->write(this->uplink(PART), this->m_wad);
    ASSERT_EQ(0, ::mkdir(this->uplink("basic.wad").c_str(), 0755));
    this->commit(PART, this->m_wad, 9);
    this->assertAnswer(9, Fw::CmdResponse::EXECUTION_ERROR);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinkFailed(0, this->uplink(PART).c_str());
    EXPECT_EQ(this->m_wad, this->read(this->uplink(PART)));
}

void DoomTester ::testOnlyThisUploadAnswersARepeat() {
    // An older NAME.wad with the words of the file sent in another order (two 8-byte runs swapped: the same size and
    // modular checksum, as reordered lumps in a WAD would give), and no .part: not this upload
    std::vector<U8> reordered(this->m_wad);
    std::swap_ranges(reordered.begin() + 1000, reordered.begin() + 1008, reordered.begin() + 1016);
    ASSERT_NE(this->m_wad, reordered);
    ASSERT_EQ(checksum(this->m_wad), checksum(reordered));
    this->write(this->uplink("basic.wad"), reordered);
    this->commit(PART, this->m_wad, 10);
    this->assertAnswer(10, Fw::CmdResponse::EXECUTION_ERROR);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinkFailed(0, this->uplink(PART).c_str());
    EXPECT_EQ(reordered, this->read(this->uplink("basic.wad")));
    this->clearHistory();
    // This upload committed for real; then a COMMIT_WAD for another upload of the same bytes, which never arrived
    this->write(this->uplink(PART), this->m_wad);
    this->commit(PART, this->m_wad, 11);
    this->assertAnswer(11, Fw::CmdResponse::OK);
    this->clearHistory();
    this->commit("basic.wad.1700000000001.part", this->m_wad, 12);
    this->assertAnswer(12, Fw::CmdResponse::EXECUTION_ERROR);
    ASSERT_EVENTS_WadUplinkFailed_SIZE(1);
    this->clearHistory();
    // ... while this upload's own repeat is still answered, until another upload of NAME is put in place
    this->commit(PART, this->m_wad, 13);
    this->assertAnswer(13, Fw::CmdResponse::OK);
    this->clearHistory();
    // ... and only for the size and checksum it was put in place with: the same size, another checksum, is not it
    std::vector<U8> otherBytes(this->m_wad);
    otherBytes[100] = static_cast<U8>(otherBytes[100] + 1);
    ASSERT_NE(checksum(this->m_wad), checksum(otherBytes));
    this->commit(PART, otherBytes, 17);
    this->assertAnswer(17, Fw::CmdResponse::EXECUTION_ERROR);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinkFailed(0, this->uplink(PART).c_str());
    EXPECT_EQ(this->m_wad, this->read(this->uplink("basic.wad")));
    this->clearHistory();
    // ... but not once NAME.wad has changed since, even to the same checksum (four zero bytes more)
    std::vector<U8> grown(this->m_wad);
    grown.insert(grown.end(), 4, 0);
    this->write(this->uplink("basic.wad"), grown);
    this->commit(PART, this->m_wad, 16);
    this->assertAnswer(16, Fw::CmdResponse::EXECUTION_ERROR);
    this->clearHistory();
    const char* const next = "basic.wad.1700000000002.part";
    this->write(this->uplink(next), this->m_wad);
    this->commit(next, this->m_wad, 14);
    this->assertAnswer(14, Fw::CmdResponse::OK);
    this->clearHistory();
    this->commit(PART, this->m_wad, 15);
    this->assertAnswer(15, Fw::CmdResponse::EXECUTION_ERROR);
}

void DoomTester ::testNothingOnBoardIsAFailure() {
    this->commit(PART, this->m_wad, 6);
    this->assertAnswer(6, Fw::CmdResponse::EXECUTION_ERROR);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinkFailed(0, this->uplink(PART).c_str());
    EXPECT_FALSE(this->exists(this->uplink("basic.wad")));
}

void DoomTester ::testAPartThatIsNotTheFileSentStaysAPart() {
    // A damaged or unfinished .part is refused, even when a NAME.wad with the bytes sent is already there: the
    // command names the .part, and the .part decides
    std::vector<U8> damaged(this->m_wad);
    damaged[0] = static_cast<U8>(damaged[0] ^ 0xFF);
    this->write(this->uplink(PART), damaged);
    for (const bool alsoInPlace : {false, true}) {
        if (alsoInPlace) {
            this->write(this->uplink("basic.wad"), this->m_wad);
        }
        this->commit(PART, this->m_wad, 7);
        this->assertAnswer(7, Fw::CmdResponse::EXECUTION_ERROR);
        ASSERT_EVENTS_SIZE(1);
        ASSERT_EVENTS_WadCommitRefused_SIZE(1);
        EXPECT_EQ(damaged, this->read(this->uplink(PART)));
        this->clearHistory();
    }
    // Another size with the same modular checksum (four zero bytes more) is refused too: the size is checked
    std::vector<U8> padded(this->m_wad);
    padded.insert(padded.end(), 4, 0);
    ASSERT_EQ(checksum(this->m_wad), checksum(padded));
    this->write(this->uplink(PART), padded);
    ::remove(this->uplink("basic.wad").c_str());
    this->commit(PART, this->m_wad, 8);
    this->assertAnswer(8, Fw::CmdResponse::EXECUTION_ERROR);
    ASSERT_EVENTS_WadCommitRefused_SIZE(1);
    EXPECT_EQ(padded, this->read(this->uplink(PART)));
    EXPECT_FALSE(this->exists(this->uplink("basic.wad")));
}

void DoomTester ::testACommitNamesABareUplinkName() {
    this->write(this->m_home + "/basic.wad.1.part", this->m_wad);
    for (const char* bad : {"../basic.wad.1.part", "basic.bin.1.part", "basic.wad", ""}) {
        this->sendCmd_COMMIT_WAD(0, 8, Fw::CmdStringArg(bad), static_cast<U32>(this->m_wad.size()),
                                 checksum(this->m_wad));
        ASSERT_EQ(Fw::QueuedComponentBase::MSG_DISPATCH_OK, this->component.doDispatch());
        this->assertAnswer(8, Fw::CmdResponse::VALIDATION_ERROR);
        ASSERT_EVENTS_WadUplinkFailed_SIZE(1);
        ASSERT_EVENTS_WadUplinked_SIZE(0);
        this->clearHistory();
    }
    EXPECT_TRUE(this->exists(this->m_home + "/basic.wad.1.part"));
    EXPECT_FALSE(this->exists(this->m_home + "/basic.wad"));
}

// ----------------------------------------------------------------------
// fileAnnounce
// ----------------------------------------------------------------------

void DoomTester ::testAnnouncedFilesOutsideTheUplinkDirectoryStayWhereTheyAre() {
    const std::string outside = this->m_home + "/basic.wad.1.part";
    this->write(outside, this->m_wad);
    Fw::String announced(outside.c_str());
    this->invoke_to_fileAnnounce(0, announced);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadUplinkFailed(0, outside.c_str());
    EXPECT_TRUE(this->exists(outside));
    EXPECT_FALSE(this->exists(this->m_home + "/basic.wad"));
    this->clearHistory();
    // Any other file a transfer delivers is none of this component's business
    Fw::String other(this->uplink("notes.txt").c_str());
    this->invoke_to_fileAnnounce(0, other);
    ASSERT_EVENTS_SIZE(0);
}

// ----------------------------------------------------------------------
// The payload's WAD reports
// ----------------------------------------------------------------------

void DoomTester ::testWadReports() {
    // ALREADY: a LOAD_WAD for the game already flying. An answer of its own (WadLoaded means a switch), the count as
    // it was, and the level goes on (no new level announced)
    this->component.m_lastLevel = 3;
    this->report(ALREADY, 1, "basic.wad over freedoom2.wad", "MAP01");
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadAlreadyFlying(0, "basic.wad over freedoom2.wad", "MAP01");
    ASSERT_EVENTS_WadLoaded_SIZE(0);
    ASSERT_TLM_WAD_LOADS_SIZE(1);
    ASSERT_TLM_WAD_LOADS(0, 1);
    EXPECT_EQ(3, this->component.m_lastLevel);
    this->clearHistory();

    // LOADED: a switch. The new WAD starts at level 1, so the level is announced again
    this->report(LOADED, 2, "basic.wad over freedoom2.wad", "MAP01");
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadLoaded(0, "basic.wad over freedoom2.wad", "MAP01");
    ASSERT_TLM_WAD_LOADS(0, 2);
    EXPECT_EQ(0, this->component.m_lastLevel);
    this->clearHistory();

    this->report(FAILED, 2, "nothere.wad", "E1M1", "nothere.wad is in neither the uplink nor the installed WAD directory");
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_WadLoadFailed(0, "nothere.wad",
                                "nothere.wad is in neither the uplink nor the installed WAD directory");
    this->clearHistory();

    // REPORT: what the game runs, sent when the link comes up; no event
    this->report(REPORT, 2, "", "");
    ASSERT_EVENTS_SIZE(0);
    ASSERT_TLM_WAD_LOADS(0, 2);
}

}  // namespace DoomMission
