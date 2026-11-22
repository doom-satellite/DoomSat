"""The science data system's own tests live in ground/sds/tests; this loads them, so the one command everyone runs
before a commit (python -m unittest discover -s tests) covers the SDS too.

They need no network, no game and no Airflow: Yamcs is replaced by a recorded window of the archive
(ground/sds/tests/data), and the few tests that need Pillow skip themselves without it.
"""
import glob
import importlib.util
import os
import sys
import unittest

SDS_TESTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ground", "sds", "tests")


def load_tests(loader, tests, pattern):
    # Loaded by path rather than with a nested loader.discover(): on Python 3.11 a discover() with another
    # top-level directory leaves the outer loader pointing at it, and every later test file then fails to load.
    for path in sorted(glob.glob(os.path.join(SDS_TESTS, "test_sds_*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        tests.addTests(loader.loadTestsFromModule(module))
    return tests


if __name__ == "__main__":
    unittest.main()
