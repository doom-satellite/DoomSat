"""fprime-yamcs stamps every F´ packet 1 s in the future: this patches the one constant that does it.

fprime-yamcs's FprimePacketPreprocessor (0.2.1, pinned, and 0.2.4, the latest, carry the same class) turns
an F´ time into a Yamcs instant as (seconds + 38) * 1000 + microseconds / 1000. Yamcs's own TimeEncoding
adds TAI-UTC, which has been 37 s since 2017, so every F´ telemetry point and event in Yamcs, its archive,
Open MCT and the SDS is stamped 1 s ahead of the ground clock that stamps commands and ground parameters
(docs/plans/fprime-yamcs-time.md). This rewrites that constant (bipush 38 -> bipush 37) in the installed
jar, and only when the class is byte for byte the known one; anything else is left as it is. The jar as
installed is kept beside it as <jar>.orig, which fprime-yamcs does not load (it loads *.jar). A Yamcs already
running keeps the class it loaded: restart it after a patch.

    python tools/yamcs_time_patch.py            apply (scripts/setup_flight.sh fprime runs it)
    python tools/yamcs_time_patch.py --check    report only; exit 0 when patched
    python tools/yamcs_time_patch.py --jar PATH another jar

Run it with the F´ venv's python (it finds the jar through the installed fprime_yamcs package), or point
it at the jar. Exit codes: 0 patched, 1 not patched (--check), 2 not a class this knows, 3 no jar found.
"""
import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path

CLASS = "com/example/myproject/FprimePacketPreprocessor.class"
UNPATCHED = "3f226b4b7cfaa235044bf86b66e0e91c92d1382e89714aa86bf6b72c9f301a1a"   # fprime-yamcs 0.2.1 to 0.2.4
PATCHED = "d40eb38b772e0a78569c066f9bd35eba30abf5645da44ea7a3f098c2879497bf"
OLD = bytes([0x10, 38, 0x36, 9])   # bipush 38; istore 9: the seconds offset, then the local it is kept in
NEW = bytes([0x10, 37, 0x36, 9])   # bipush 37: TAI-UTC, what Yamcs's TimeEncoding.fromUnixMillisec adds


def sha(data):
    return hashlib.sha256(data).hexdigest()


def find_jar():
    """The fprime-yamcs jar of the F´ venv this runs in, else the one under $DOOMSAT_HOME (default ~/doom)."""
    try:
        import fprime_yamcs   # noqa: PLC0415 (only the F´ venv has it; a namespace package, so no __file__)
        for d in fprime_yamcs.__path__:
            jars = sorted((Path(d) / "jars").glob("fprime-yamcs-*.jar"))
            if jars:
                return jars[0]
    except ImportError:
        pass
    home = Path(os.environ.get("DOOMSAT_HOME", Path.home() / "doom"))
    jars = sorted(home.glob("DoomSat/fprime-venv/lib/python3*/site-packages/fprime_yamcs/jars/fprime-yamcs-*.jar"))
    return jars[0] if jars else None


def state(jar, known=None):
    """'patched', 'unpatched', 'unknown' (a class this does not know) or 'missing'."""
    known = known or (UNPATCHED, PATCHED)
    if jar is None or not Path(jar).is_file():
        return "missing"
    try:
        with zipfile.ZipFile(jar) as z:
            data = z.read(CLASS)
    except (KeyError, zipfile.BadZipFile):
        return "unknown"
    return {known[0]: "unpatched", known[1]: "patched"}.get(sha(data), "unknown")


def patch(jar, known=None):
    """Rewrite the constant in place when the class is the known unpatched one; return the state after.

    Every other entry is copied as it is, with its name, date and compression. The new jar is written beside
    the old and moved over it in one step, so a failure leaves the jar as it was."""
    known = known or (UNPATCHED, PATCHED)
    s = state(jar, known)
    if s != "unpatched":
        return s
    jar = Path(jar)
    with zipfile.ZipFile(jar) as z:
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
    data = dict((i.filename, b) for i, b in entries)[CLASS]
    if data.count(OLD) != 1:
        return "unknown"
    fixed = data.replace(OLD, NEW)
    if sha(fixed) != known[1]:
        return "unknown"
    # The jar as installed now (a reinstall or upgrade replaces the last one), written whole before it is named
    backup, btmp = jar.with_name(jar.name + ".orig"), jar.with_name(jar.name + ".orig.tmp")
    btmp.write_bytes(jar.read_bytes())
    os.replace(btmp, backup)
    tmp = jar.with_name(jar.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as out:
        for info, blob in entries:
            out.writestr(info, fixed if info.filename == CLASS else blob)
    os.replace(tmp, jar)
    return state(jar, known)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="report only, change nothing")
    p.add_argument("--jar", type=Path, help="the fprime-yamcs jar (default: the installed one)")
    a = p.parse_args(argv)
    jar = a.jar or find_jar()
    before = state(jar)
    s = before if a.check else patch(jar)
    if s == "missing":
        print(f"fprime-yamcs time: no jar found ({jar or 'fprime_yamcs not installed'})")
        return 3
    if s == "unknown":
        print(f"fprime-yamcs time: {jar} is not a version this knows; left as it is. If it still adds 38 s, its "
              "F´ times are 1 s ahead (docs/plans/fprime-yamcs-time.md)")
        return 2
    if s == "unpatched":
        print(f"fprime-yamcs time: not patched, F´ times in Yamcs are 1 s ahead: {jar} "
              "(apply: scripts/flight.sh setup fprime, or this tool without --check)")
        return 1
    print(f"fprime-yamcs time: patched (TAI-UTC 37 s, as Yamcs): {jar}")
    if before == "unpatched":
        print("fprime-yamcs time: a Yamcs already running keeps the old class; restart it (scripts/flight.sh start)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
