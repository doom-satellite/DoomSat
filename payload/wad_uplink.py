"""LOAD_WAD on the payload side: the two link records, and which file names the game may be pointed at.

The operator uplinks a WAD to NAME.<nonce>.part in the uplink directory (F' file packets on the native build,
CFDP class 2 on the CFDP build). The Doom component renames it to NAME only once it is verified: on fileAnnounce,
which FileUplink sends after its checksum matches (native) and cfdpGuard sends with a clean class 2 FIN (CFDP), or
on a COMMIT_WAD whose size and CFDP checksum match the file on board (class 1, or a commit by hand). The operator
then commands LOAD_WAD; the Doom component forwards the three names as record kind 0x16, and the payload answers
with a WAD report, kind 3. The payload also sends a report when the link comes up, so the ground always knows which
file the game is running.
A LOAD_WAD sent again (its answer lost on a lossy link) must not switch the game twice: `load_key` says when two
requests ask for the same files and map.

Pure Python with no ViZDoom, so the unit tests import it from the ground venv. Nothing here reads a WAD:
names are checked as text and looked up with os.path, and the game is the only thing that ever opens one
(charter 2.4, test 2).

    flight -> payload  kind 0x16  iwad, pwad ('' for none), map
    payload -> flight  kind 3     result:u8 loads:u16, then iwad, pwad (what the game runs now),
                                  name (what the request asked for), map, reason
    every text is a length byte and its ASCII bytes
"""
import os
import re
import struct

KIND_LOAD_WAD = 0x16
KIND_WAD = 3
# A report on connect, a switch made, a request refused, and a request for the game already flying (no switch)
REPORT, LOADED, FAILED, ALREADY = 0, 1, 2, 3
# Yamcs counts a string argument's two-byte length tag against its declared size, so LOAD_WAD's
# `string size 40` carries 38 characters. The WadName telemetry array has room for 40.
NAME_MAX = 38
MAP_MAX = 8                           # a map lump name is at most eight characters
TEXT_MAX = 120                        # the longest text the Doom component keeps (WadLoadFailed's reason)
NAME_CHARS = re.compile(r"[A-Za-z0-9_.+-]+")
MAP_CHARS = re.compile(r"[A-Za-z0-9_]+")
PART = ".part"                        # an uplink still arriving, or one that failed its checksum


def pack_texts(*texts):
    out = b""
    for t in texts:
        b = (t or "").encode("ascii", "replace")[:255]
        out += bytes([len(b)]) + b
    return out


def unpack_texts(body, count, at=0):
    out = []
    for _ in range(count):
        if at >= len(body) or at + 1 + body[at] > len(body):
            raise ValueError("short WAD record")
        n = body[at]
        out.append(body[at + 1:at + 1 + n].decode("ascii", "replace"))
        at += 1 + n
    return out, at


def encode_load_wad(iwad, pwad, map_name):
    """The body the Doom component sends for LOAD_WAD (here for the tests and the dashboard's mirror)."""
    return pack_texts(iwad, pwad, map_name)


def decode_load_wad(body):
    (iwad, pwad, map_name), _ = unpack_texts(body, 3)
    return iwad, pwad, map_name


def encode_wad_report(result, loads, iwad, pwad, name="", map_name="", reason=""):
    return struct.pack("!BH", result, loads & 0xFFFF) + pack_texts(iwad, pwad, name, map_name, reason[:TEXT_MAX])


def decode_wad_report(body):
    if len(body) < 3:
        raise ValueError("short WAD record")
    result, loads = struct.unpack("!BH", body[:3])
    (iwad, pwad, name, map_name, reason), _ = unpack_texts(body, 5, 3)
    return dict(result=result, loads=loads, iwad=iwad, pwad=pwad, name=name, map=map_name, reason=reason)


def display_name(iwad, pwad):
    """How an operator says it: 'basic.wad over freedoom2.wad', or just the IWAD."""
    return f"{pwad} over {iwad}" if pwad else iwad


def name_problem(name, what="IWAD"):
    """Why `name` may not be handed to the game, or None. A bare file name ending in .wad, nothing else."""
    if not name:
        return f"no {what} named"
    if len(name) > NAME_MAX:
        return f"{what} name longer than {NAME_MAX} characters"
    if "/" in name or "\\" in name or ":" in name:
        return f"{what} must be a bare file name, not a path"
    if ".." in name:
        return f"{what} name may not contain '..'"
    if not name.endswith(".wad"):
        return f"{what} is not a .wad file"
    if name.startswith(".") or not NAME_CHARS.fullmatch(name):
        return f"{what} name may use only letters, digits and _ . + -"
    return None


