"""LOAD_WAD: its two link records and its name checks, pinned against the flight software.

The Doom component packs the LOAD_WAD record and unpacks the WAD report in C++ that nothing here can run,
so the layouts are checked against Doom.cpp and Doom.fpp as text, the way test_runner.py pins the status
record. No network and no game: payload/wad_uplink.py imports neither ViZDoom nor anything heavy, and the
"WADs" below are a few bytes of zeros made at test time (nothing here ever needs a real one).
"""
import inspect
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "payload"))

import wad_uplink as wu  # noqa: E402


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read().replace("\r\n", "\n")


CPP = _read("flight", "Components", "Doom", "Doom.cpp")
FPP = _read("flight", "Components", "Doom", "Doom.fpp")
PAYLOAD = _read("payload", "doom_payload.py")


class TestTheRecords(unittest.TestCase):
    def test_load_wad_is_three_length_prefixed_texts(self):
        body = wu.encode_load_wad("freedoom2.wad", "basic.wad", "MAP01")
        self.assertEqual(body, b"\x0dfreedoom2.wad\x09basic.wad\x05MAP01")
        self.assertEqual(wu.decode_load_wad(body), ("freedoom2.wad", "basic.wad", "MAP01"))

    def test_no_pwad_is_an_empty_text_not_a_missing_one(self):
        body = wu.encode_load_wad("doom1.wad", "", "E1M1")
        self.assertEqual(body, b"\x09doom1.wad\x00\x04E1M1")
        self.assertEqual(wu.decode_load_wad(body), ("doom1.wad", "", "E1M1"))

    def test_the_wad_report_bytes(self):
        body = wu.encode_wad_report(wu.LOADED, 3, "freedoom2.wad", "basic.wad", "basic.wad over freedoom2.wad",
                                    "MAP01")
        self.assertEqual(body[:3], b"\x01\x00\x03")
        self.assertEqual(body[3:], b"\x0dfreedoom2.wad\x09basic.wad\x1cbasic.wad over freedoom2.wad\x05MAP01\x00")
        self.assertEqual(wu.decode_wad_report(body),
                         dict(result=wu.LOADED, loads=3, iwad="freedoom2.wad", pwad="basic.wad",
                              name="basic.wad over freedoom2.wad", map="MAP01", reason=""))

    def test_a_failure_carries_its_reason_and_the_wad_still_running(self):
        r = wu.decode_wad_report(wu.encode_wad_report(wu.FAILED, 1, "doom1.wad", "", "x.wad", "E1M1",
                                                      "x.wad is in neither directory"))
        self.assertEqual((r["result"], r["iwad"], r["name"], r["reason"]),
                         (wu.FAILED, "doom1.wad", "x.wad", "x.wad is in neither directory"))

    def test_a_reason_is_cut_to_what_the_flight_software_keeps(self):
        r = wu.decode_wad_report(wu.encode_wad_report(wu.FAILED, 0, "a.wad", "", "b.wad", "M", "z" * 500))
        self.assertEqual(len(r["reason"]), wu.TEXT_MAX)

    def test_every_truncation_is_refused_rather_than_misread(self):
        for body, decode in ((wu.encode_load_wad("doom1.wad", "x.wad", "E1M1"), wu.decode_load_wad),
                             (wu.encode_wad_report(wu.LOADED, 1, "a.wad", "b.wad", "n", "m", "r"),
                              wu.decode_wad_report)):
            for cut in range(len(body)):
                with self.assertRaises(ValueError, msg="%r cut to %d bytes" % (body, cut)):
                    decode(body[:cut])

    def test_the_loads_count_wraps_like_the_u16_it_travels_in(self):
        self.assertEqual(wu.decode_wad_report(wu.encode_wad_report(wu.LOADED, 65537, "a.wad", "", "", ""))["loads"], 1)


