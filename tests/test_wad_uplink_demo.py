"""tools/wad_uplink_demo.py against a stand-in Yamcs: no network, no game, no flight software.

The stand-in answers the way the stack does (FileReceived and WadUplinked once a file is sent, WadLoaded
and new WAD telemetry and a frame once LOAD_WAD is issued), so the tool's own logic is what is tested: the
name the file goes up under, the remote path, the bucket clean-up, the command it sends, and the exit code.
On the CFDP build it also does what Doom.cpp does with COMMIT_WAD (rename only a .part with the size and
checksum sent) and what payload/wad_uplink.py does with a name that is not in place.
"""
import base64
import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    import wad_uplink_demo as demo  # noqa: E402  needs yamcs-client (ground/.venv)
except ImportError:  # pragma: no cover
    demo = None

DOOM = "/DoomSat_DoomSat/DoomSat/doom/"


def wad_bytes(name):
    return base64.b64encode(name.encode().ljust(40, b"\0")).decode()


class FakeStack:
    """Just enough of YamcsClient, a processor, the storage and file transfer clients."""

    def __init__(self, refuse_load=False, cfdp=False, lose=(), damage=False, stall=False, pace_s=0.0, hold_s=0.0,
                 queued_s=0.0):
        self.refuse_load, self.cfdp = refuse_load, cfdp
        self.damage = damage      # the file lands on board with one byte wrong (class 1: no retransmission)
        self.stall = stall        # the transfer starts and never moves again
        self.queued_s = queued_s  # it waits this long QUEUED behind another upload, at 0 bytes
        self.pace_s = pace_s      # then takes this long, moving all the time
        self.hold_s = hold_s      # then sits RUNNING at full size this long (NAK resends, the wait for the FIN)
        self.lose = list(lose)    # what a lossy link drops, once each: a command name, or an event's [Id]
        self.parts = {}           # with cfdp: .part files on board (name -> bytes), waiting for COMMIT_WAD
        self.placed = set()       # names put in place on board
        self.objects = {}         # the bucket
        self.uploaded, self.deleted, self.transfers, self.commands, self.cancelled = [], [], [], [], []
        self.live = None
        self.on_event = self.on_data = None
        self.tlm = {"CMDS_RECEIVED": 7, "WAD_IWAD": wad_bytes("freedoom1.wad"), "WAD_PWAD": wad_bytes(""),
                    "WAD_LOADS": 0, "EPISODE": 1, "FRAMES_SENT": 100, "PAYLOAD_LINK": "True"}

    # -- YamcsClient
    def __call__(self, *_a, **_k):
        return self

    def get_processor(self, *_a):
        return self

    def create_event_subscription(self, _instance, on_data):
        self.on_event = on_data

    def create_parameter_subscription(self, _names, on_data):
        self.on_data = on_data
        self.publish()

    def get_storage_client(self):
        return self

    def get_file_transfer_client(self, _instance):
        return self

    # -- storage
    def list_buckets(self):
        return []

    def create_bucket(self, _name):
        pass

    def get_bucket(self, _name):
        return self

    def upload_object(self, name, content, content_type=None):
        self.uploaded.append((name, len(content)))
        self.objects[name] = bytes(content)

    def delete_object(self, name):
        self.deleted.append(name)

    # -- file transfer
    def list_services(self):
        return [types.SimpleNamespace(name="cfdp" if self.cfdp else "FprimeFilePacketService")]

    def get_service(self, name):
        self.service = name
        return self

    def create_transfer_subscription(self):
        return self

    def get_transfer(self, _id):
        return self.live() if self.live else None

    def cancel_transfer(self, transfer_id):
        self.cancelled.append(transfer_id)

    def progress(self, size, elapsed):
        if self.stall:
            return "RUNNING", 10
        if elapsed < self.queued_s:
            return "QUEUED", 0
        elapsed -= self.queued_s
        if elapsed < self.pace_s:
            return "RUNNING", int(size * elapsed / self.pace_s)
        if elapsed < self.pace_s + self.hold_s:
            return "RUNNING", size
        return "COMPLETED", size

    @staticmethod
    def snapshot(state, size):
        done = state in ("COMPLETED", "FAILED")
        return types.SimpleNamespace(id="1", transferred_size=size, is_complete=lambda: done,
                                     is_success=lambda: state == "COMPLETED", state=state, error=None)

    def upload(self, bucket, obj, remote, **kw):
        self.transfers.append((bucket, obj, remote))
        self.upload_kw = kw
        content = self.objects[obj]
        if self.stall or self.queued_s or self.pace_s or self.hold_s:   # on time.time(), which a test may fake
            t0 = time.time()
            self.live = lambda: self.snapshot(*self.progress(len(content), time.time() - t0))
        if self.cfdp:             # cfdpManager writes the file in place and announces nothing
            onboard = bytes([content[0] ^ 0xFF]) + content[1:] if self.damage and content else content
            self.parts[obj] = onboard
            if not kw.get("options", {}).get("reliable"):    # class 1: the receiver's own events
                if onboard != content:
                    threading.Timer(0.05, self.event, ["[RxCrcMismatch] File CRC mismatch"]).start()
                threading.Timer(0.1, self.event, ["[RxFileTransferCompleted] done"]).start()
            return self.live() if self.live else self.snapshot("COMPLETED", len(content))
        name = remote.rsplit("/", 1)[1].rsplit(".", 2)[0]
        self.placed.add(name)
        threading.Timer(0.1, self.event, [f"[FileReceived] Received file {remote[:40]}"]).start()
        threading.Timer(0.2, self.event, [f"[WadUplinked] Uplinked WAD ready to load: {remote.rsplit('/', 1)[0]}/{name}"]).start()
        return self.live() if self.live else self.snapshot("COMPLETED", len(content))

    # -- commands
    def issue_command(self, name, args):
        self.commands.append((name.rsplit("/", 1)[1], dict(args)))
        if self.dropped(name.rsplit("/", 1)[1]):
            return
        self.tlm["CMDS_RECEIVED"] += 1
        if name.endswith("COMMIT_WAD"):     # Doom::COMMIT_WAD_cmdHandler
            path = "/home/x/doom/wads/uplink/" + args["part"]
            onboard = self.parts.get(args["part"])
            if onboard is None:
                threading.Timer(0.1, self.event, [f"[WadUplinkFailed] Uplinked WAD could not be renamed into place: {path}"]).start()
            elif (len(onboard), demo.cfdp_checksum(onboard)) != (args["fileSize"], args["checksum"]):
                threading.Timer(0.1, self.event, [f"[WadCommitRefused] Uplinked WAD not committed: {path}"]).start()
            else:
                del self.parts[args["part"]]
                final = args["part"].rsplit(".", 2)[0]
                self.placed.add(final)
                threading.Timer(0.1, self.event, [f"[WadUplinked] Uplinked WAD ready to load: /home/x/doom/wads/uplink/{final}"]).start()
        if name.endswith("LOAD_WAD"):
            waiting = [(n, p) for n in (args["iwad"], args["pwad"]) if n and n not in self.placed
                       for p in self.parts if p.startswith(n + ".")]
            if waiting:                     # payload/wad_uplink.py find()
                n, part = waiting[0]
                threading.Timer(0.1, self.event, [f"[WadLoadFailed] Could not load {n}: {n} has not finished its uplink ({part} so far)"]).start()
            elif self.refuse_load:
                threading.Timer(0.1, self.event, ["[WadLoadFailed] Could not load x: no"]).start()
            else:
                self.tlm.update(WAD_IWAD=wad_bytes(args["iwad"]), WAD_PWAD=wad_bytes(args["pwad"]), WAD_LOADS=1,
                                EPISODE=2)
                threading.Timer(0.1, self.event, [f"[WadLoaded] Now flying {args['iwad']} on {args['map']}"]).start()
                for i in range(10):      # the new game flies on: frames keep coming
                    threading.Timer(0.3 + 0.2 * i, self.frame, [500 + i]).start()
        self.publish()

    def dropped(self, what):
        if what in self.lose:
            self.lose.remove(what)
            return True
        return False

    # -- what the stack sends down
    def event(self, message):
        if not self.dropped(message.split("]", 1)[0] + "]"):
            self.on_event(types.SimpleNamespace(message=message))

    def publish(self):
        if self.on_data:
            pvs = [types.SimpleNamespace(name=DOOM + k, eng_value=v) for k, v in self.tlm.items()]
            self.on_data(types.SimpleNamespace(parameters=pvs))

    def frame(self, seq):
        chunk = dict(seq=seq, index=0, count=1, length=4, data=base64.b64encode(b"jpeg").decode())
        self.on_data(types.SimpleNamespace(parameters=[types.SimpleNamespace(name=DOOM + "FRAME_CHUNK", eng_value=chunk)]))