def map_problem(map_name):
    if not map_name:
        return "no map named"
    if len(map_name) > MAP_MAX or not MAP_CHARS.fullmatch(map_name):
        return f"a map is up to {MAP_MAX} letters and digits"
    return None


def home():
    return os.environ.get("DOOMSAT_HOME", os.path.expanduser("~/doom"))


def wads_dir():
    return os.path.join(home(), "wads")


def uplink_dir():
    return os.path.join(wads_dir(), "uplink")


def search_dirs():
    """Where LOAD_WAD looks, in order: what was uplinked, then what was installed."""
    return [uplink_dir(), wads_dir()]


# find()'s reasons for a file that is not in place, which tools/wad_uplink_demo.py tells apart from a refused file
NOT_FINISHED = "has not finished its uplink"
NOT_FOUND = "is in neither the uplink nor the installed WAD directory"


def find(name, dirs):
    """(path, None) for a loadable file, or (None, why not). Existence and size only; nothing is read."""
    for d in dirs:
        root = os.path.realpath(d)
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root:
            return None, "resolves outside the WAD directories"
        if os.path.isfile(path):
            size = os.path.getsize(path)
            if size < 12:
                return None, f"{name} is {size} bytes, too short to be a WAD"
            return path, None
    for d in dirs:
        arriving = [f for f in (os.listdir(d) if os.path.isdir(d) else [])
                    if f.startswith(name + ".") and f.endswith(PART)]
        if arriving:
            return None, f"{name} {NOT_FINISHED} ({arriving[0]} so far)"
    return None, f"{name} {NOT_FOUND}"


def pin(path, serial):
    """A hard link to `path` under the uplink directory's `.pinned/<serial>/`, keeping its file name.

    LOAD_WAD proves a file in a child process and then loads it in the payload. Between the two an uplink of
    the same name can be renamed over it, and the game would then load a file nobody proved: a damaged one
    kills the payload. A hard link holds the inode that was proven. Linking reads nothing. Returns None when
    the link cannot be made (another file system, say); the caller then compares `identity()` instead.
    """
    d = os.path.join(uplink_dir(), ".pinned", str(serial))
    try:
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, os.path.basename(path))
        if os.path.lexists(dest):
            os.unlink(dest)
        os.link(path, dest)
        return dest
    except OSError:
        return None


def unpin(serial=None):
    """Drop the links for one request, or (serial None) every one: names only, the files stay where they are."""
    root = os.path.join(uplink_dir(), ".pinned")
    for d in ([os.path.join(root, str(serial))] if serial is not None else
              [os.path.join(root, x) for x in (os.listdir(root) if os.path.isdir(root) else [])]):
        for f in (os.listdir(d) if os.path.isdir(d) else []):
            try:
                os.unlink(os.path.join(d, f))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


def identity(path):
    """What says a path still names the same file: device, inode, size and modification time."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def files_key(ipath, ppath):
    """The IWAD and the PWAD (None for none) as files: each one's name, through any symbolic link, and identity.
    None if a file is not there.

    Names alone would not do: an uplink of the same name renamed over the file is a new file. A pinned link (`pin`)
    has the name and the identity of the file it pins, and `find` returns real paths, so a game launched under a
    linked name compares equal to a request that resolves to the same file.
    """
    files = []
    for path in (ipath, ppath):
        ident = identity(path) if path else None
        if path and ident is None:
            return None
        files.append((os.path.basename(os.path.realpath(path)), ident) if path else None)
    return files[0], files[1]


def load_key(ipath, ppath, map_name):
    """What a LOAD_WAD asks for: its files (`files_key`) and the map; None if a file is not there. Two requests
    with the same key ask for the same game, so the second changes nothing."""
    files = files_key(ipath, ppath)
    return None if files is None else (files[0], files[1], map_name.upper())


def resolve(iwad, pwad, map_name, dirs=None):
    """(iwad_path, pwad_path or None, None), or (None, None, why not) for a LOAD_WAD request."""
    dirs = dirs or search_dirs()
    problem = name_problem(iwad, "IWAD") or (pwad and name_problem(pwad, "PWAD")) or map_problem(map_name)
    if problem:
        return None, None, problem
    ipath, why = find(iwad, dirs)
    if why:
        return None, None, why
    ppath = None
    if pwad:
        ppath, why = find(pwad, dirs)
        if why:
            return None, None, why
    return ipath, ppath, None