class TestTheFlightSoftwareAgrees(unittest.TestCase):
    """Doom.cpp and Doom.fpp read as text: the C++ cannot be run here, so its constants are pinned."""

    def test_the_record_kinds(self):
        handler = CPP[CPP.index("void Doom ::LOAD_WAD_cmdHandler"):]
        handler = handler[:handler.index("\n}\n")]
        self.assertEqual(int(re.search(r"sendToPayload\(0x(\w+), body, n\)", handler).group(1), 16), wu.KIND_LOAD_WAD)
        self.assertRegex(CPP, r"case %d:\s*\n\s*this->handleWad\(" % wu.KIND_WAD)
        self.assertIn("elif kind == wu.KIND_LOAD_WAD:", PAYLOAD)

    def test_the_flight_software_packs_load_wad_the_way_the_payload_reads_it(self):
        handler = CPP[CPP.index("void Doom ::LOAD_WAD_cmdHandler"):]
        handler = handler[:handler.index("\n}\n")]
        self.assertIn("names[3] = {&iwad, &pwad, &map};", handler, "the names go in the order decode_load_wad reads")
        self.assertLess(handler.index("body[n++] = take;"), handler.index("std::memcpy(&body[n], name->toChar(), take);"),
                        "each name is a length byte, then its bytes")
        self.assertEqual(wu.decode_load_wad(wu.pack_texts("a.wad", "b.wad", "M")), ("a.wad", "b.wad", "M"))

    def test_the_flight_software_puts_each_report_field_where_it_belongs(self):
        handler = CPP[CPP.index("void Doom ::handleWad"):]
        handler = handler[:handler.index("\n}\n")]
        self.assertIn("this->m_wadIwad[i] = static_cast<U8>(i < textLen[IWAD] ? text[IWAD][i] : 0);", handler)
        self.assertIn("this->m_wadPwad[i] = static_cast<U8>(i < textLen[PWAD] ? text[PWAD][i] : 0);", handler)
        self.assertIn("this->m_wadLoads = loads;", handler)
        self.assertRegex(handler, r"result == WAD_LOADED\) \{[^}]*log_ACTIVITY_HI_WadLoaded\(Fw::String\(text\[NAME\]\), "
                                  r"Fw::String\(text\[MAP\]\)\)")
        self.assertRegex(handler, r"result == WAD_FAILED\) \{\s*this->log_WARNING_HI_WadLoadFailed\(Fw::String\(text\[NAME\]\), "
                                  r"Fw::String\(text\[REASON\]\)\)")

    def test_the_report_fields_come_in_the_order_the_flight_software_reads_them(self):
        order = re.search(r"enum \{ (IWAD, PWAD, NAME, MAP, REASON), TEXTS \}", CPP)
        self.assertIsNotNone(order, "handleWad's field list has changed")
        params = list(inspect.signature(wu.encode_wad_report).parameters)
        self.assertEqual(params, ["result", "loads", "iwad", "pwad", "name", "map_name", "reason"])
        self.assertEqual([p.upper() for p in params[2:]], ["IWAD", "PWAD", "NAME", "MAP_NAME", "REASON"])
        self.assertIn("const U8 result = rdU8(p);\n    const U16 loads = rdU16(p);", CPP)

    def test_the_result_codes(self):
        codes = dict(re.findall(r"WAD_(REPORT|LOADED|FAILED) = (\d)", CPP))
        self.assertEqual({k: int(v) for k, v in codes.items()},
                         {"REPORT": wu.REPORT, "LOADED": wu.LOADED, "FAILED": wu.FAILED})

    def test_the_sizes(self):
        command = FPP[FPP.index("async command LOAD_WAD("):]
        command = command[:command.index(") opcode")]
        sizes = dict(re.findall(r"\$?(\w+): string size (\d+)", command))
        self.assertEqual(sizes, {"iwad": "40", "pwad": "40", "map": "10"})
        self.assertIn(") opcode 0x06", FPP[FPP.index("async command LOAD_WAD("):][:900])
        # Yamcs counts the two-byte length tag against a string argument's declared size
        self.assertEqual(wu.NAME_MAX, int(sizes["iwad"]) - 2)
        self.assertEqual(wu.MAP_MAX, int(sizes["map"]) - 2)
        self.assertEqual(int(re.search(r"WAD_ARG_MAX = (\d+)", CPP).group(1)), int(sizes["iwad"]))
        self.assertGreaterEqual(int(re.search(r"array WadName = \[(\d+)\] U8", FPP).group(1)), wu.NAME_MAX)
        self.assertEqual(int(re.search(r"WAD_TEXT_MAX = (\d+)", CPP).group(1)), wu.TEXT_MAX)
        self.assertIn("reason: string size %d" % wu.TEXT_MAX, FPP)

    def test_the_wad_name_channel_is_bytes_not_a_string(self):
        """fprime-xtce gives F' string telemetry a fixed size that Yamcs then cannot decode (docs/ARCHITECTURE.md)."""
        block = FPP[FPP.index("array WadName") - 300:FPP.index("array WadName")]
        self.assertIn("@ !binary", block)
        for channel in ("WAD_IWAD", "WAD_PWAD"):
            self.assertRegex(FPP, r"telemetry %s: WadName id \d+" % channel)

    def test_the_payload_reports_on_connect(self):
        run = PAYLOAD[PAYLOAD.index("    def run(self, conn):"):]
        self.assertIn("self.wad_report(wu.REPORT)", run[:300])


