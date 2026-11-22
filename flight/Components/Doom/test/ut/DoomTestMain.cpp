// ======================================================================
// \title  DoomTestMain.cpp
// \brief  The Doom component's WAD handling: COMMIT_WAD, fileAnnounce and the payload's WAD reports
//         (scripts/flight.sh ut)
// ======================================================================

#include "DoomTester.hpp"

#define DOOM_TEST(group, name)      \
    TEST(group, name) {             \
        DoomMission::DoomTester t;  \
        t.test##name();             \
    }

DOOM_TEST(Commit, ACommitPutsTheCheckedFileInPlace)
DOOM_TEST(Commit, ACommitSentAgainAnswersAsTheFirstDid)
DOOM_TEST(Commit, ACommitAfterTheGuardsAnswersAsTheGuardDid)
DOOM_TEST(Commit, AnOlderFileOfTheSameNameIsNotTakenForThisOne)
DOOM_TEST(Commit, ARenameThatFailsIsOneFailure)
DOOM_TEST(Commit, OnlyThisUploadAnswersARepeat)
DOOM_TEST(Commit, NothingOnBoardIsAFailure)
DOOM_TEST(Commit, APartThatIsNotTheFileSentStaysAPart)
DOOM_TEST(Commit, ACommitNamesABareUplinkName)
DOOM_TEST(Announce, AnnouncedFilesOutsideTheUplinkDirectoryStayWhereTheyAre)
DOOM_TEST(Load, WadReports)

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
