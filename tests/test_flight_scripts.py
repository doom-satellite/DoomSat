"""The flight-side scripts and the topology header they copy, checked as text.

Two branches (the native FileUplink build and the CFDP spike) share one F´ project in $DOOMSAT_HOME, so each
sync must bring the topology header that fits its own topology, and the launchers must not depend on what the
other branch left in bin/. No network, no F´ install: everything here reads files in the repo (or the git
index), and the only code it runs is the health check's own reply printers.
"""
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read().replace("\r\n", "\n")


TOP = ("flight", "DoomSat", "Top")
# Component types in these topologies with health-ping ports (pingIn/pingOut in their FPP)
PINGED = {"Svc.ActiveRateGroup", "Svc.CmdSequencer", "Svc.Ccsds.Cfdp.CfdpManager", "Svc.FileManager", "Svc.PrmDb"}


class TestTheTopologyHeader(unittest.TestCase):
    def setUp(self):
        self.topology = _read(*TOP, "topology.fpp")
        self.instances = _read(*TOP, "instances.fpp")
        self.header = _read(*TOP, "DoomSatTopologyDefs.hpp")

    def test_every_imported_subtopology_has_its_ping_entries_and_state(self):
        # The autocoder names a subtopology's ping entries PingEntries::<Sub>_<instance>; only that subtopology's
        # PingEntries.hpp declares them, so a header missing one fails the compile on undeclared identifiers.
        imported = set(re.findall(r"^\s*import\s+(\w+)\.Subtopology\b", self.topology, re.M))
        self.assertIn("CdhCore", imported)
        pings = set(re.findall(r'#include "Svc/Subtopologies/(\w+)/PingEntries.hpp"', self.header))
        defs = set(re.findall(r'#include "Svc/Subtopologies/(\w+)/SubtopologyTopologyDefs.hpp"', self.header))
        state = set(re.findall(r"^\s*(\w+)::SubtopologyState\s+\w+;", self.header, re.M))
        self.assertEqual(pings, imported)
        self.assertEqual(defs, imported)
        self.assertEqual(state, imported)

    def test_the_header_pings_exactly_the_instances_that_answer_pings(self):
        # The autocoder emits PingEntries::DoomSat_<instance>::WARN/FATAL for every instance with health-ping ports
        # that the topology uses: a missing namespace is an undeclared identifier, an extra one is dead text. A new
        # component type with ping ports has to be added to PINGED here.
        types = dict(re.findall(r"^\s*instance\s+(\w+)\s*:\s*([\w.]+)", self.instances, re.M))
        used = set(re.findall(r"^\s*instance\s+(\w+)\s*$", self.topology, re.M))
        need = {i for i in used if types.get(i) in PINGED}
        pinged = set(re.findall(r"namespace\s+DoomSat_(\w+)\s*\{", self.header))
        self.assertTrue(need)
        self.assertEqual(pinged, need)

    def test_the_sync_copies_the_header(self):
        sync = _read("scripts", "wsl_sync.sh")
        copies = [l for l in sync.splitlines() if l.startswith("cp ") and "$DST/DoomSat/Top/" in l]
        self.assertEqual(len(copies), 1)
        self.assertIn("$SRC/DoomSat/Top/DoomSatTopologyDefs.hpp", copies[0])