class FakeClock:
    """Real time plus whatever the code under test has slept: sleeps cost nothing, and deadlines measured in real
    time (event waits) still pass."""

    def __init__(self, interrupt_after=None):
        self.real, self.slept, self.sleeps, self.interrupt_after = time.time, 0.0, 0, interrupt_after

    def time(self):
        return self.real() + self.slept

    def sleep(self, s):
        self.sleeps += 1
        if self.interrupt_after is not None and self.sleeps > self.interrupt_after:
            raise KeyboardInterrupt
        self.slept += s


@unittest.skipIf(demo is None, "yamcs-client is not installed (run with ground/.venv/bin/python)")
class TestTheDemo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wad = os.path.join(self.tmp.name, "basic.wad")
        with open(self.wad, "wb") as f:   # stands for the file: the tool only sums it
            f.write(bytes((i * 7 + 3) % 256 for i in range(2704)))

    def tearDown(self):
        self.tmp.cleanup()

    def run_demo(self, stack, *argv, said=None, clock=None):
        """clock: a FakeClock, so a transfer of many minutes runs in milliseconds (time.time() is faked for the
        stand-in stack too); otherwise sleeps return at once and time is real."""
        argv = ["wad_uplink_demo.py", "--remote-dir", "/home/x/doom/wads/uplink", "--out", self.tmp.name, *argv]
        say = said.append if said is not None else (lambda *_: None)
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(demo, "YamcsClient", stack))
            patches.enter_context(mock.patch.object(sys, "argv", argv))
            patches.enter_context(mock.patch.object(demo, "say", say))
            patches.enter_context(mock.patch.object(demo.time, "sleep", clock.sleep if clock else lambda _s: None))
            if clock:
                patches.enter_context(mock.patch.object(demo.time, "time", clock.time))
            return demo.main()

    def names(self, stack):
        return [c for c, _ in stack.commands]

    def test_a_pwad_goes_up_under_its_own_name_and_is_loaded_over_the_iwad(self):
        stack = FakeStack()
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01"), 0)
        (bucket, obj, remote), = stack.transfers
        self.assertRegex(obj, r"^basic\.wad\.\d+\.part$")
        self.assertEqual(remote, "/home/x/doom/wads/uplink/" + obj)
        self.assertEqual(stack.deleted, [obj], "the bucket object is removed once sent")
        self.assertIn(("LOAD_WAD", {"iwad": "freedoom2.wad", "pwad": "basic.wad", "map": "MAP01"}), stack.commands)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "wad_uplink_basic_MAP01.jpg")))

    def test_an_iwad_goes_up_under_the_name_asked_for(self):
        stack = FakeStack()
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--as", "shareware.wad", "--map", "E1M1"), 0)
        self.assertRegex(stack.transfers[0][1], r"^shareware\.wad\.\d+\.part$")
        self.assertIn(("LOAD_WAD", {"iwad": "shareware.wad", "pwad": "", "map": "E1M1"}), stack.commands)

    def test_truncate_never_uses_the_real_name(self):
        stack = FakeStack(refuse_load=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--truncate", "100", "--map", "MAP01",
                                       "--expect-fail"), 0)
        self.assertRegex(stack.transfers[0][1], r"^trunc-basic\.wad\.\d+\.part$")
        self.assertEqual(stack.uploaded[0][1], 100)

    def test_a_bad_name_stops_before_anything_goes_up(self):
        stack = FakeStack()
        with self.assertRaises(SystemExit):
            self.run_demo(stack, "--wad", self.wad, "--as", "two words.wad", "--map", "MAP01")
        self.assertEqual(stack.transfers, [])

    def test_cfdp_class_2_commits_the_part_then_loads_it(self):
        stack = FakeStack(cfdp=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--cfdp", "2", "--pdu-delay", "10"), 0)
        self.assertEqual(stack.upload_kw, {"source_entity": "ground", "destination_entity": "doomsat",
                                           "options": {"reliable": True, "pduDelay": 10}})
        # PrmDb.json sets the CRC pass rate at boot; the tool sends no parameter of its own
        self.assertEqual(self.names(stack), ["COMMIT_WAD", "LOAD_WAD"])
        content = Path(self.wad).read_bytes()
        self.assertEqual(stack.commands[0][1], {"part": stack.transfers[0][1], "fileSize": len(content),
                                                "checksum": demo.cfdp_checksum(content)})
        self.assertNotEqual(demo.cfdp_checksum(content), 0)
        self.assertEqual(stack.deleted, [stack.transfers[0][1]])

    def test_a_cfdp_name_too_long_for_commit_wad_stops_before_anything_goes_up(self):
        stack = FakeStack(cfdp=True)
        with self.assertRaises(SystemExit):   # 20 characters + 19 is one over COMMIT_WAD's 38
            self.run_demo(stack, "--wad", self.wad, "--as", "a_twenty_char_nm.wad", "--map", "MAP01", "--cfdp", "2")
        self.assertEqual(stack.transfers, [])
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--as", "nineteen_chars0.wad", "--iwad",
                                       "freedoom2.wad", "--map", "MAP01", "--cfdp", "2"), 0)
        self.assertEqual(len(stack.commands[0][1]["part"]), 38)

    def test_the_cfdp_name_limit_follows_the_name_the_tool_builds(self):
        stack = FakeStack(cfdp=True)
        with mock.patch.object(demo.time, "time_ns", lambda: 10 ** 20), self.assertRaises(SystemExit):
            # a 15-digit millisecond count: the 19-character name that fits today no longer does
            self.run_demo(stack, "--wad", self.wad, "--as", "nineteen_chars0.wad", "--map", "MAP01", "--cfdp", "2")
        self.assertEqual(stack.transfers, [])

    def test_the_build_decides_the_transfer_when_not_told(self):
        native, cfdp = FakeStack(), FakeStack(cfdp=True)
        args = ("--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01")
        self.assertEqual(self.run_demo(native, *args), 0)
        self.assertEqual(native.service, "FprimeFilePacketService")
        self.assertNotIn("COMMIT_WAD", [c for c, _ in native.commands])
        self.assertEqual(self.run_demo(cfdp, *args), 0)
        self.assertEqual(cfdp.service, "cfdp")
        self.assertTrue(cfdp.upload_kw["options"]["reliable"], "class 2 unless told otherwise")
        self.assertIn("COMMIT_WAD", [c for c, _ in cfdp.commands])

    def test_a_lost_commit_answer_is_asked_again_and_the_load_settles_it(self):
        # The first COMMIT_WAD works but its WadUplinked is lost; the second finds no .part (WadUplinkFailed)
        stack = FakeStack(cfdp=True, lose=["[WadUplinked]"])
        with mock.patch.object(demo, "COMMIT_ANSWER_S", 0.5):
            self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                           "--cfdp", "2", "--tries", "3"), 0)
        self.assertEqual([c for c, _ in stack.commands].count("COMMIT_WAD"), 2)
        self.assertIn("LOAD_WAD", [c for c, _ in stack.commands])

    def test_a_failed_commit_on_the_first_try_is_a_failure(self):
        stack = FakeStack(cfdp=True)
        stack.upload = lambda *a, **k: (FakeStack.upload(stack, *a, **k), stack.parts.clear())[0]
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--cfdp", "2", "--tries", "3"), 1)
        self.assertNotIn("LOAD_WAD", [c for c, _ in stack.commands])

    def test_a_lost_load_wad_is_sent_again(self):
        stack = FakeStack(lose=["LOAD_WAD"])
        with mock.patch.object(demo, "LOAD_RETRY_ANSWER_S", 0.5), mock.patch.object(demo, "TLM_STANDIN_S", 0.2):
            self.assertEqual(self.run_demo(stack, "--iwad", "freedoom1.wad", "--map", "E1M2", "--tries", "2"), 0)
        self.assertEqual([c for c, _ in stack.commands].count("LOAD_WAD"), 2)

    def test_a_lost_wad_loaded_event_is_stood_in_for_by_telemetry(self):
        stack = FakeStack(lose=["[WadLoaded]"])
        with mock.patch.object(demo, "LOAD_ANSWER_S", 0.5):
            self.assertEqual(self.run_demo(stack, "--iwad", "freedoom1.wad", "--map", "E1M2"), 0)
        self.assertEqual([c for c, _ in stack.commands].count("LOAD_WAD"), 1)

    def test_a_refused_load_fails_unless_expected(self):
        self.assertEqual(self.run_demo(FakeStack(refuse_load=True), "--iwad", "nothere.wad", "--map", "E1M1",
                                       "--expect-fail"), 0)
        self.assertEqual(self.run_demo(FakeStack(refuse_load=True), "--iwad", "freedoom1.wad", "--map", "E1M1"), 1)


    # -- the CFDP checksum COMMIT_WAD carries: F' CFDP::Checksum's own unit test vector (ChecksumMain.cpp)
    def test_the_checksum_is_cfdp_s_modular_checksum(self):
        self.assertEqual(demo.cfdp_checksum(bytes(range(8))), 0x00010203 + 0x04050607)
        self.assertEqual(demo.cfdp_checksum(bytes(range(9))), 0x00010203 + 0x04050607 + 0x08000000)
        self.assertEqual(demo.cfdp_checksum(b""), 0)
        self.assertEqual(demo.cfdp_checksum(b"\xff" * 8), 0xFFFFFFFE)   # modulo 2**32

    def test_checksum_prints_what_a_commit_by_hand_needs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_demo(FakeStack(), "--checksum", self.wad), 0)
        content = Path(self.wad).read_bytes()
        self.assertEqual(out.getvalue().strip(), f"fileSize {len(content)} checksum {demo.cfdp_checksum(content)}")

    # -- what is sent, and how
    def test_service_cfdp_alone_means_class_2_and_commit(self):
        stack = FakeStack(cfdp=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--service", "cfdp"), 0)
        self.assertEqual(stack.service, "cfdp")
        self.assertIs(stack.upload_kw["options"]["reliable"], True)
        self.assertEqual(stack.upload_kw["destination_entity"], "doomsat")
        self.assertEqual(self.names(stack), ["COMMIT_WAD", "LOAD_WAD"])
        self.assertEqual(stack.parts, {})
        with self.assertRaises(SystemExit):   # and the COMMIT_WAD name limit applies
            self.run_demo(FakeStack(cfdp=True), "--wad", self.wad, "--as", "a_twenty_char_nm.wad", "--map", "MAP01",
                          "--service", "cfdp")

    def test_cfdp_with_another_service_is_refused(self):
        stack = FakeStack(cfdp=True)
        with self.assertRaises(SystemExit):
            self.run_demo(stack, "--wad", self.wad, "--map", "MAP01", "--cfdp", "2",
                          "--service", "FprimeFilePacketService")
        self.assertEqual(stack.transfers, [])

    def test_tries_and_pace_are_checked_before_anything_goes_up(self):
        for bad in (["--tries", "0"], ["--tries", "-1"], ["--pdu-delay", "0"], ["--pdu-delay", "-4"],
                    ["--pdu-delay", "2"]):
            with self.subTest(bad=bad):
                stack = FakeStack(cfdp=True)
                with self.assertRaises(SystemExit):
                    self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                  "--cfdp", "2", *bad)
                self.assertEqual((stack.transfers, stack.commands), ([], []))
        stack = FakeStack(cfdp=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--no-load", "--cfdp", "2", "--pdu-delay", "5"), 0)
        self.assertEqual(stack.upload_kw["options"]["pduDelay"], 5)

    # -- a file that is not whole never gets its name
    def test_a_damaged_class_1_file_whose_crc_event_is_lost_is_refused_on_board(self):
        stack = FakeStack(cfdp=True, damage=True, lose=["[RxCrcMismatch]"])
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--cfdp", "1"), 1)
        self.assertEqual(self.names(stack), ["COMMIT_WAD"], "committed on the events alone, refused on board")
        self.assertEqual(len(stack.parts), 1, "it stays a .part")
        self.assertNotIn("basic.wad", stack.placed)
        self.assertEqual(stack.deleted, [stack.transfers[0][1]])

    def test_a_class_1_crc_mismatch_is_not_committed_and_the_bucket_is_cleaned(self):
        stack = FakeStack(cfdp=True, damage=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--map", "MAP01", "--cfdp", "1"), 1)
        self.assertNotIn("COMMIT_WAD", self.names(stack))
        self.assertEqual(stack.deleted, [stack.transfers[0][1]])

    def test_a_clean_class_1_file_is_committed(self):
        stack = FakeStack(cfdp=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--cfdp", "1"), 0)
        self.assertEqual(self.names(stack), ["COMMIT_WAD", "LOAD_WAD"])

    # -- how long an uplink may take (on a fake clock)
    def big_wad(self, size=4_196_020):
        path = os.path.join(self.tmp.name, "doom1.wad")
        with open(path, "wb") as f:
            f.write(bytes(size))
        return path

    def test_a_stalled_transfer_is_cancelled_and_its_object_removed(self):
        stack, clock = FakeStack(cfdp=True, stall=True), FakeClock()
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--no-load", "--cfdp", "2", clock=clock), 1)
        self.assertEqual(stack.cancelled, ["1"])
        self.assertEqual(stack.deleted, [stack.transfers[0][1]])
        self.assertNotIn("COMMIT_WAD", self.names(stack))
        self.assertLess(clock.slept, demo.STALL_S + 5)

    def test_a_transfer_longer_than_any_size_budget_is_not_given_up(self):
        # doom1.wad at 100 ms a PDU: 4,226 PDUs, about 423 s, beyond the old budget of size / 15000 + 60 = 340 s
        stack, clock = FakeStack(cfdp=True, pace_s=423.0), FakeClock()
        self.assertEqual(self.run_demo(stack, "--wad", self.big_wad(), "--no-load", "--cfdp", "2", "--pdu-delay",
                                       "100", clock=clock), 0)
        self.assertGreater(clock.slept, 4_196_020 / 15000 + 60)
        self.assertEqual(stack.cancelled, [])
        self.assertEqual(self.names(stack), ["COMMIT_WAD"])

    def test_the_nak_tail_at_full_size_is_not_a_stall(self):
        # Yamcs counts first-pass bytes only: resends and the wait for the FIN show (RUNNING, size) unchanged
        stack, clock = FakeStack(cfdp=True, pace_s=30.0, hold_s=150.0), FakeClock()
        self.assertGreater(150.0, demo.STALL_S)
        self.assertEqual(self.run_demo(stack, "--wad", self.big_wad(), "--no-load", "--cfdp", "2", clock=clock), 0)
        self.assertEqual(stack.cancelled, [])

    def test_a_tail_past_its_limit_is_given_up(self):
        size = 4_196_020
        stack, clock = FakeStack(cfdp=True, pace_s=30.0, hold_s=1e9), FakeClock()
        self.assertEqual(self.run_demo(stack, "--wad", self.big_wad(size), "--no-load", "--cfdp", "2", clock=clock), 1)
        self.assertEqual(stack.cancelled, ["1"])
        self.assertLess(clock.slept, 30 + demo.tail_limit(size, demo.DEFAULT_PDU_DELAY_MS) + 5)

    def test_the_tail_limit_covers_an_iwad_at_five_percent_loss(self):
        # freedoom2.wad (28,787,748 B): about 28 NAK rounds at 5 % loss each way; median tails of 96 s to 243 s
        # at 5 to 100 ms a PDU (the re-review's simulation, checked against the measured doom1.wad tail)
        for pace, tail in ((5, 96), (10, 104), (40, 150), (100, 243)):
            self.assertGreater(demo.tail_limit(28_787_748, pace), 2 * tail)

    def test_waiting_in_the_queue_is_not_a_stall(self):
        stack, clock = FakeStack(cfdp=True, queued_s=300.0, pace_s=10.0), FakeClock()
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--no-load", "--cfdp", "2", clock=clock), 0)
        self.assertEqual(stack.cancelled, [])

    def test_ctrl_c_cancels_the_transfer_and_removes_the_object(self):
        stack = FakeStack(cfdp=True, stall=True)
        with self.assertRaises(KeyboardInterrupt):
            self.run_demo(stack, "--wad", self.wad, "--no-load", "--cfdp", "2", clock=FakeClock(interrupt_after=3))
        self.assertEqual(stack.cancelled, ["1"])
        self.assertEqual(stack.deleted, [stack.transfers[0][1]])

    def test_a_failed_load_early_cancels_the_transfer(self):
        stack = FakeStack(cfdp=True, stall=True, lose=["[WadLoadFailed]"])
        with mock.patch.object(demo, "LOAD_ANSWER_S", 0.3):
            self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                           "--cfdp", "2", "--load-early", clock=FakeClock()), 1)
        self.assertEqual(stack.cancelled, ["1"])

    # -- a COMMIT_WAD nobody saw run
    def test_an_unconfirmed_commit_without_a_load_is_a_failure(self):
        stack = FakeStack(cfdp=True, lose=["COMMIT_WAD", "COMMIT_WAD"])
        with mock.patch.object(demo, "COMMIT_ANSWER_S", 0.3):
            self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--no-load", "--cfdp", "2", "--tries", "2"), 1)
        self.assertEqual(self.names(stack), ["COMMIT_WAD", "COMMIT_WAD"])

    def test_a_negative_case_fails_when_the_commit_never_ran(self):
        # The truncated file never got its name, so LOAD_WAD's refusal says nothing about the truncated file
        for tries in (1, 3):
            with self.subTest(tries=tries):
                stack = FakeStack(cfdp=True, refuse_load=True, lose=["COMMIT_WAD"] * tries)
                with mock.patch.object(demo, "COMMIT_ANSWER_S", 0.3):
                    self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--truncate", "100", "--map", "MAP01",
                                                   "--expect-fail", "--cfdp", "2", "--tries", str(tries)), 1)
                self.assertEqual(len(stack.parts), 1)

    def test_a_lost_commit_does_not_fly_an_older_file_of_the_same_name(self):
        # With no answer at all, the COMMIT_WAD may never have arrived; LOAD_WAD would then use the older file
        for extra, kw in (((), {}), (("--truncate", "100", "--expect-fail"), {"refuse_load": True})):
            with self.subTest(extra=extra):
                stack = FakeStack(cfdp=True, lose=["COMMIT_WAD"], **kw)
                stack.placed.add("trunc-basic.wad" if extra else "basic.wad")
                with mock.patch.object(demo, "COMMIT_ANSWER_S", 0.3):
                    self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map",
                                                   "MAP01", "--cfdp", "2", *extra), 1)
                self.assertNotIn("LOAD_WAD", self.names(stack))

    def test_a_negative_case_whose_commit_answer_alone_was_lost_still_passes(self):
        stack = FakeStack(cfdp=True, refuse_load=True, lose=["[WadUplinked]"])
        with mock.patch.object(demo, "COMMIT_ANSWER_S", 0.3):
            self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--truncate", "100", "--map", "MAP01",
                                           "--expect-fail", "--cfdp", "2", "--tries", "3"), 0)
        self.assertIn("trunc-basic.wad", stack.placed)

    # -- the telemetry that stands in for a lost LOAD_WAD answer must show this load
    def _concurrent_load(self, stack):
        # Someone else's LOAD_WAD lands at the same time: the count moves, the names are not ours, and the old game
        # keeps sending frames
        real = stack.issue_command

        def issue(name, args):
            real(name, args)
            if name.endswith("LOAD_WAD"):
                stack.tlm.update(WAD_LOADS=(stack.tlm.get("WAD_LOADS") or 0) + 1, WAD_IWAD=wad_bytes("other.wad"))
                stack.publish()
                for i in range(20):
                    threading.Timer(0.05 * i, stack.frame, [700 + i]).start()
        stack.issue_command = issue

    def test_a_refused_load_is_not_stood_in_for_by_an_unrelated_count(self):
        stack, said = FakeStack(refuse_load=True, lose=["[WadLoadFailed]", "[WadLoadFailed]"]), []
        self._concurrent_load(stack)
        with mock.patch.object(demo, "LOAD_RETRY_ANSWER_S", 0.3), mock.patch.object(demo, "TLM_STANDIN_S", 0.3):
            self.assertEqual(self.run_demo(stack, "--iwad", "freedoom2.wad", "--map", "MAP01", "--tries", "2",
                                           said=said), 1)
        # The other load's count never stands in, so the second LOAD_WAD still goes up (the later name check
        # would also fail the run, which is why the rc alone does not show it)
        self.assertFalse([m for m in said if "(event not seen" in m], said)
        self.assertEqual(self.names(stack).count("LOAD_WAD"), 2)

    def test_no_stand_in_without_a_count_from_before(self):
        stack = FakeStack(lose=["[WadLoaded]"])
        del stack.tlm["WAD_LOADS"]
        with mock.patch.object(demo, "LOAD_ANSWER_S", 0.3), mock.patch.object(demo, "TLM_STANDIN_S", 0.3):
            self.assertEqual(self.run_demo(stack, "--iwad", "freedoom1.wad", "--map", "E1M2"), 1)

    def test_a_wad_loaded_that_names_another_file_is_a_failure(self):
        stack = FakeStack()
        real = stack.issue_command

        def issue(name, args):
            real(name, args)
            if name.endswith("LOAD_WAD"):
                stack.tlm.update(WAD_IWAD=wad_bytes("other.wad"))
                stack.publish()
        stack.issue_command = issue
        self.assertEqual(self.run_demo(stack, "--iwad", "freedoom2.wad", "--map", "MAP01"), 1)

    # -- on the CFDP build, nothing waits for FileUplink's events
    def test_cfdp_waits_only_for_the_doom_component(self):
        needles, said = [], []
        real = demo.Link.wait_event

        def spy(link, since, *wanted, timeout=60.0):
            needles.extend(wanted)
            return real(link, since, *wanted, timeout=timeout)
        with mock.patch.object(demo.Link, "wait_event", spy):
            self.assertEqual(self.run_demo(FakeStack(cfdp=True), "--wad", self.wad, "--iwad", "freedoom2.wad",
                                           "--map", "MAP01", "--cfdp", "2", said=said), 0)
        self.assertFalse({"[FileReceived]", "[BadChecksum]", "[FileWriteError]", "[FileOpenError]"} & set(needles))
        self.assertNotIn("event: None", said)

if __name__ == "__main__":
    unittest.main()
