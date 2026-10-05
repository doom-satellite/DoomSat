"""The flight-side scripts and the topology header they copy, checked as text.

Two branches (the native FileUplink build and the CFDP spike) share one F´ project in $DOOMSAT_HOME, so each
sync must bring the topology header that fits its own topology, and the launchers must not depend on what the
other branch left in bin/. No network, no F´ install: everything here reads files in the repo.
"""
import os
import re
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


if __name__ == "__main__":
    unittest.main()
