// ======================================================================
// \title  CfdpGuardTestMain.cpp
// \brief  CfdpGuard unit tests (scripts/flight.sh ut)
// ======================================================================

#include "CfdpGuardTester.hpp"

#define GUARD_TEST(group, name)          \
    TEST(group, name) {                  \
        DoomMission::CfdpGuardTester t;  \
        t.test##name();                  \
    }

GUARD_TEST(Uplink, AnUploadToTheUplinkDirectoryPassesUnchanged)
GUARD_TEST(Uplink, EveryOtherDestinationIsRefused)
GUARD_TEST(Uplink, ARefusedUploadIsReportedOnce)
GUARD_TEST(Uplink, TheDestinationIsTheStringCfdpManagerOpens)
GUARD_TEST(Uplink, UnreadableMetadataIsRefused)
GUARD_TEST(Uplink, AResendKeepsTheFirstDestination)
GUARD_TEST(Uplink, OtherTrafficPassesUntouched)
GUARD_TEST(Commit, ItsFinCommitsItOnce)
GUARD_TEST(Commit, YamcsWidthIdsMatchTheBoardsFin)
GUARD_TEST(Commit, FailedFinsCommitNothing)
GUARD_TEST(Commit, ATruncatedFinCountsForNothing)
GUARD_TEST(Commit, OnlyUploadsLetThroughAreCommitted)
GUARD_TEST(Commit, Class1IsLetThroughButNeverCommitted)
GUARD_TEST(Commit, TheOldestUploadMakesRoom)
GUARD_TEST(Commit, TheSourceIsPartOfTheTransaction)
GUARD_TEST(Commit, ALateMetadataAfterTheFinChangesNothing)
GUARD_TEST(Commit, ACancelledUploadMakesRoomFirst)
GUARD_TEST(Commit, MetadataForAnotherEntityNeverPushesOutAnUpload)

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