class TestTheLaunchers(unittest.TestCase):
    def test_settings_the_run_script_reads_reach_wsl(self):
        # From Git Bash, flight.sh forwards named variables into WSL; one missing from its list is silently dropped
        run = _read("scripts", "wsl_run_flight.sh")
        read = set(re.findall(r"\$\{?(DOOMSAT_\w+)", run)) - {"DOOMSAT_HOME", "DOOMSAT_REPO", "DOOMSAT_OS"}
        forwarded = re.search(r"^\s*for v in ([^;]+); do .*PASS\+=", _read("scripts", "flight.sh"), re.M)
        self.assertIsNotNone(forwarded)
        self.assertLessEqual(read, set(forwarded.group(1).split()))

    def test_every_launcher_names_the_binary(self):
        # fprime-gds's find_app exits when bin/ holds more than one file, so a launcher that guesses breaks as soon
        # as a PrmDb.dat (or anything else) sits next to the binary.
        calls = []
        for name in ("wsl_run_flight.sh", "flight.sh"):
            for line in _read("scripts", name).splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"fprime-yamcs --deployment|launch\.py'? --deployment|fprime-gds -d", line):
                    calls.append((name, line))
        self.assertGreaterEqual(len(calls), 2, calls)
        for name, line in calls:
            self.assertRegex(line, r'--app "?\$DEPLOY/bin/DoomSat"?', f"{name}: {line.strip()}")

    @unittest.skipUnless(shutil.which("git") and os.path.exists(os.path.join(ROOT, ".git")), "needs git and a checkout")
    def test_every_script_with_a_shebang_is_executable(self):
        # The README and CLAUDE.md run these by path (scripts/flight.sh start, scripts/start_pilot.sh): one committed
        # as 100644 fails with "Permission denied" on a fresh clone. Read from git, which is what a clone gets.
        listed = subprocess.run(["git", "-C", ROOT, "ls-files", "-s", "--", "scripts/*.sh"], capture_output=True,
                                text=True, check=True).stdout
        entries = [line.split(None, 3) for line in listed.splitlines()]
        self.assertTrue(entries)
        for mode, _, _, path in entries:
            with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                if f.readline().startswith("#!"):
                    self.assertEqual(mode, "100755", f"{path} starts with a shebang but is not executable in git")


class TestTheHealthCheck(unittest.TestCase):
    """scripts/wsl_check.sh (flight.sh check) asks Yamcs for Doom channels by name. One the component no longer
    has comes back as an error, and used to print as None, which reads like a channel that has not updated yet."""

    PARAMETER = "DoomSat_DoomSat/DoomSat/doom/"

    def setUp(self):
        self.check = _read("scripts", "wsl_check.sh")
        # The loops that query one channel per name, each with the Python that prints the reply
        self.loops = re.findall(r'^for c in ([^;]+); do\n(.*?)^done$', self.check, re.M | re.S)
        self.loops = [(names.split(), body) for names, body in self.loops if self.PARAMETER + "$c" in body]

    def test_every_channel_it_polls_is_telemetry(self):
        channels = set(re.findall(r"^\s*telemetry\s+(\w+)\s*:", _read("flight", "Components", "Doom", "Doom.fpp"),
                                  re.M))
        polled = {name for names, _ in self.loops for name in names}
        polled |= set(re.findall(re.escape(self.PARAMETER) + r"(\w+)", self.check))   # named in the URL itself
        self.assertIn("FRAME_CHUNK", polled)
        self.assertIn("FRAMES_SENT", polled)
        self.assertEqual(polled - channels, set(), "not telemetry in Doom.fpp: Yamcs has no such parameter")

    def test_a_channel_yamcs_does_not_know_prints_as_missing(self):
        error = '{"code": 404, "type": "NotFoundException", "msg": "No parameter named NOPE"}'
        self.assertGreaterEqual(len(self.loops), 2)
        for names, body in self.loops:
            code = re.search(r'python3 -c "(.*?)" 2>/dev/null', body, re.S)
            self.assertIsNotNone(code, body)
            # As bash hands it over: the loop variable filled in, the double-quote escapes undone
            code = re.sub(r'\\([\\$"`])', r"\1", code.group(1).replace("$c", "NOPE"))
            with self.subTest(names[0]):
                out = subprocess.run([sys.executable, "-c", code], input=error, capture_output=True, text=True,
                                     timeout=30)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(out.stdout.strip(), "NOPE MISSING: No parameter named NOPE")


if __name__ == "__main__":
    unittest.main()