class TestTheNames(unittest.TestCase):
    def test_plain_wad_names_pass(self):
        for name in ("freedoom2.wad", "basic.wad", "my_level-2.wad", "a+b.wad", "x" * 34 + ".wad"):
            self.assertIsNone(wu.name_problem(name), name)

    def test_what_is_refused_and_why(self):
        cases = {
            "": "no IWAD named",
            "../../etc/passwd.wad": "bare file name",
            "..secret.wad": "'..'",
            "sub/level.wad": "bare file name",
            "sub\\level.wad": "bare file name",
            "C:level.wad": "bare file name",
            "level.txt": "not a .wad file",
            "level.WAD": "not a .wad file",
            "level.wad.part": "not a .wad file",
            ".hidden.wad": "letters, digits",
            "two words.wad": "letters, digits",
            "nul\x00.wad": "letters, digits",
            "x" * 35 + ".wad": "longer than",
        }
        for name, why in cases.items():
            self.assertIn(why, wu.name_problem(name) or "", repr(name))

    def test_the_pwad_is_named_as_such(self):
        self.assertIn("PWAD", wu.name_problem("../x.wad", "PWAD"))

    def test_maps(self):
        for m in ("MAP01", "E1M1", "START"):
            self.assertIsNone(wu.map_problem(m), m)
        for m in ("", "MAP01;quit", "TOOLONGMAP", "E1 M1", "../E1M1"):
            self.assertIsNotNone(wu.map_problem(m), m)

    def test_the_display_name(self):
        self.assertEqual(wu.display_name("freedoom2.wad", "basic.wad"), "basic.wad over freedoom2.wad")
        self.assertEqual(wu.display_name("doom1.wad", ""), "doom1.wad")


class TestFindingTheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wads = os.path.join(self.tmp.name, "wads")
        self.uplink = os.path.join(self.wads, "uplink")
        os.makedirs(self.uplink)
        self.dirs = [self.uplink, self.wads]

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, d, name, size=64):
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        return path

    def test_the_uplink_directory_is_looked_in_first(self):
        self.put(self.wads, "level.wad")
        up = self.put(self.uplink, "level.wad")
        self.assertEqual(wu.find("level.wad", self.dirs), (os.path.realpath(up), None))

    def test_an_installed_wad_is_found(self):
        inst = self.put(self.wads, "doom1.wad")
        self.assertEqual(wu.find("doom1.wad", self.dirs), (os.path.realpath(inst), None))

    def test_a_file_too_short_to_be_a_wad(self):
        self.put(self.uplink, "tiny.wad", size=11)
        path, why = wu.find("tiny.wad", self.dirs)
        self.assertIsNone(path)
        self.assertIn("too short", why)

    def test_one_still_arriving_says_so(self):
        self.put(self.uplink, "big.wad.1696350000.part")
        path, why = wu.find("big.wad", self.dirs)
        self.assertIsNone(path)
        self.assertIn("has not finished its uplink", why)

    def test_one_that_is_nowhere(self):
        self.assertIn("in neither", wu.find("nothere.wad", self.dirs)[1])

    @unittest.skipIf(not hasattr(os, "symlink"), "no symlinks here")
    def test_a_link_out_of_the_wad_directories_is_refused(self):
        outside = self.put(self.tmp.name, "elsewhere.wad")
        try:
            os.symlink(outside, os.path.join(self.uplink, "escape.wad"))
        except OSError:
            self.skipTest("symlinks not permitted")
        self.assertIn("outside", wu.find("escape.wad", self.dirs)[1])

    def test_resolve_a_request(self):
        iw = self.put(self.wads, "freedoom2.wad")
        pw = self.put(self.uplink, "basic.wad")
        self.assertEqual(wu.resolve("freedoom2.wad", "basic.wad", "MAP01", self.dirs),
                         (os.path.realpath(iw), os.path.realpath(pw), None))
        self.assertEqual(wu.resolve("freedoom2.wad", "", "MAP01", self.dirs), (os.path.realpath(iw), None, None))
        self.assertIn("map", wu.resolve("freedoom2.wad", "", "MAP 1", self.dirs)[2])
        self.assertIn("bare file name", wu.resolve("freedoom2.wad", "../basic.wad", "MAP01", self.dirs)[2])

    def test_the_directories_follow_doomsat_home(self):
        with mock.patch.dict(os.environ, {"DOOMSAT_HOME": self.tmp.name}):
            self.assertEqual(wu.search_dirs(), [os.path.join(self.tmp.name, "wads", "uplink"),
                                                os.path.join(self.tmp.name, "wads")])


