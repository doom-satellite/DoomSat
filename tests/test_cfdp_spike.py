"""The CFDP spike's ground plumbing, pinned: what took a live run to find out stays found out.

No network beyond 127.0.0.1, no game, no flight software, no Yamcs: the YAML and SQL are read as text, the
launcher wrapper runs against a stand-in fprime_yamcs, the relay forwards between local sockets, and the run
script builds its parameter file with stand-ins for the F' venv's tools.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ETC = os.path.join(ROOT, "ground", "yamcs", "etc")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read().replace("\r\n", "\n")


INSTANCE = yaml.safe_load(_read("ground", "yamcs", "etc", "yamcs.fprime-project.yaml"))
SQL = _read("ground", "yamcs", "etc", "cfdp_streams.sql")
CFDP_CFG = _read("flight", "config", "CfdpCfg.fpp")
CFDP_HPP = _read("flight", "config", "CfdpCfg.hpp")
FPP = _read("flight", "Components", "Doom", "Doom.fpp")
CPP = _read("flight", "Components", "Doom", "Doom.cpp")
TOPOLOGY_CPP = _read("flight", "DoomSat", "Top", "DoomSatTopology.cpp")
RUN_SCRIPT = _read("scripts", "wsl_run_flight.sh")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import prmdb  # noqa: E402
PRM = json.loads(_read("flight", "config", "PrmDb.json"))["DoomSat.cfdpManager"]


def cfdp_service():
    return next(s for s in INSTANCE["services"] if s.get("class") == "org.yamcs.cfdp.CfdpService")["args"]


def tc_link():
    return next(link for link in INSTANCE["dataLinks"] if link["name"] == "UDP_TC_OUT")


class TestTheYamcsSide(unittest.TestCase):
    def test_the_sql_file_comes_after_the_streams_it_reads(self):
        keys = list(INSTANCE["streamConfig"])
        self.assertEqual(keys[-1], "sqlFile", "Yamcs creates streamConfig entries in order; cfdp_in reads tm_realtime")
        self.assertLess(keys.index("tm"), keys.index("sqlFile"))
        self.assertIn("${env.DOOMSAT_REPO}", INSTANCE["streamConfig"]["sqlFile"], "Yamcs runs from the F' project dir")

    def test_commands_go_ahead_of_pdus_and_one_packet_per_frame(self):
        link = tc_link()
        self.assertEqual(link["priorityScheme"], "ABSOLUTE", "FIFO would put a whole transfer ahead of commands")
        vcs = {vc["vcId"]: vc for vc in link["virtualChannels"]}
        self.assertEqual(vcs[1]["stream"], "tc_realtime")
        self.assertEqual(vcs[2]["stream"], "cfdp_tc")
        self.assertGreater(vcs[1]["priority"], vcs[2]["priority"])
        for vc in vcs.values():
            self.assertIs(vc["multiplePacketsPerFrame"], False, "F' keeps only the first space packet of a TC frame")

    def test_the_cfdp_service_matches_the_flight_software(self):
        a = cfdp_service()
        self.assertEqual((a["inStream"], a["outStream"]), ("cfdp_in", "cfdp_out"))
        self.assertEqual(a["checksumType"], "MODULAR", "cfdpManager always checks the modular checksum")
        self.assertLessEqual(a["sequenceNrLength"], 4, "F' keeps transaction numbers in a U32")
        self.assertLessEqual(a["entityIdLength"], 4)
        # 1024-byte TC frame - 5 header - 2 FECF - 6 space packet header - 2 F' descriptor
        self.assertLessEqual(a["maxPduSize"], 1024 - 5 - 2 - 6 - 2)
        self.assertEqual([e["id"] for e in a["remoteEntities"]], [42], "cfdpManager's LocalEid")
        self.assertEqual([e["id"] for e in a["localEntities"]], [100], "cfdpManager's FileInDefaultDestEntityId")
        self.assertFalse(a["hasDownloadCapability"], "F' v4.3.0 has no Proxy Put; downlinks start on board")
        # F' restarts its transaction numbers at every boot; Yamcs must not keep old ones answering for 10 min,
        # but must outlast the FIN retries (ChannelConfig ack_timer 2 s x ack_limit 10)
        self.assertTrue(20000 < a["pendingAfterCompletion"] <= 120000)

    def test_the_file_packet_service_is_gone(self):
        self.assertNotIn("com.example.myproject.FprimeFilePacketService", [s.get("class") for s in INSTANCE["services"]])

    def test_the_sql_wraps_and_unwraps_the_way_f_prime_frames_a_pdu(self):
        # downlink: APID 3, F' descriptor 0x0003, CFDP version 001 in the PDU's first byte; strip 6 + 2 bytes
        self.assertIn("(extract_ushort(packet, 0) & 2047) = 3", SQL)
        self.assertIn("extract_ushort(packet, 6) = 3", SQL)
        self.assertIn("(extract_ushort(packet, 8) >> 13) = 1", SQL)
        self.assertIn("substring(packet, 8) as pdu", SQL)
        # uplink: TC type bit + APID 3, unsegmented, length filled later, then the descriptor
        self.assertIn("unhex('1003C00000000003') + pdu", SQL)
        self.assertIn("insert into cfdp_tc", SQL)


class TestTheFlightSide(unittest.TestCase):
    def test_a_downlinked_pdu_fits_the_aggregator(self):
        size = int(re.search(r"^\s*constant MaxPduSize = (\d+)", CFDP_CFG, re.M).group(1))
        # ComAggregator asserts on a space packet over TmFrameFixedSize 1024 - 15; a PDU rides behind 6 + 2 bytes
        self.assertLessEqual(size + 6 + 2, 1024 - 15)

    def test_the_boot_parameters_match_the_ground(self):
        # scripts/wsl_run_flight.sh builds PrmDb.dat from flight/config/PrmDb.json at every start
        a = cfdp_service()
        self.assertEqual(PRM["LocalEid"], a["remoteEntities"][0]["id"])
        self.assertEqual(PRM["FileInDefaultDestEntityId"], a["localEntities"][0]["id"])
        self.assertGreaterEqual(PRM["RxCrcCalcBytesPerCycle"], 4 << 20, "the default 64 KiB a tick: a minute for doom1")
        self.assertEqual(PRM["FileInDefaultKeep"], "KEEP", "DELETE removes a file once downlinked")
        self.assertEqual(PRM["FileInDefaultClass"], "CLASS_2")
        self.assertEqual(len(PRM), 10, "every cfdpManager parameter, or prmDb warns PrmIdNotFound at boot")

    def test_a_downlinked_pdu_fits_whatever_its_transaction_number(self):
        # F' sizes file data from a header it has not filled in yet (TransactionTx.cpp sSendFileData), so MaxPduSize
        # alone does not bound a PDU. Cap the data for the worst header: fixed 4, both entity ids sized from the larger
        # (SendFile takes any destId, up to 4 bytes each), transaction number up to 4, and the 4-byte offset.
        size = int(re.search(r"^\s*constant MaxPduSize = (\d+)", CFDP_CFG, re.M).group(1))
        self.assertLessEqual(PRM["OutgoingFileChunkSize"] + 4 + 4 + 4 + 4 + 4, size)

    def test_cfdp_temp_files_stay_in_the_uplink_directory(self):
        # wsl_run_flight.sh fills $DOOMSAT_HOME/wads/uplink in for @UPLINK@ (tools/prmdb.py), makes the directory,
        # and builds PrmDb.dat from the result. Do the same here and check what the flight software would get, for
        # paths a sed replacement or a bare string in JSON would get wrong.
        self.assertIn('fprime-venv/bin/python "$REPO/tools/prmdb.py" "$REPO/flight/config/PrmDb.json" "$WADS/uplink"',
                      RUN_SCRIPT)
        self.assertIn('fprime-prm-write dat "$RUN/PrmDb.json"', RUN_SCRIPT, "and builds PrmDb.dat from that copy")
        self.assertIn('mkdir -p "$WADS/uplink/.cfdp-tmp"', RUN_SCRIPT)
        for uplink in ("/home/someone/doom/wads/uplink", "/home/u/R&D/doom/wads/uplink", "/a#b/wads/uplink",
                       "/a\\x41b/wads/uplink", '/a"b/wads/uplink', "/a\\1b/wads/uplink"):
            with self.subTest(uplink=uplink):
                built = json.loads(prmdb.render(_read("flight", "config", "PrmDb.json"), uplink))
                for channel in built["DoomSat.cfdpManager"]["ChannelConfig"]:
                    self.assertEqual(channel["tmp_dir"], uplink + "/.cfdp-tmp")
                    # F' v4.3.0 renames a failed poll file onto fail_dir itself, so a directory there only means
                    # "delete"; empty says so (and DoomSat runs no polls)
                    self.assertEqual(channel["fail_dir"], "")
                    self.assertEqual(channel["move_dir"], "")
        with self.assertRaises(ValueError):
            prmdb.render(_read("flight", "config", "PrmDb.json"), "doom/wads/uplink")

    def test_leftover_temp_files_are_cleared_while_nothing_runs(self):
        # A receive that ends before its metadata leaves <tmp_dir>/<eid>:<seq>.tmp, and F' never removes it. The
        # run script clears them before it starts the flight software, after stop.
        body = RUN_SCRIPT[RUN_SCRIPT.index("start_yamcs() {"):]
        body = body[:body.index("\n}\n")]
        clear = body.index('rm -f "$WADS/uplink/.cfdp-tmp/"*.tmp')
        self.assertLess(body.index('mkdir -p "$WADS/uplink/.cfdp-tmp"'), clear)
        self.assertLess(clear, body.index("detach "))
        for arm in ("start", "yamcs"):
            line = re.search(rf"^\s*{arm}\) (.*)$", RUN_SCRIPT, re.M).group(1)
            self.assertLess(line.index("stop;"), line.index("start_yamcs"), arm)

    def test_the_parameter_file_lives_outside_bin(self):
        # A second file in bin/ stops fprime-gds's find_app guessing the binary (and broke the native branch on a
        # shared install): the flight software reads $DOOMSAT_HOME/run/PrmDb.dat, and the script writes it there
        self.assertNotIn('configure("PrmDb.dat")', TOPOLOGY_CPP)
        self.assertIn('getenv("DOOMSAT_HOME")', TOPOLOGY_CPP)
        self.assertIn('prmFile.format("%s/run/PrmDb.dat", home)', TOPOLOGY_CPP)
        self.assertIn('prmFile.format("%s/doom/run/PrmDb.dat"', TOPOLOGY_CPP)
        self.assertIn('-o "$RUN/PrmDb.dat"', RUN_SCRIPT)
        self.assertNotIn('-o "$DEPLOY/bin/', RUN_SCRIPT)
        self.assertIn('rm -f "$DEPLOY/bin/PrmDb.dat"', RUN_SCRIPT, "the file earlier versions left in bin/")
        for arm in ("start", "yamcs"):
            line = re.search(rf"^\s*{arm}\) (.*)$", RUN_SCRIPT, re.M).group(1)
            self.assertLess(line.index("build_prmdb"), line.index("start_yamcs"), arm)
        start = re.search(r"^\s*start\) (.*)$", RUN_SCRIPT, re.M).group(1)
        self.assertLess(start.index("build_prmdb"), start.index("start_payload"), "a failed build starts nothing")
        self.assertIn("wsl_run_flight.sh\" prmdb", _read("scripts", "flight.sh"), "flight.sh gds builds it too")

    def test_no_file_packet_leftovers_in_the_run_script(self):
        # FPRIME_DOWNLINK_DIR fed FprimeFilePacketService's downlink mirror, which the CFDP instance does not run
        self.assertNotIn("FPRIME_DOWNLINK_DIR", RUN_SCRIPT)

    def test_the_cfdp_pool_never_overflows_the_file_queue(self):
        count = int(re.search(r"CFDP_BUFFER_COUNT = (\d+)", TOPOLOGY_CPP).group(1))
        per_cycle = [c["max_outgoing_pdus_per_cycle"] for c in PRM["ChannelConfig"]]
        self.assertGreaterEqual(count, max(per_cycle), "one channel alone never runs out")
        self.assertLessEqual(count, 100, "ComCcsdsConfig.QueueDepths.file: a full pool fits the FILE queue")
        self.assertIn("static_assert(CFDP_BUFFER_COUNT <= ComCcsdsConfig::QueueDepths::file", TOPOLOGY_CPP)

    def test_a_lossy_receive_remembers_what_it_already_has(self):
        # Stock F' tracks NakMaxSegments (58) received runs per transaction and forgets data past that, so a lossy
        # upload is mostly resent. The override has to be built (CMakeLists) and copied (wsl_sync.sh) to count.
        rx = re.search(r"^#define CFDP_CHANNEL_NUM_RX_CHUNKS_PER_TRANSACTION \{(\d+),", CFDP_HPP, re.M)
        self.assertIsNotNone(rx, "channel 0's RX chunk count is a number in flight/config/CfdpCfg.hpp")
        self.assertGreaterEqual(int(rx.group(1)), 1024)
        self.assertLess(int(rx.group(1)), 65536, "ChunkIdx is a U16")
        self.assertIn('"${CMAKE_CURRENT_LIST_DIR}/CfdpCfg.hpp"', _read("flight", "config", "CMakeLists.txt"))
        self.assertIn("$SRC/config/CfdpCfg.hpp", _read("scripts", "wsl_sync.sh"))

    def test_commit_wad_takes_a_bare_name_and_what_was_sent(self):
        command = FPP[FPP.index("async command COMMIT_WAD("):]
        command = command[:command.index(") opcode 0x07") + len(") opcode 0x07")]
        args = re.findall(r"^\s*(\w+): ([\w ]+?)\s*(?:@<.*)?$", command, re.M)
        self.assertEqual(args, [("part", "string size 40"), ("fileSize", "U32"), ("checksum", "U32")])

    def test_commit_wad_checks_the_file_before_it_gets_its_name(self):
        # cfdpManager writes in place and announces nothing: the rename waits for the size and CFDP checksum the
        # ground sent, so a commit sent early, or after a damaged class 1 upload, leaves a .part
        handler = CPP[CPP.index("void Doom ::COMMIT_WAD_cmdHandler("):]
        handler = handler[:handler.index("\n}\n")]
        self.assertLess(handler.index("fileSum("), handler.index("placeWad("))
        self.assertIn("log_WARNING_HI_WadCommitRefused", handler)
        self.assertIn("CFDP::Checksum", CPP)
        self.assertIn("CFDP_Checksum", _read("flight", "Components", "Doom", "CMakeLists.txt"))
        self.assertRegex(FPP, r"event WadCommitRefused\(")
        self.assertNotIn("becomes NAME.wad only here", FPP, "fileAnnounce is not the only rename any more")


class TestTheLauncher(unittest.TestCase):
    """ground/yamcs/launch.py against a stand-in fprime_yamcs: key order kept, links moved only with DOOMSAT_RELAY."""

    def load(self):
        stub = types.ModuleType("fprime_yamcs.__main__")
        stub.main = lambda: 0
        pkg = types.ModuleType("fprime_yamcs")
        with mock.patch.dict(sys.modules, {"fprime_yamcs": pkg, "fprime_yamcs.__main__": stub}):
            spec = importlib.util.spec_from_file_location("doomsat_launch", os.path.join(ROOT, "ground", "yamcs", "launch.py"))
            mod = importlib.util.module_from_spec(spec)
            real = yaml.safe_dump
            try:
                spec.loader.exec_module(mod)
            finally:
                yaml.safe_dump = real   # the module patches yaml for the launcher's process; undo it here
        return mod

    def config(self):
        return {"streamConfig": {"tm": ["tm_realtime"], "tc": ["tc_realtime"], "sqlFile": "x.sql"},
                "dataLinks": [{"name": "UDP_TM_IN", "class": "org.yamcs.tctm.ccsds.UdpTmFrameLink", "port": 50000},
                              {"name": "UDP_TC_OUT", "class": "org.yamcs.tctm.ccsds.UdpTcFrameLink", "port": 50001},
                              {"name": "UDP_TM_SPLIT_IN", "class": "org.yamcs.tctm.UdpTmDataLink", "port": 50002}]}

    def test_the_key_order_is_kept(self):
        mod = self.load()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOOMSAT_RELAY", None)
            text = mod.safe_dump(self.config())
        self.assertLess(text.index("tm:"), text.index("sqlFile:"))
        self.assertIn("port: 50000", text)

    def test_the_relay_moves_only_the_frame_links(self):
        mod = self.load()
        with mock.patch.dict(os.environ, {"DOOMSAT_RELAY": "1"}):
            data = yaml.safe_load(mod.safe_dump(self.config()))
        ports = {link["name"]: link["port"] for link in data["dataLinks"]}
        self.assertEqual(ports, {"UDP_TM_IN": 51000, "UDP_TC_OUT": 51001, "UDP_TM_SPLIT_IN": 50002})

    def test_the_relay_setting_means_what_it_says(self):
        # common.sh exports every DOOMSAT_* line from .env, so 0 or false there (or on the command line) must be off
        mod = self.load()
        for value, moved in (("0", False), ("false", False), ("no", False), ("off", False), ("", False),
                             (" 0 ", False), ("OFF", False), ("1", True), ("true", True), ("yes", True)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DOOMSAT_RELAY": value}):
                data = yaml.safe_load(mod.safe_dump(self.config()))
                ports = {link["name"]: link["port"] for link in data["dataLinks"]}
                self.assertEqual(ports, {"UDP_TM_IN": 51000 if moved else 50000,
                                         "UDP_TC_OUT": 51001 if moved else 50001, "UDP_TM_SPLIT_IN": 50002})


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "runs scripts/wsl_run_flight.sh under bash")
class TestTheParameterFileBuild(unittest.TestCase):
    """`wsl_run_flight.sh prmdb` with stand-ins for the F' venv's python and fprime-prm-write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "R&D #1", "doom")   # characters sed would have got wrong
        venv = os.path.join(self.home, "DoomSat", "fprime-venv", "bin")
        self.bin = os.path.join(self.home, "DoomSat", "build-artifacts", os.uname().sysname, "DoomSat", "bin")
        os.makedirs(venv)
        os.makedirs(self.bin)
        self.write(os.path.join(venv, "python"), f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        self.prm_write = os.path.join(venv, "fprime-prm-write")
        self.run_dir = os.path.join(self.home, "run")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def build(self, works, defaults="0"):
        self.write(self.prm_write, '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                                   + ('printf prm > "$out"\n' if works else 'echo "no such parameter" >&2; exit 1\n'))
        self.write(os.path.join(self.bin, "PrmDb.dat"), "left by an earlier version")
        env = dict(os.environ, DOOMSAT_HOME=self.home, DOOMSAT_PRM_DEFAULTS=defaults)
        return subprocess.run(["bash", os.path.join(ROOT, "scripts", "wsl_run_flight.sh"), "prmdb"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_a_good_build_lands_in_run_and_bin_holds_only_the_binary(self):
        done = self.build(works=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("built", done.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.run_dir, "PrmDb.dat")))
        self.assertFalse(os.path.exists(os.path.join(self.bin, "PrmDb.dat")))
        with open(os.path.join(self.run_dir, "PrmDb.json"), encoding="utf-8") as f:
            built = json.load(f)
        for channel in built["DoomSat.cfdpManager"]["ChannelConfig"]:
            self.assertEqual(channel["tmp_dir"], os.path.join(self.home, "wads", "uplink", ".cfdp-tmp"))

    def test_a_failed_build_stops_the_start(self):
        os.makedirs(self.run_dir)
        self.write(os.path.join(self.run_dir, "PrmDb.dat"), "the last start's file")
        done = self.build(works=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not starting", done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "PrmDb.dat")), "never the last start's file")
        with open(os.path.join(self.run_dir, "prmdb.log")) as f:
            self.assertIn("no such parameter", f.read())

    def test_flying_on_defaults_takes_an_explicit_yes(self):
        done = self.build(works=False, defaults="1")
        self.assertEqual(done.returncode, 0)
        self.assertIn("flying on parameter defaults", done.stderr)
        self.assertNotIn("built", done.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "PrmDb.dat")))


class TestTheRelay(unittest.TestCase):
    def free_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def run_relay(self, loss, n=200):
        listen, target = self.free_port(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target.bind(("127.0.0.1", 0))
        target.settimeout(0.5)
        other = f"{self.free_port()}:{self.free_port()}"
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "lossy_relay.py"), "--loss", str(loss),
                                 "--seed", "3", "--report", "0", "--tm", f"{listen}:{target.getsockname()[1]}",
                                 "--tc", other], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        got = []
        self.final = ""

        def drain():   # read as it arrives: a default receive buffer holds only about 256 small datagrams
            try:
                while True:
                    got.append(int.from_bytes(target.recv(16), "big"))
            except socket.timeout:
                pass

        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in ("TM", "TC"):
                self.assertTrue(proc.stdout.readline().startswith("[relay]"), "the relay did not start")
            reader = threading.Thread(target=drain)
            reader.start()
            for i in range(n):
                out.sendto(i.to_bytes(4, "big"), ("127.0.0.1", listen))
                time.sleep(0.0005)
            reader.join()
            return got
        finally:
            proc.terminate()
            self.final = proc.communicate(timeout=5)[0]
            out.close()
            target.close()

    def test_no_loss_is_a_pass_through(self):
        self.assertEqual(self.run_relay(0), list(range(200)))
        if os.name != "nt":   # Popen.terminate is TerminateProcess on Windows: no handler runs, no final report
            self.assertIn("TM sent 200 dropped 0", self.final, "SIGTERM still prints the final counts")

    def test_loss_drops_about_that_share_and_never_reorders(self):
        got = self.run_relay(20, n=500)
        self.assertEqual(got, sorted(got))
        self.assertTrue(330 < len(got) < 470, len(got))
        if os.name != "nt":   # as above: the final counts need SIGTERM
            self.assertIn(f"TM sent {len(got)} dropped {500 - len(got)}", self.final)


if __name__ == "__main__":
    unittest.main()
