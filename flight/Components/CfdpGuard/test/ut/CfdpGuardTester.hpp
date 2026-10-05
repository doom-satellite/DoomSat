// ======================================================================
// \title  CfdpGuardTester.hpp
// \brief  GTest harness for CfdpGuard (scripts/flight.sh ut)
// ======================================================================

#ifndef DoomMission_CfdpGuardTester_HPP
#define DoomMission_CfdpGuardTester_HPP

#include "DoomMission/Components/CfdpGuard/CfdpGuard.hpp"
#include "DoomMission/Components/CfdpGuard/CfdpGuardGTestBase.hpp"

#include <Svc/Ccsds/CfdpManager/Types/PduBase.hpp>

#include <string>
#include <vector>

namespace DoomMission {

class CfdpGuardTester final : public CfdpGuardGTestBase {
  public:
    static const FwSizeType MAX_HISTORY_SIZE = 40;
    static const FwEnumStoreType TEST_INSTANCE_ID = 0;
    static constexpr U32 GROUND = 100;  //!< the ground's entity id (Yamcs)
    static constexpr U32 BOARD = 42;    //!< cfdpManager's LocalEid
    static const char* const UPLINK;    //!< the uplink directory the tests set DOOMSAT_HOME for

    CfdpGuardTester();
    ~CfdpGuardTester();

    void testAnUploadToTheUplinkDirectoryPassesUnchanged();
    void testItsFinCommitsItOnce();
    void testEveryOtherDestinationIsRefused();
    void testARefusedUploadIsReportedOnce();
    void testTheDestinationIsTheStringCfdpManagerOpens();
    void testYamcsWidthIdsMatchTheBoardsFin();
    void testFailedFinsCommitNothing();
    void testATruncatedFinCountsForNothing();
    void testOnlyUploadsLetThroughAreCommitted();
    void testClass1IsLetThroughButNeverCommitted();
    void testUnreadableMetadataIsRefused();
    void testAResendKeepsTheFirstDestination();
    void testOtherTrafficPassesUntouched();
    void testTheOldestUploadMakesRoom();
    void testTheSourceIsPartOfTheTransaction();
    void testALateMetadataAfterTheFinChangesNothing();
    void testACancelledUploadMakesRoomFirst();
    void testMetadataForAnotherEntityNeverPushesOutAnUpload();

  private:
    void connectPorts();
    void initComponents();

    //! FW_PACKET_FILE descriptor + Metadata PDU (F´'s own encoder: the fewest bytes for each id), as
    //! fprimeRouter.fileOut delivers it
    Fw::Buffer metadata(U32 seq,
                        const char* dest,
                        Svc::Ccsds::Cfdp::Class::T txm = Svc::Ccsds::Cfdp::Class::CLASS_2,
                        U32 src = GROUND,
                        U32 dst = BOARD);
    //! The same, encoded the way Yamcs encodes it: entity ids in 2 bytes, the sequence number in 4
    Fw::Buffer yamcsMetadata(U32 seq, const std::string& dest);
    //! FW_PACKET_FILE descriptor + the receiver's FIN, as cfdpManager.dataOut sends it
    Fw::Buffer fin(U32 seq,
                   Svc::Ccsds::Cfdp::ConditionCode cc = Svc::Ccsds::Cfdp::ConditionCode::CONDITION_CODE_NO_ERROR,
                   Svc::Ccsds::Cfdp::FinDeliveryCode dc = Svc::Ccsds::Cfdp::FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE,
                   Svc::Ccsds::Cfdp::FinFileStatus fs = Svc::Ccsds::Cfdp::FinFileStatus::FIN_FILE_STATUS_RETAINED,
                   U32 dst = BOARD,
                   U32 src = GROUND);
    //! FW_PACKET_FILE descriptor + the sender's EOF, as fprimeRouter.fileOut delivers it
    Fw::Buffer eof(U32 seq, Svc::Ccsds::Cfdp::ConditionCode cc);
    //! A Metadata to the uplink directory, sent up
    void upload(U32 seq, const std::string& dest);
    //! Whether the last buffer sent on uplinkOut still reads as a CFDP PDU to cfdpManager
    bool lastUplinkReadable();
    std::string path(const char* name) const;

    //! The port each buffer left on (the generated history keeps only the buffer), and how many files had been
    //! announced by then
    void from_downlinkOut_handler(FwIndexType portNum, Fw::Buffer& fwBuffer) override;
    std::vector<FwIndexType> m_downlinkPorts;
    std::vector<U32> m_announcedAtDownlink;

    CfdpGuard component;
    U8 m_up[600];
    U8 m_down[100];
};

}  // namespace DoomMission

#endif
