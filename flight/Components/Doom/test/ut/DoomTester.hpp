// ======================================================================
// \title  DoomTester.hpp
// \brief  GTest harness for the Doom component's WAD handling (scripts/flight.sh ut)
// ======================================================================

#ifndef DoomMission_DoomTester_HPP
#define DoomMission_DoomTester_HPP

#include "DoomMission/Components/Doom/Doom.hpp"
#include "DoomMission/Components/Doom/DoomGTestBase.hpp"

#include <string>
#include <vector>

namespace DoomMission {

class DoomTester final : public DoomGTestBase {
  public:
    static const FwSizeType MAX_HISTORY_SIZE = 20;
    static const FwEnumStoreType TEST_INSTANCE_ID = 0;
    static const FwSizeType TEST_INSTANCE_QUEUE_DEPTH = 10;
    static const char* const PART;  //!< the name the ground uplinks basic.wad under

    DoomTester();
    ~DoomTester();

    void testACommitPutsTheCheckedFileInPlace();
    void testACommitSentAgainAnswersAsTheFirstDid();
    void testACommitAfterTheGuardsAnswersAsTheGuardDid();
    void testAnOlderFileOfTheSameNameIsNotTakenForThisOne();
    void testARenameThatFailsIsOneFailure();
    void testOnlyThisUploadAnswersARepeat();
    void testNothingOnBoardIsAFailure();
    void testAPartThatIsNotTheFileSentStaysAPart();
    void testACommitNamesABareUplinkName();
    void testAnnouncedFilesOutsideTheUplinkDirectoryStayWhereTheyAre();
    void testWadReports();

  private:
    void connectPorts();
    void initComponents();

    std::string uplink(const std::string& name) const;  //!< the path of a file in the uplink directory
    void write(const std::string& path, const std::vector<U8>& bytes);
    bool exists(const std::string& path) const;
    std::vector<U8> read(const std::string& path) const;
    static U32 checksum(const std::vector<U8>& bytes);  //!< the CFDP modular checksum, as the ground sends it
    //! COMMIT_WAD(part, what the ground sent), dispatched
    void commit(const char* part, const std::vector<U8>& sent, U32 cmdSeq);
    void assertAnswer(U32 cmdSeq, Fw::CmdResponse response);
    //! A WAD report (record kind 3) from the payload, as drainSocket hands it on
    void report(U8 result, U16 loads, const char* name, const char* map, const char* reason = "");

    Doom component;
    std::string m_home;
    std::vector<U8> m_wad;  //!< the file the ground sends
};

}  // namespace DoomMission

#endif
