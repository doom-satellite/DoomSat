"""tools/yamcs_time_patch.py patches the one constant that puts fprime-yamcs's F´ times 1 s ahead, and nothing else.

No network, no install: the jars are built here. The class in them is a stand-in that carries the instruction
the tool looks for, and its hashes are passed in place of the real ones; the last test checks the real jar
when an F´ install is present.
"""
import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import yamcs_time_patch as fyt   # noqa: E402

CLASS = fyt.CLASS
STAND_IN = b"\xca\xfe\xba\xbe" + b"head" + fyt.OLD + b"tail: ldc2_w 1000l"
OTHERS = {"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n", "com/example/Other.class": b"\xca\xfe\xba\xbe" + fyt.OLD}


def sha(b):
    return hashlib.sha256(b).hexdigest()


KNOWN = (sha(STAND_IN), sha(STAND_IN.replace(fyt.OLD, fyt.NEW)))


class Patch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.jar = Path(self.dir.name) / "fprime-yamcs-1.0.0-SNAPSHOT.jar"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, cls=STAND_IN):
        with zipfile.ZipFile(self.jar, "w") as z:
            z.writestr(zipfile.ZipInfo("META-INF/MANIFEST.MF", (2026, 9, 15, 17, 51, 0)), OTHERS["META-INF/MANIFEST.MF"])
            z.writestr(zipfile.ZipInfo(CLASS, (2026, 9, 15, 17, 51, 0)), cls, compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("com/example/Other.class", OTHERS["com/example/Other.class"])
        return self.jar.read_bytes()

    def entries(self):
        with zipfile.ZipFile(self.jar) as z:
            return {i.filename: (z.read(i.filename), i.date_time, i.compress_type) for i in z.infolist()}

    def test_the_known_class_is_patched_and_nothing_else(self):
        self.write()
        before = self.entries()
        self.assertEqual(fyt.state(self.jar, KNOWN), "unpatched")
        self.assertEqual(fyt.patch(self.jar, KNOWN), "patched")
        after = self.entries()
        self.assertEqual(list(after), list(before), "same entries, same order")
        self.assertEqual(after[CLASS][0], STAND_IN.replace(fyt.OLD, fyt.NEW))
        self.assertEqual(after[CLASS][1:], before[CLASS][1:], "date and compression kept")
        for name in OTHERS:   # another class with the same bytes is not touched
            self.assertEqual(after[name], before[name], name)

    def test_the_jar_as_installed_is_kept_once(self):
        original = self.write()
        fyt.patch(self.jar, KNOWN)
        backup = self.jar.with_name(self.jar.name + ".orig")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual([p.name for p in self.jar.parent.glob("*.jar")], [self.jar.name],
                         "fprime-yamcs loads jars/*.jar: the backup must not be one")
        patched = self.jar.read_bytes()
        self.assertEqual(fyt.patch(self.jar, KNOWN), "patched")   # again: nothing changes
        self.assertEqual(self.jar.read_bytes(), patched)
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(self.jar.with_name(self.jar.name + ".tmp").exists())

    def test_a_reinstall_is_patched_again_and_backed_up_again(self):
        self.write()
        fyt.patch(self.jar, KNOWN)
        with zipfile.ZipFile(self.jar, "w") as z:   # pip put a fresh copy back, with another manifest
            z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\nBuilt-By: a reinstall\n")
            z.writestr(CLASS, STAND_IN)
        reinstalled = self.jar.read_bytes()
        self.assertEqual(fyt.state(self.jar, KNOWN), "unpatched")
        self.assertEqual(fyt.patch(self.jar, KNOWN), "patched")
        self.assertEqual(self.jar.with_name(self.jar.name + ".orig").read_bytes(), reinstalled)
        self.assertFalse(self.jar.with_name(self.jar.name + ".orig.tmp").exists())

    def test_a_class_it_does_not_know_is_left_alone(self):
        for cls in (STAND_IN + b"x", STAND_IN.replace(fyt.OLD, fyt.NEW) + b"x",   # another build, patched or not
                    STAND_IN.replace(fyt.OLD, fyt.OLD + fyt.OLD)):                 # or two sites
            with self.subTest(cls=cls[-12:]):
                original = self.write(cls)
                self.assertEqual(fyt.state(self.jar, KNOWN), "unknown")   # what doctor and flight.sh start report
                self.assertEqual(fyt.patch(self.jar, KNOWN), "unknown")
                self.assertEqual(self.jar.read_bytes(), original)
                self.assertFalse(self.jar.with_name(self.jar.name + ".orig").exists())

    def test_a_known_hash_with_two_sites_is_refused(self):
        twice = STAND_IN.replace(fyt.OLD, fyt.OLD + fyt.OLD)
        original = self.write(twice)   # both hashes match, so only the one-site guard can refuse it
        self.assertEqual(fyt.patch(self.jar, (sha(twice), sha(twice.replace(fyt.OLD, fyt.NEW)))), "unknown")
        self.assertEqual(self.jar.read_bytes(), original)

    def test_no_jar_and_no_class(self):
        self.assertEqual(fyt.state(None), "missing")
        self.assertEqual(fyt.state(self.jar), "missing")
        with zipfile.ZipFile(self.jar, "w") as z:
            z.writestr("x", b"y")
        self.assertEqual(fyt.state(self.jar, KNOWN), "unknown")
        self.jar.write_bytes(b"not a zip")
        self.assertEqual(fyt.state(self.jar, KNOWN), "unknown")

    def test_check_changes_nothing_and_exit_codes(self):
        original = self.write()
        fyt.UNPATCHED, saved = KNOWN[0], (fyt.UNPATCHED, fyt.PATCHED)
        fyt.PATCHED = KNOWN[1]
        try:
            self.assertEqual(fyt.main(["--check", "--jar", str(self.jar)]), 1)
            self.assertEqual(self.jar.read_bytes(), original)
            self.assertEqual(fyt.main(["--jar", str(self.jar)]), 0)
            self.assertEqual(fyt.main(["--check", "--jar", str(self.jar)]), 0)
            self.assertEqual(fyt.main(["--check", "--jar", str(self.jar) + ".missing"]), 3)
        finally:
            fyt.UNPATCHED, fyt.PATCHED = saved


class TheRealOne(unittest.TestCase):
    def test_the_constants_describe_one_byte(self):
        self.assertEqual(len(fyt.OLD), len(fyt.NEW))
        self.assertEqual([a != b for a, b in zip(fyt.OLD, fyt.NEW)], [False, True, False, False])
        self.assertEqual((fyt.OLD[1], fyt.NEW[1]), (38, 37))

    def test_the_installed_jar_is_one_this_knows(self):
        jar = fyt.find_jar()
        if jar is None:
            self.skipTest("no F´ install with fprime-yamcs here")
        self.assertIn(fyt.state(jar), ("patched", "unpatched"), f"{jar}: a new fprime-yamcs? see the tool's docstring")
        with zipfile.ZipFile(jar) as z:
            data = z.read(CLASS)
        unpatched = data if fyt.state(jar) == "unpatched" else data.replace(fyt.NEW, fyt.OLD, 1)
        self.assertEqual(sha(unpatched), fyt.UNPATCHED)
        self.assertEqual(sha(unpatched.replace(fyt.OLD, fyt.NEW)), fyt.PATCHED)


if __name__ == "__main__":
    unittest.main()