class TestPinningTheProvenFile(unittest.TestCase):
    """The probe proves a file and the payload then loads it: a hard link makes sure it is the same file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DOOMSAT_HOME": self.tmp.name})
        self.env.start()
        os.makedirs(wu.uplink_dir())
        self.path = os.path.join(wu.uplink_dir(), "level.wad")
        with open(self.path, "wb") as f:
            f.write(b"\0" * 64)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_a_pin_is_the_same_file_under_the_same_name(self):
        pinned = wu.pin(self.path, 7)
        self.assertEqual(os.path.basename(pinned), "level.wad")
        self.assertTrue(os.path.samefile(pinned, self.path))
        self.assertIsNone(wu.name_problem(os.path.basename(pinned)))
        # nothing LOAD_WAD can name reaches it: names are bare, and the directory starts with a dot
        self.assertIn(os.sep + ".pinned" + os.sep, pinned)

    def test_a_new_uplink_renamed_over_the_name_does_not_touch_the_pin(self):
        pinned = wu.pin(self.path, 1)
        before = wu.identity(pinned)
        newer = self.path + ".123.part"
        with open(newer, "wb") as f:
            f.write(b"\1" * 32)
        os.replace(newer, self.path)                     # what the Doom component does on fileAnnounce
        self.assertEqual(wu.identity(pinned), before)
        self.assertNotEqual(wu.identity(self.path), before)

    def test_unpin_drops_the_links_and_keeps_the_files(self):
        wu.pin(self.path, 1)
        wu.pin(self.path, 2)
        wu.unpin(1)
        self.assertFalse(os.path.exists(os.path.join(wu.uplink_dir(), ".pinned", "1")))
        self.assertTrue(os.path.exists(os.path.join(wu.uplink_dir(), ".pinned", "2", "level.wad")))
        wu.unpin()
        self.assertEqual(os.listdir(os.path.join(wu.uplink_dir(), ".pinned")), [])
        self.assertTrue(os.path.isfile(self.path))

    def test_a_pin_that_cannot_be_made_says_so(self):
        self.assertIsNone(wu.pin(os.path.join(wu.uplink_dir(), "nothere.wad"), 3))
        self.assertIsNone(wu.identity(os.path.join(wu.uplink_dir(), "nothere.wad")))


class TestTheUplinkCodeKeepsTheCharter(unittest.TestCase):
    """research/honesty.py scans a fixed PILOT_SIDE list that does not include payload/wad_uplink.py, and
    research/ is not edited for this: hold the new code to the same patterns here (charter 2.4)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "research"))
        import honesty  # noqa: E402  its patterns only
        cls.honesty = honesty
        cls.src = _read("payload", "wad_uplink.py")

    def test_it_never_reads_a_level_file(self):
        for token in self.honesty.WAD_READING + self.honesty.CHEATS:
            self.assertNotIn(token, self.src)
        self.assertIsNone(re.search(r"\bopen\(", self.src), "LOAD_WAD hands paths to ViZDoom; it opens nothing")

    def test_it_names_no_level(self):
        for i, line in enumerate(self.src.splitlines(), 1):
            self.assertIsNone(self.honesty.LEVEL_NAME.search(line), "line %d names a level: %s" % (i, line.strip()))

    def test_it_imports_no_game(self):
        self.assertIsNone(re.search(r"^\s*(import|from)\s+(vizdoom|numpy|PIL)\b", self.src, re.M))

    def test_the_pilot_never_subscribes_to_the_wad_channels(self):
        pilot = _read("ground", "pilot.py")
        channels = pilot[pilot.index("STATUS_CHANNELS = ["):pilot.index("def _knowledge")]
        self.assertNotIn("WAD_", channels)

    def test_the_payload_has_one_game_and_sets_its_files_in_one_place(self):
        make = "\n".join(self.honesty._method_body(PAYLOAD, "_make_game"))
        self.assertEqual(PAYLOAD.count("vzd.DoomGame()"), 1)
        for call in ("set_doom_game_path(", "set_doom_scenario_path("):
            self.assertEqual(PAYLOAD.count(call), make.count(call), call)

    def test_the_bench_can_still_build_a_payload(self):
        """research/runner.py builds Payload from its own Namespace; every argument read without a default must be in it."""
        cls = PAYLOAD[PAYLOAD.index("class Payload:"):PAYLOAD.index("\ndef main():")]
        guarded = set(re.findall(r'getattr\((?:self\.)?args,\s*"(\w+)"', cls))
        used = set(re.findall(r"\bargs\.(\w+)", cls)) - guarded
        runner = _read("research", "runner.py")
        passed = set(re.findall(r"(\w+)=", re.search(r"dp\.Payload\(_ap\.Namespace\((.*?)\)\)", runner, re.S).group(1)))
        self.assertLessEqual(used, passed)


if __name__ == "__main__":
    unittest.main()
