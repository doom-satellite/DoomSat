"""cfdpGuard, pinned as text: where it sits, what it reads PDUs with, and the name rule it shares with Doom.

Its behaviour is pinned by its own GTest suite (flight/Components/CfdpGuard/test/ut, `scripts/flight.sh ut`), which
needs the F´ install. These checks need nothing: they read the topology, the sources and the scripts. One also reads
F´ itself when the install is there (skipped otherwise), because the guard refuses a Metadata by relying on
cfdpManager to hand back, unread, a buffer whose descriptor is not FW_PACKET_FILE.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


TOPOLOGY = _read("flight", "DoomSat", "Top", "topology.fpp")
INSTANCES = _read("flight", "DoomSat", "Top", "instances.fpp")
GUARD_CPP = _read("flight", "Components", "CfdpGuard", "CfdpGuard.cpp")
GUARD_FPP = _read("flight", "Components", "CfdpGuard", "CfdpGuard.fpp")
GUARD_HPP = _read("flight", "Components", "CfdpGuard", "CfdpGuard.hpp")
DOOM_CPP = _read("flight", "Components", "Doom", "Doom.cpp")
NUM_CHANNELS = int(re.search(r"constant NumChannels = (\d+)", _read("flight", "config", "CfdpCfg.fpp")).group(1))
FPRIME = os.path.join(os.environ.get("DOOMSAT_HOME", os.path.expanduser("~/doom")), "DoomSat", "lib", "fprime")


def connections():
    return {tuple(m) for m in re.findall(r"^\s*([\w.\[\]]+)\s*->\s*([\w.\[\]]+)\s*$", TOPOLOGY, re.M)}


class TestWhereTheGuardSits(unittest.TestCase):
    def test_every_uplinked_pdu_passes_through_it(self):
        c = connections()
        self.assertIn(("ComCcsds.fprimeRouter.fileOut", "cfdpGuard.uplinkIn"), c)
        self.assertIn(("cfdpGuard.uplinkOut", "cfdpManager.dataIn[0]"), c)
        self.assertFalse([x for x in c if x[0] == "ComCcsds.fprimeRouter.fileOut" and x[1] != "cfdpGuard.uplinkIn"],
                         "nothing else may feed cfdpManager past the guard")
        self.assertFalse([x for x in c if x[1].startswith("cfdpManager.dataIn") and x[0] != "cfdpGuard.uplinkOut"])

    def test_every_downlinked_pdu_passes_through_it(self):
        c = connections()
        for ch in range(NUM_CHANNELS):
            self.assertIn((f"cfdpManager.dataOut[{ch}]", f"cfdpGuard.downlinkIn[{ch}]"), c)
            self.assertIn((f"cfdpGuard.downlinkOut[{ch}]",
                           "ComCcsds.comQueue.bufferQueueIn[ComCcsds.Ports_ComBufferQueue.FILE]"), c)
        self.assertFalse([x for x in c if x[0].startswith("cfdpManager.dataOut") and not x[1].startswith("cfdpGuard.")])

    def test_it_commits_through_the_doom_component(self):
        self.assertIn(("cfdpGuard.fileAnnounceOut", "doom.fileAnnounce"), connections())
        self.assertTrue(re.search(r"^\s*instance cfdpGuard\s*$", TOPOLOGY, re.M))
        self.assertRegex(INSTANCES, r"instance cfdpGuard: DoomMission\.CfdpGuard base id 0x[0-9A-Fa-f]+")

    def test_its_ports_cannot_take_the_routers_lock_again(self):
        # Sync, not guarded: the router calls in while holding its own guarded mutex, and the guard never calls back
        self.assertRegex(GUARD_FPP, r"sync input port uplinkIn: Fw\.BufferSend")
        self.assertRegex(GUARD_FPP, r"sync input port downlinkIn: \[Svc\.Ccsds\.Cfdp\.NumChannels\] Fw\.BufferSend")
        self.assertNotIn("uplinkReturnOut", GUARD_FPP)


class TestItReadsPdusAsCfdpManagerDoes(unittest.TestCase):
    def test_it_uses_f_primes_own_cfdp_classes(self):
        self.assertIn("#include <Svc/Ccsds/CfdpManager/Types/PduBase.hpp>",
                      _read("flight", "Components", "CfdpGuard", "CfdpGuard.hpp"))
        for used in ("type == PduTypeEnum::METADATA", "MetadataPdu md;", "md.deserializeFrom(sb)",
                     "md.getDestFilename()", "peekPduType(pdu) == PduTypeEnum::FINISHED", "FinPdu fin;",
                     "fin.deserializeFrom(sb) != Fw::FW_SERIALIZE_OK", "Fw::ComPacketType::FW_PACKET_FILE",
                     "type == PduTypeEnum::END_OF_FILE", "EofPdu eof;", "eof.deserializeFrom(sb) != Fw::FW_SERIALIZE_OK"):
            self.assertIn(used, GUARD_CPP)
        self.assertIn("Svc_Ccsds_CfdpManager_Types", _read("flight", "Components", "CfdpGuard", "CMakeLists.txt"))

    def test_it_commits_only_on_a_clean_fin(self):
        for cond in ("ConditionCode::CONDITION_CODE_NO_ERROR", "FinFileStatus::FIN_FILE_STATUS_RETAINED",
                     "FinDeliveryCode::FIN_DELIVERY_CODE_COMPLETE", "PduDirection::DIRECTION_TOWARD_SENDER"):
            self.assertIn(cond, GUARD_CPP)

    def test_its_local_entity_is_cfdp_managers(self):
        # Only a class 2 upload addressed to cfdpManager's LocalEid can end with its FIN; the guard holds the same id
        prm = json.loads(_read("flight", "config", "PrmDb.json"))
        local = int(re.search(r"LOCAL_EID = (\d+);", GUARD_HPP).group(1))
        self.assertEqual(local, prm["DoomSat.cfdpManager"]["LocalEid"])

    @unittest.skipUnless(os.path.isdir(FPRIME), "no F´ install to read")
    def test_cfdp_manager_hands_back_a_buffer_that_is_not_a_file_pdu(self):
        with open(os.path.join(FPRIME, "Svc", "Ccsds", "CfdpManager", "CfdpManager.cpp"), encoding="utf-8") as f:
            src = f.read()
        handler = src[src.index("void CfdpManager ::dataIn_handler("):]
        handler = handler[:handler.index("\n}\n")]
        branch = re.search(r"if \(status != Fw::FW_SERIALIZE_OK \|\| packetType != Fw::ComPacketType::FW_PACKET_FILE\)"
                           r"\s*\{(.*?)\}", handler, re.S)
        self.assertIsNotNone(branch, "the silent hand-back the guard relies on to refuse a Metadata has changed")
        self.assertIn("dataInReturn_out(portNum, fwBuffer)", branch.group(1))
        self.assertIn("return;", branch.group(1))


class TestOneNameRule(unittest.TestCase):
    def test_doom_and_the_guard_share_it(self):
        self.assertIn('#include "DoomMission/Components/Doom/WadPath.hpp"', GUARD_CPP)
        self.assertIn("WadPath::isUplinkPart(dest.toChar())", GUARD_CPP)
        self.assertIn('#include "DoomMission/Components/Doom/WadPath.hpp"', DOOM_CPP)
        self.assertNotIn("wadDestLength", DOOM_CPP, "one copy of the rule, in WadPath.hpp")
        self.assertNotIn('getenv("DOOMSAT_HOME")', DOOM_CPP)
        announce = DOOM_CPP[DOOM_CPP.index("void Doom ::fileAnnounce_handler("):]
        announce = announce[:announce.index("\n}\n")]
        self.assertLess(announce.index("WadPath::isUplinkPart"), announce.index("placeWad"))

    def test_the_sync_and_the_unit_tests_have_it(self):
        sync = _read("scripts", "wsl_sync.sh")
        for f in ("Doom/WadPath.hpp", "CfdpGuard/CfdpGuard.fpp", "CfdpGuard/CfdpGuard.hpp", "CfdpGuard/CfdpGuard.cpp",
                  "CfdpGuard/CMakeLists.txt", "CfdpGuard/test/ut/CfdpGuardTester.cpp",
                  "CfdpGuard/test/ut/CfdpGuardTester.hpp", "CfdpGuard/test/ut/CfdpGuardTestMain.cpp"):
            self.assertIn("$SRC/Components/" + f, sync)
        self.assertIn('"${CMAKE_CURRENT_LIST_DIR}/CfdpGuard/"', sync)
        self.assertIn('ut)      exec bash "$HERE/wsl_ut.sh"', _read("scripts", "flight.sh"))


if __name__ == "__main__":
    unittest.main()
