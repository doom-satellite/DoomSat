"""The honesty boundary around the science data system: products never flow back to the pilot.

A path image of a level is a map of it, and charter 2.2 says no map survives from one attempt to the next. The SDS
exists to keep exactly such things (path images, per-episode records, captured frames, the payload's own map PNGs),
so the one way it could break the charter is by being reachable from the pilot. research/honesty.py scans a fixed
list of files (PILOT_SIDE) and cannot see ground/sds at all, so these tests draw the line from both sides:

1. nothing the pilot process runs names the SDS: its package, its home, its bucket, its catalog;
2. the SDS writes nowhere the pilot reads (the pilot shows out/frames/latest_map.png to a model as its map);
3. nothing in the SDS opens a WAD (only research/grader may, CLAUDE.md);
4. the SDS is read-only by default: its one command is cfdpManager SendFile (class 2, keeping the file on board),
   sent from a module named for it; of CFDP it only reads the transfer list and the cfdpDown bucket, and deletes the
   one object it has ingested, in record.forget_downlinked alone (a URL is followed through the module's helpers
   and record's BUCKET and object_url, not only through the strings in a call);
5. its processes survive research/preflight.py --kill and the flight side's pkill, which both match by command
   line, and its own stop cannot take down the flight;
6. its DAGs and its package import nothing from the pilot.

Every scan has a canary, as research/honesty.py does: a planted leak the matcher must catch, so that a scan that
passes says something. The scans read source only: no SDS module that needs Airflow or yamcs-client is imported.
"""
import ast
import datetime as dt
import functools
import glob
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # ground/sds
REPO = os.path.dirname(os.path.dirname(SDS))
sys.path.insert(0, SDS)

from doomsat_sds import capture, config, products, record, store  # noqa: E402


def _load_honesty():
    """research/honesty.py, read-only and under a private name, so the repo suite's own import is untouched."""
    spec = importlib.util.spec_from_file_location("_sds_guard_honesty", os.path.join(REPO, "research", "honesty.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


honesty = _load_honesty()


def read(path):
    """A repo file by its repo-relative path."""
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return f.read()


def rel(path):
    return os.path.relpath(path, REPO).replace(os.sep, "/")


# ------------------------------------------------------------------------------------------------ what is pilot side
# Modules the pilot and payload processes import that honesty.PILOT_SIDE does not list. The import closure below
# finds these and any added later; the explicit list is a floor, so a closure that silently found nothing would fail.
PILOT_EXTRA = ["payload/world_model.py", "payload/executor.py", "payload/mapclasses.py", "payload/oracle.py",
               "ground/targeting.py"]
PILOT_NAMES = {"pilot", "decision_graph", "targeting", "providers", "after_action", "graph_config"}


def imported_modules(text):
    """Top-level names of every absolute import in `text`, nested ones included (the oracle is imported in a branch)."""
    names = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    names.update(re.findall(r"""(?:import_module|__import__)\(\s*['"](\w+)""", text))
    return names


@functools.lru_cache(maxsize=None)
def pilot_import_closure():
    """Every repo module the pilot side reaches by importing a sibling (payload/ and ground/ each put their own
    directory on sys.path), starting from the Python files in honesty.PILOT_SIDE."""
    todo = [p for p in honesty.PILOT_SIDE if p.endswith(".py")]
    seen = set()
    while todo:
        path = todo.pop()
        if path in seen or not os.path.isfile(os.path.join(REPO, path)):
            continue
        seen.add(path)
        folder = os.path.dirname(path)
        for name in imported_modules(read(path)):
            candidate = folder + "/" + name + ".py"
            if os.path.isfile(os.path.join(REPO, candidate)):
                todo.append(candidate)
    return frozenset(seen)


def pilot_side_files():
    graphs = sorted(rel(p) for p in glob.glob(os.path.join(REPO, "ground", "graph", "*.json")))
    return sorted(set(honesty.PILOT_SIDE) | set(PILOT_EXTRA) | pilot_import_closure() | set(graphs))


# ------------------------------------------------------------------------------------------------ what is the SDS
def sds_files(suffixes=(".py",)):
    """The package and the DAGs (not the tests, which name everything they guard against)."""
    out = []
    for sub in ("doomsat_sds", "dags"):
        for root, dirs, files in os.walk(os.path.join(SDS, sub)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            out += [os.path.join(root, f) for f in sorted(files) if suffixes is None or f.endswith(suffixes)]
    return sorted(out)


def sds_sources():
    return {rel(p): Path(p).read_text(encoding="utf-8") for p in sds_files()}


def code_strings(text):
    """String literals that are not docstrings: what the code actually uses, not what it says about itself."""
    tree = ast.parse(text)
    docs = set()
    for node in [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                       ast.ClassDef))]:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def own_calls(func):
    """The calls a function makes itself, not those of functions defined inside it."""
    stack, out = list(func.body), []
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def calls(text, attr):
    return [n for n in ast.walk(ast.parse(text))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


# ------------------------------------------------------------------------------------------------ the matchers
SDS_MARKERS = ("doomsat_sds", "ground/sds", "DOOMSAT_SDS", "doomsat-sds", "out/sds", "catalog.sqlite",
               "sds/products", "sds/capture")
SDS_LITERAL = re.compile(r"""['"]sds['"]""")        # os.path.join(home, "sds") or Path(home) / "sds"


def sds_mentions(text):
    flat = re.sub(r"[\\/]+", "/", text)                 # ground\sds on Windows is still ground/sds
    found = [m for m in SDS_MARKERS if m in flat]
    if SDS_LITERAL.search(text):
        found.append("a bare 'sds' path component")
    return found


def sds_imports(text):
    try:
        names = imported_modules(text)
    except SyntaxError:                                 # a probe that no longer parses is still scanned as text
        names = set(re.findall(r"^\s*(?:from|import)\s+(\w+)", text, re.M))
    return ["imports doomsat_sds"] if "doomsat_sds" in names else []


PILOT_READS = ("out/frames", "latest_map")
OUT_FRAMES_JOINED = re.compile(r"""['"]out['"]\s*\)?\s*[,/]\s*['"]frames['"]""")


def pilot_read_locations(text):
    flat = re.sub(r"[\\/]+", "/", text)
    found = [m for m in PILOT_READS if m in flat]
    if OUT_FRAMES_JOINED.search(text):
        found.append("out + frames joined")
    return found


WAD_PATTERNS = [
    (re.compile(r"^\s*(?:import|from)\s+omg\b", re.M), "imports omgifol"),
    (re.compile(r"\bomg\.\w+"), "uses omgifol"),
    (re.compile(r"\b[IP]WAD\b"), "a WAD header magic"),
    (re.compile(r"""['"]wads['"]|/wads\b"""), "the WAD directory"),
]
OPENER = re.compile(r"\bopen\s*\(|read_bytes\s*\(|read_text\s*\(|\bmmap\b|\bFileIO\b")


def wad_reads(text):
    found = [t for t in honesty.WAD_READING if t in text]        # the repo's own list: lumps, wad_map, the grader
    found += [why for rx, why in WAD_PATTERNS if rx.search(text)]
    for i, line in enumerate(text.splitlines(), 1):
        if "wad" in line.lower() and OPENER.search(line):
            found.append("line %d opens something named like a WAD: %s" % (i, line.strip()[:80]))
    return found


CHANNELS = set(config.SCIENCE) | set(config.LINK) | set(config.CONTEXT_TLM) | {config.short(config.FRAME_CHUNK)}
SENDING_CALLS = ("create_command_connection", "set_parameter_value", "set_parameter_values")


def _is_sendfile(node):
    return ((isinstance(node, ast.Attribute) and node.attr == "SENDFILE")
            or (isinstance(node, ast.Name) and node.id == "SENDFILE")
            or (isinstance(node, ast.Constant) and node.value == config.SENDFILE))


def command_findings(name, text):
    """Everything in one SDS source file that could command the flight side or write into the pilot's processor.

    Allowed: parameter paths under the doom namespace that name a telemetry channel, and SENDFILE. issue_command
    only in a module whose file name says "record", only with SENDFILE as the command.
    """
    out = []
    base = os.path.basename(name)
    for m in re.finditer(r"/DoomSat_DoomSat/[A-Za-z0-9_/]*", text):
        path = m.group(0)
        if path == config.SENDFILE or path in (config.NAMESPACE, config.NAMESPACE.rstrip("/")):
            continue
        if path.startswith(config.NAMESPACE) and path[len(config.NAMESPACE):] in CHANNELS:
            continue
        out.append("names %s, which is neither a telemetry channel nor SendFile" % path)
    if "issue_command" in text:
        if "record" not in base:
            out.append("issue_command in %s, a module not named for the SendFile request" % base)
        if "SENDFILE" not in text:
            out.append("issue_command without SENDFILE")
    for call in calls(text, "issue_command"):
        command = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "command"), None)
        if command is None or not _is_sendfile(command):
            out.append("line %d issues a command other than SENDFILE" % call.lineno)
    for attr in SENDING_CALLS:
        for call in calls(text, attr):
            out.append("line %d calls %s" % (call.lineno, attr))
    for s in code_strings(text):
        if "/commands" in s:
            out.append("a REST command endpoint: %r" % s[:60])
        if "/DoomGround" in s:
            out.append("the pilot's /DoomGround parameters: %r" % s[:60])
    if "record" not in base and (re.search(r"\brecord\.request\s*\(", text)
                                 or re.search(r"from\s+[\w.]*record\s+import\s+[^\n]*\brequest\b", text)):
        out.append("calls record.request (the SendFile sender) outside a record module")
    return out


# yamcs-client's file transfer calls: the two that start a transfer and the four that steer one.
TRANSFER_STARTS = ("upload", "download")
TRANSFER_CONTROL = ("cancel_transfer", "pause_transfer", "resume_transfer", "run_file_action")
HTTP_WRITES = ("post", "put", "patch", "delete")
# yamcs-client's storage writes (StorageClient, Bucket): writes like the HTTP ones, a finding when they name cfdpDown
STORAGE_WRITES = ("upload_object", "delete_object", "create_bucket", "remove_bucket")
NEEDLES = ("/filetransfer", "cfdpDown")


def _bound_here(tree):
    """Every name a module binds itself: assignments, functions, classes, imports and arguments."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.arg):
            out.add(n.arg)
    return out


def _own_nodes(scope):
    """The nodes of a module or function that are not inside a function defined in it."""
    stack, out = list(ast.iter_child_nodes(scope)), []
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
ANSWERS = ("get", "head", "options", "request") + HTTP_WRITES     # what these return is an answer, not an address


def standing_for(tree, needle, from_record=frozenset()):
    """(names, holds) for one module: the module-level names that stand for `needle` (the endpoint or the bucket),
    and holds(node, function=None), whether an expression in that function (or at module level) does.

    A name stands for it when it is assigned a value that holds it (in its own scope), or is a function that
    returns one, followed through each other until nothing new turns up: so record.object_url, which builds its
    URL from BUCKET, stands for cfdpDown. What a request returns does not (r = http.get(url) is an answer).
    `from_record` are the top-level names that stand for it in record.py: they count here as record.X, when
    imported from record, and bare when this module does not bind the name itself (publish.py has a BUCKET and an
    object_url of its own, for the doomsat-sds bucket)."""
    names = {n for n in from_record if n not in _bound_here(tree)}
    aliases = {"record"}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            aliases.update(a.asname or a.name for a in n.names if a.name == "record")
            if n.module and n.module.split(".")[-1] == "record":
                names.update(a.asname or a.name for a in n.names if a.name in from_record)
    functions = [f for f in ast.walk(tree) if isinstance(f, FUNCTIONS)]
    local = {id(f): set() for f in functions}
    hidden = {id(f): {a.arg for a in ast.walk(f.args) if isinstance(a, ast.arg)} for f in functions}

    def holds(node, function=None, value=False):
        """`value`: node is a value being assigned or returned, which a request's answer never stands for."""
        mine = local[id(function)] if function is not None else set()
        seen = (names - hidden[id(function)]) | mine if function is not None else names
        stack = [node]
        while stack:
            m = stack.pop()
            if (value or m is not node) and isinstance(m, ast.Call) and isinstance(m.func, ast.Attribute) \
                    and m.func.attr in ANSWERS:
                continue
            if isinstance(m, ast.Constant) and isinstance(m.value, str) and needle in m.value:
                return True
            if isinstance(m, ast.Name) and m.id in seen:
                return True
            if isinstance(m, ast.Attribute) and m.attr in from_record and (
                    (isinstance(m.value, ast.Name) and m.value.id in aliases)
                    or (isinstance(m.value, ast.Attribute) and m.value.attr == "record")):
                return True
            stack.extend(ast.iter_child_nodes(m))
        return False

    def assigned(scope, into, function=None):
        for n in _own_nodes(scope):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and holds(n.value, function, True):
                into.update(t.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                            if isinstance(t, ast.Name))

    while True:
        before = len(names) + sum(len(v) for v in local.values())
        assigned(tree, names)
        for f in functions:
            assigned(f, local[id(f)], f)
            if any(isinstance(r, ast.Return) and r.value is not None and holds(r.value, f, True)
                   for r in _own_nodes(f)):
                names.add(f.name)
        if len(names) + sum(len(v) for v in local.values()) == before:
            return names, holds


def _record_exports():
    """The top-level names of record.py that stand for each needle (BUCKET and object_url for cfdpDown)."""
    tree = ast.parse(read("ground/sds/doomsat_sds/record.py"))
    top = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    top |= {n.name for n in tree.body if isinstance(n, FUNCTIONS)}
    return {needle: frozenset(standing_for(tree, needle)[0] & top) for needle in NEEDLES}


RECORD_EXPORTS = _record_exports()


def file_transfer_findings(name, text):
    """Everything in one SDS source file that could start, steer or cancel a CFDP transfer, or write to cfdpDown.

    A write is an HTTP POST, PUT, PATCH or DELETE (as a method, or named to .request) or one of yamcs-client's
    storage writes. It names /filetransfer or cfdpDown when anything in the call stands for it (standing_for): a
    string, a variable or a function of the module, or record's BUCKET and object_url. Allowed: GETs of both, and
    one DELETE of a cfdpDown object, in record.forget_downlinked (the ingested record, on the opt-in Phase D path).
    upload and download count as yamcs-client's unless the module defines a method of that name
    (publish.Publisher.upload writes the doomsat-sds bucket).
    """
    out = []
    base = os.path.basename(name)
    tree = ast.parse(text)
    own = {n.name for n in ast.walk(tree) if isinstance(n, FUNCTIONS)}
    holds = {needle: standing_for(tree, needle, RECORD_EXPORTS[needle])[1] for needle in NEEDLES}
    enclosing = {}
    for f in ast.walk(tree):                     # outer functions come first, so the innermost one is kept
        if isinstance(f, FUNCTIONS):
            enclosing.update({id(c): f for c in ast.walk(f) if isinstance(c, ast.Call)})

    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        attr = call.func.attr
        if attr in TRANSFER_CONTROL:
            out.append("line %d calls %s, which steers a file transfer" % (call.lineno, attr))
        elif attr in TRANSFER_STARTS and attr not in own:
            out.append("line %d calls %s, which starts a file transfer" % (call.lineno, attr))
        method = attr if attr in HTTP_WRITES + STORAGE_WRITES else None
        if attr == "request" and call.args and isinstance(call.args[0], ast.Constant) \
                and str(call.args[0].value).lower() in HTTP_WRITES:
            method = str(call.args[0].value).lower()
        if method is None:
            continue
        function = enclosing.get(id(call))
        if holds["/filetransfer"](call, function):
            out.append("line %d sends %s to /filetransfer" % (call.lineno, method.upper()))
        forget = method == "delete" and base == "record.py" and getattr(function, "name", None) == "forget_downlinked"
        if holds["cfdpDown"](call, function) and not forget:
            out.append("line %d sends %s to the cfdpDown bucket" % (call.lineno, method.upper()))
    if "record" not in base and re.search(r"\bforget_downlinked\s*\(", text):
        out.append("deletes the ingested cfdpDown object (forget_downlinked) outside a record module")
    return out


def pilot_imports(text):
    forbidden = PILOT_NAMES | {"ground", "payload"} | {
        os.path.splitext(os.path.basename(p))[0] for p in set(honesty.PILOT_SIDE) | pilot_import_closure()
        if p.endswith(".py")}
    return sorted(imported_modules(text) & forbidden)


# ------------------------------------------------------------------------------------------------ process lines
def shell_expand(s, env):
    pattern = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")
    for _ in range(5):
        s = pattern.sub(lambda m: env.get(m.group(1) or m.group(3), m.group(2) or ""), s)
    return s


def orphan_patterns():
    """research/preflight.py's ORPHAN_PATTERNS, parsed: importing it runs sys.path edits and imports honesty."""
    for node in ast.parse(read("research/preflight.py")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ORPHAN_PATTERNS" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("research/preflight.py has no ORPHAN_PATTERNS")


def pkill_patterns(text):
    return re.findall(r'\b(?:pkill|pgrep)\s+(?:-[A-Za-z]+\s+)*?-f\s+"([^"]+)"', text)


def flight_patterns():
    """What the flight side kills (pkill -f) and counts as its own (status: grep -E) in wsl_run_flight.sh."""
    text = read("scripts/wsl_run_flight.sh")
    status = [p for alt in re.findall(r'grep\s+-E\s+"([^"]+)"', text) for p in alt.split("|")]
    return pkill_patterns(text), status


def sds_env(doomsat_home):
    text = read("scripts/sds.sh")
    env = {"DOOMSAT_HOME": doomsat_home, "HOME": os.path.dirname(doomsat_home),
           "DOOMSAT_REPO": "/home/pilot/DoomSat"}
    for name in ("SDS_HOME", "VENV", "COMPONENTS", "SDS_PORT"):
        m = re.search(r'^%s="([^"]*)"' % name, text, re.M)
        if m:
            env[name] = shell_expand(m.group(1), env)
    return env


def sds_command_lines(doomsat_home):
    """Every command line `scripts/sds.sh start` puts in the process table, as (name, line) pairs: the bash wrapper
    `detach` runs (it holds the pid-file and log paths until it execs), and the exec'd process as ps shows it."""
    text = read("scripts/sds.sh")
    env = sds_env(doomsat_home)
    out = []
    for name, command in re.findall(r'^\s*detach\s+("?\$?\w+"?)\s+"(.*)"\s*$', text, re.M):
        name = name.strip('"')
        names = env["COMPONENTS"].split() if name.startswith("$") else [name]
        for n in names:
            e = dict(env, **{name.lstrip("$"): n})
            line = shell_expand(command, e)
            pidfile = "%s/run/%s.pid" % (env["SDS_HOME"], n)
            out.append((n, "bash -c echo $$ > '%s'; exec %s" % (pidfile, line)))
            argv = re.split(r"\s+[12]?>", line)[0].replace("'", "")
            out.append((n, argv))
            if not argv.startswith(env["VENV"] + "/bin/python"):     # an entry-point script runs under its shebang
                out.append((n, env["VENV"] + "/bin/python " + argv))
    return out


# Process titles Airflow 3.3.2 gives its own processes (setproctitle in api_server_command.py, serve_logs/core.py,
# executors/local_executor.py, executors/base_executor.py and sdk/execution_time/task_runner.py), as seen live on
# 4 October 2026. Task processes are forked from the supervisor, so task_runner.py -- whose name contains
# preflight's "runner.py" -- is not on any command line. An upgrade that started tasks as `python .../task_runner.py`
# would make preflight --kill take down every running SDS task; a live ps is the only check for that.
AIRFLOW_TITLES = [
    "airflow api_server -- host:127.0.0.1 port:8080",
    "airflow serve-logs",
    "airflow worker -- LocalExecutor: <idle>",
    "airflow worker -- LocalExecutor: 01a10482-e810-709b-b6f2-8e8f8785a34a",
    "airflow supervisor: 01a10482-e810-709b-b6f2-8e8f8785a34a",
    "airflow worker -- 01a10482-e810-709b-b6f2-8e8f8785a34a",
]


def killer_hits(line, orphans, patterns):
    hits = ["preflight kills %r" % p for p in orphans if p in line]       # preflight: `p in cmd`
    hits += ["pkill -f %r" % p for p in patterns if re.search(p, line)]
    return hits


HOMES = ("/root/doom", "/home/pilot/doom")


# ================================================================================================ 1. pilot side
class PilotSideNeverNamesTheSds(unittest.TestCase):
    def test_the_files_scanned_exist(self):
        # honesty.read_sources turns a missing file into None, so a renamed file would leave a scan passing on nothing.
        for path in list(honesty.PILOT_SIDE) + PILOT_EXTRA:
            self.assertTrue(os.path.isfile(os.path.join(REPO, path)), "%s is gone: update the guard" % path)
        self.assertTrue(glob.glob(os.path.join(REPO, "ground", "graph", "*.json")), "no ground/graph/*.json to scan")

    def test_the_import_closure_finds_the_modules_the_fixed_list_misses(self):
        closure = pilot_import_closure()
        for path in PILOT_EXTRA:
            self.assertIn(path, closure, "%s is no longer imported by the pilot side, or the closure is broken" % path)
        self.assertTrue(set(p for p in honesty.PILOT_SIDE if p.endswith(".py")) <= closure)

    def test_no_pilot_side_file_names_the_sds(self):
        bad = []
        for path in pilot_side_files():
            for found in sds_mentions(read(path)):
                bad.append("%s: %s" % (path, found))
        self.assertEqual(bad, [], "the pilot side can reach the SDS (charter 2.2: no map survives an attempt)")

    def test_no_payload_or_ground_module_imports_the_sds(self):
        files = glob.glob(os.path.join(REPO, "payload", "*.py")) + glob.glob(os.path.join(REPO, "ground", "*.py"))
        self.assertTrue(files)
        bad = [rel(p) for p in sorted(files) if sds_imports(Path(p).read_text(encoding="utf-8"))]
        self.assertEqual(bad, [])

    def test_canary_planted_references_are_caught(self):
        planted = [
            "from doomsat_sds import products",
            'sys.path.insert(0, os.path.join(ROOT, "ground", "sds"))',
            'sys.path.insert(0, r"C:\\DoomSat\\ground\\sds")',
            'home = os.environ.get("DOOMSAT_SDS_HOME")',
            "png = bucket_read('doomsat-sds', 'episodes/x/l2_path-2.png')",
            'Path("~/doom/sds/products/episodes").expanduser()',
            'sqlite3.connect(home / "catalog.sqlite")',
            'frames = Path.home() / "doom" / "sds" / "capture"',
            "latest = 'out/sds/latest.png'",
        ]
        for line in planted:
            self.assertTrue(sds_mentions(line), "not caught: %s" % line)
        self.assertEqual(sds_mentions(read("ground/pilot.py")), [])

    def test_canary_planted_imports_are_caught(self):
        for line in ("import doomsat_sds", "from doomsat_sds.products import render_path",
                     "def f():\n    import doomsat_sds.catalog as c\n", "m = importlib.import_module('doomsat_sds')",
                     "print 'old probe'\nimport doomsat_sds\n"):
            self.assertTrue(sds_imports(line), "not caught: %r" % line)
        self.assertEqual(sds_imports("import sds_helpers\nfrom ground import pilot"), [])


# ================================================================================================ 2. pilot reads
class SdsNeverWritesWherePilotReads(unittest.TestCase):
    def test_the_pilot_really_reads_its_map_from_out_frames(self):
        # If the pilot moves its map, this guard is watching the wrong place: move it too. The path is built in three
        # steps (--out-dir defaults to <repo>/out, frames go in out_dir/"frames", the map is that dir/"latest_map.png"
        # and is what ascii_map shows the model), so each step is checked, not just two strings anywhere in the file.
        text = read("ground/pilot.py")
        self.assertRegex(text, r'HERE\s*=\s*Path\(__file__\)\.resolve\(\)\.parent\b')          # ground/
        self.assertRegex(text, r'"--out-dir"[^\n]*default=HERE\.parent\s*/\s*"out"')               # <repo>/out
        self.assertRegex(text, r'FrameAssembler\(\s*args\.out_dir\s*/\s*"frames"\s*\)')            # out/frames
        self.assertRegex(text, r'ascii_map\(\s*self\.frames\.out_dir\s*/\s*"latest_map\.png"\s*\)')  # shown to a model

    def test_no_sds_file_names_out_frames_or_latest_map(self):
        files = sds_files(suffixes=None) + [os.path.join(REPO, "scripts", "sds.sh")]
        bad = []
        for path in files:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            bad += ["%s: %s" % (rel(path), f) for f in pilot_read_locations(text)]
        self.assertEqual(bad, [])

    def test_canary_planted_writes_are_caught(self):
        for line in ('write_atomic(REPO / "out/frames/latest_map.png", data)', "p = 'out\\\\frames\\\\x.png'",
                     'os.path.join("out", "frames")', 'Path("out") / "frames" / name', "copy(src, 'latest_map.png')"):
            self.assertTrue(pilot_read_locations(line), "not caught: %s" % line)
        self.assertEqual(pilot_read_locations('settings.capture / "frames" / day'), [])

    def test_every_place_the_sds_writes_is_under_its_own_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = config.Settings(home=Path(tmp) / "sds", doomsat_home=Path(tmp) / "doom")
            written = [s.products, s.catalog, s.capture, s.lineage,
                       store.episode_product_path(s, "20261004T004647Z-e0002", "l2_path", products.version("l2_path"),
                                                  "png"),
                       store.l3_product_path(s, "l3_rollup", products.version("l3_rollup"), "ab" * 32),
                       store.quicklook_path(s, "ql_contact_sheet", "20261004T010137Z", "png"),
                       capture.Capture(s).root]
            for p in written:
                self.assertTrue(Path(p).resolve().is_relative_to(s.home.resolve()), "%s is outside %s" % (p, s.home))
            with mock.patch.dict(os.environ, {"DOOMSAT_HOME": str(Path(tmp) / "doom")}):
                os.environ.pop("DOOMSAT_SDS_HOME", None)
                self.assertEqual(config.Settings.from_env().home, Path(tmp) / "doom" / "sds")
            with mock.patch.dict(os.environ, {"DOOMSAT_SDS_HOME": str(Path(tmp) / "elsewhere")}):
                self.assertEqual(config.Settings.from_env().home, Path(tmp) / "elsewhere")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("DOOMSAT_HOME", None)
            os.environ.pop("DOOMSAT_SDS_HOME", None)
            default = config.Settings.from_env().home           # computed, never touched
        self.assertEqual(default, Path.home() / "doom" / "sds")
        self.assertFalse(default.is_relative_to(Path(REPO) / "out"), "the SDS home must not be in the repo's out/")


# ================================================================================================ 3. WADs
class SdsNeverOpensAWad(unittest.TestCase):
    def test_no_sds_source_reads_a_wad(self):
        bad = ["%s: %s" % (path, f) for path, text in sorted(sds_sources().items()) for f in wad_reads(text)]
        self.assertEqual(bad, [], "only research/grader may open a WAD (CLAUDE.md, charter 2.4)")

    def test_canary_planted_wad_reads_are_caught(self):
        planted = [
            'lumps = wad.read_lump("LINEDEFS")',
            "things = lumps['THINGS']",
            "import omg",
            "from omg import WAD, MapEditor",
            "w = omg.WAD(path)",
            'data = open(os.path.join(home, "wads", "doom1.wad"), "rb").read()',
            "raw = (settings.doomsat_home / 'wads' / 'freedoom1.wad').read_bytes()",
            "assert head[:4] in (b'IWAD', b'PWAD')",
            "from research.grader import wad_map",
            "stats = wad_stats(path)",
        ]
        for line in planted:
            self.assertTrue(wad_reads(line), "not caught: %s" % line)
        # Naming the WAD a flight used is context, not level knowledge (the catalog has a wad column).
        self.assertEqual(wad_reads('PAYLOAD_DEFAULTS = {"--wad": "doom1.wad"}\nctx["wad"] = opts.get("--wad")'), [])


# ================================================================================================ 4. read-only
class SdsIsReadOnlyByDefault(unittest.TestCase):
    def test_the_one_command_is_sendfile(self):
        self.assertEqual(config.SENDFILE, "/DoomSat_DoomSat/DoomSat/cfdpManager/SendFile")
        bad = ["%s: %s" % (path, f) for path, text in sorted(sds_sources().items())
               for f in command_findings(path, text)]
        self.assertEqual(bad, [])

    def test_issue_command_appears_only_in_a_record_module_beside_sendfile(self):
        users = [path for path, text in sds_sources().items() if "issue_command" in text]
        for path in users:
            self.assertIn("record", os.path.basename(path), path)
            self.assertIn("SENDFILE", sds_sources()[path], path)

    def test_the_command_issued_keeps_the_file_on_board_and_is_class_2(self):
        # keep DELETE (enum value 0) would remove the payload's record once sent, and Yamcs requires all seven
        # arguments; what record.request hands yamcs-client is checked, not a constant beside it.
        sent = []

        class Processor:
            def issue_command(self, command, args=None, comment=None):
                sent.append((command, dict(args)))
                return types.SimpleNamespace(id="c1", generation_time=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc))

        class Client:
            def __init__(self, address):
                pass

            def get_processor(self, instance, processor):
                return Processor()

            def close(self):
                pass

        fake = types.ModuleType("yamcs.client")
        fake.YamcsClient = Client
        settings = config.Settings(home=Path("/x/sds"), doomsat_home=Path("/x"))
        p = {"episode_id": "20261004T160006Z-e0001", "source": "/x/run/rec/1-11049.json",
             "dest": "rec_20261004T160006Z-e0001.json"}
        with mock.patch.dict(sys.modules, {"yamcs": types.ModuleType("yamcs"), "yamcs.client": fake}):
            out = record.request(settings, p)
        self.assertEqual(len(sent), 1)
        command, args = sent[0]
        self.assertEqual(command, config.SENDFILE)
        self.assertEqual(args["keep"], "KEEP")
        self.assertEqual(args["cfdpClass"], "CLASS_2")
        self.assertEqual(sorted(args), sorted(["channelId", "destId", "cfdpClass", "keep", "priority",
                                               "sourceFileName", "destFileName"]))
        self.assertEqual((args["sourceFileName"], args["destFileName"]), (p["source"], p["dest"]))
        self.assertEqual(out["args"], args)

    def test_cfdp_is_read_only_but_for_the_ingested_object(self):
        bad = ["%s: %s" % (path, f) for path, text in sorted(sds_sources().items())
               for f in file_transfer_findings(path, text)]
        self.assertEqual(bad, [])
        users = [path for path, text in sds_sources().items() if "/filetransfer" in text]
        self.assertEqual([os.path.basename(u) for u in users], ["record.py"])

    def test_canary_planted_transfer_writes_are_caught(self):
        planted = [
            ("record.py", 'requests.post("%s/api/filetransfer/%s/cfdp/transfers" % (url, inst), json=req)'),
            ("record_x.py", 'url = base + "/api/filetransfer/fprime-project/cfdp/transfers/3:cancel"\nhttp.post(url)'),
            ("pipeline.py", 'self.http.request("PUT", base + "/filetransfer/fprime-project/cfdp/transfers/3")'),
            ("capture.py", 'svc = client.get_file_transfer_client(inst).get_service("cfdp")\nsvc.upload("b", "o")'),
            ("quicklook.py", 'svc.download("cfdpDown", "rec_x.json")'),
            ("store.py", "svc.cancel_transfer(t.id)"),
            ("catalog.py", "svc.pause_transfer(t.id)"),
            ("context.py", "svc.resume_transfer(t.id)"),
            ("frames.py", "svc.run_file_action(t.id, 'x')"),
            ("publish.py", 'requests.delete("%s/api/storage/buckets/cfdpDown/objects/%s" % (url, n), timeout=10)'),
            ("record_y.py", 'requests.post(base + "/api/storage/buckets/cfdpDown/objects/x", data=b"")'),
            ("lineage.py", "ref = record.forget_downlinked(settings, t)"),
            # the URL from a helper that builds it, record's own or one of the module's
            ("pipeline.py", "http.delete(record.object_url(settings, t['objectName']))"),
            ("pipeline.py", "http.post(object_url(settings, name), data=data)"),
            ("pipeline.py", "from .record import object_url as where\nhttp.put(where(s, n), data=b'')"),
            ("capture.py", "from doomsat_sds import record as r\nhttp.delete(r.object_url(s, n))"),
            ("lineage.py", 'http.request("DELETE", record.object_url(s, n))'),
            ("record.py", "def ingest(settings, name, data, http=None):\n"
                          "    _http(http).put(object_url(settings, name), data=data)"),
            ("record.py", 'BUCKET = "cfdpDown"\n'
                          "def where(s, n):\n"
                          "    return '%s/api/storage/buckets/%s/objects/%s' % (s.yamcs_url, BUCKET, n)\n"
                          "def keep(s, n, http):\n    http.put(where(s, n), data=b'')"),
            ("record.py", "def _turl(s):\n"
                          "    return '%s/api/filetransfer/%s/cfdp/transfers' % (s.yamcs_url, s.instance)\n"
                          "def stop(s, http):\n    http.post(_turl(s) + '/3:cancel')"),
            # yamcs-client's storage writes
            ("pipeline.py", "client.get_storage_client().get_bucket('cfdpDown').delete_object(name)"),
            ("publish.py", "client.get_storage_client().upload_object('cfdpDown', name, data)"),
            ("store.py", "client.get_storage_client().remove_bucket('cfdpDown')"),
            ("catalog.py", "client.get_storage_client().create_bucket(record.BUCKET)"),
            ("pipeline.py", "b = client.get_storage_client().get_bucket(record.BUCKET)\nb.delete_object(n)"),
            # the one DELETE allowed is forget_downlinked's, in record.py, and nothing else there
            ("record.py", "def ingest(s, t, http):\n    http.delete(object_url(s, t['objectName']))"),
            ("record.py", "def forget_downlinked(s, t, http):\n    http.post(object_url(s, t['objectName']))"),
            ("sds_record.py", "requests.delete(record.object_url(s, n))"),
            ("record_x.py", "def forget_downlinked(s, t, http):\n    http.delete(object_url(s, t['objectName']))"),
        ]
        for name, text in planted:
            self.assertTrue(file_transfer_findings(name, text), "not caught in %s: %s" % (name, text))
        ok = [
            ("record.py", 'r = http.get("%s/api/filetransfer/%s/cfdp/transfers" % (u, i), params={})\n'
                          'requests.get(base + "/api/storage/buckets/cfdpDown/objects/x")\n'
                          "def forget_downlinked(settings, transfer, http=None):\n"
                          "    r = _http(http).delete(object_url(settings, transfer['objectName']), timeout=10)\n"
                          "    r.raise_for_status()"),
            ("publish.py", "class P:\n    def upload(self, name, data):\n        pass\nyamcs.upload('x', b'')"),
            # publish.py's own BUCKET and object_url are the doomsat-sds bucket's
            ("publish.py", 'BUCKET = "doomsat-sds"\n'
                           "def object_url(s, n):\n"
                           "    return '%s/api/storage/buckets/%s/objects/%s' % (s.yamcs_url, BUCKET, n)\n"
                           "http.put(object_url(s, n), data=b'')\n"
                           "http.post('%s/storage/buckets' % base, json={'name': BUCKET})"),
            ("sds_record.py", "ref = record.ingest(settings, c, p, cmd, t)\n"
                              "ref['deleted'] = record.forget_downlinked(settings, t)"),
            # what a GET returns is the record, not the bucket's address
            ("pipeline.py", "r = http.get(record.object_url(s, n))\nhttp.post(other_url, data=r.content)"),
        ]
        for name, text in ok:
            self.assertEqual(file_transfer_findings(name, text), [], "%s: %s" % (name, text))
        self.assertLessEqual({"BUCKET", "object_url"}, RECORD_EXPORTS["cfdpDown"])     # read from record.py

    @unittest.skipUnless(os.path.exists(os.path.join(REPO, "ground", "sds", "dags", "sds_record.py")),
                         "Phase D (the record DAG) is not in this tree")
    def test_record_requests_are_off_unless_switched_on(self):
        dag = read("ground/sds/dags/sds_record.py")
        self.assertRegex(dag, r'environ\.get\(\s*"DOOMSAT_SDS_RECORDS"\s*,\s*"off"\s*\)\s*==\s*"on"')
        self.assertIn('DOOMSAT_SDS_RECORDS="${DOOMSAT_SDS_RECORDS:-off}"', read("scripts/sds.sh"))
        senders = [f for f in ast.walk(ast.parse(dag)) if isinstance(f, ast.FunctionDef)
                   and any(isinstance(c.func, ast.Attribute) and c.func.attr == "request" for c in own_calls(f))]
        self.assertEqual(len(senders), 1, "exactly one task sends the command")
        body = [s for s in senders[0].body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        first = body[0]
        self.assertIsInstance(first, ast.If, "the sending task must check ENABLED before anything else")
        self.assertEqual(ast.unparse(first.test), "not ENABLED")
        self.assertIsInstance(first.body[0], ast.Raise)

    def test_canary_planted_commands_are_caught(self):
        commands = re.findall(r"\bcommand\s+([A-Z_]+)\s*[(\s]", read("flight/Components/Doom/Doom.fpp"))
        self.assertIn("RESET_GAME", commands)
        for name in commands:          # the doom component's commands share the parameter namespace
            self.assertTrue(command_findings("pipeline.py", 'C = "%s%s"' % (config.NAMESPACE, name)), name)
        planted = {
            "pipeline.py": "proc.issue_command(config.SENDFILE, args={})",
            "record.py": 'proc.issue_command("/DoomSat_DoomSat/DoomSat/doom/RESET_GAME")\nSENDFILE',
            "record_more.py": "proc.issue_command(config.qualified('INTENT'), args=a)\nconfig.SENDFILE",
            "products.py": "conn = proc.create_command_connection()",
            "quicklook.py": "proc.set_parameter_value('/DoomGround/goal', 3)",
            "publish.py": 'self.http.post("%s/processors/%s/realtime/commands/x" % (base, inst))',
            "capture.py": "from doomsat_sds.record import plan, request",
            "pipeline2.py": "out = record.request(settings, p)",
        }
        for name, text in planted.items():
            self.assertTrue(command_findings(name, text), "not caught in %s: %s" % (name, text))
        ok = "proc.issue_command(config.SENDFILE, args={'sourceFileName': s})\n"
        self.assertEqual(command_findings("record.py", ok), [])
        self.assertEqual(command_findings("archive.py", 'NAMESPACE + "PAYLOAD_LINK"\n"%sFRAME_CHUNK"' %
                                          config.NAMESPACE), [])


# ================================================================================================ 5. process lines
class SdsProcessesSurviveTheFlightSideKillers(unittest.TestCase):
    def test_the_patterns_and_command_lines_were_found(self):
        orphans = orphan_patterns()
        for p in ("pilot.py", "runner.py", "doom_payload.py", "vizdoom"):
            self.assertIn(p, orphans)
        pkill, status = flight_patterns()
        self.assertGreaterEqual(len(pkill), 5)
        self.assertGreaterEqual(len(status), 4)
        lines = sds_command_lines(HOMES[0])
        names = {n for n, _ in lines}
        self.assertEqual(names, set(sds_env(HOMES[0])["COMPONENTS"].split()) | {"capture"})
        self.assertTrue(any("-m doomsat_sds.capture" in line for _, line in lines))
        self.assertTrue(any(line.endswith("/sds/venv/bin/airflow scheduler") for _, line in lines))

    def test_no_sds_command_line_is_killed_by_preflight_or_the_flight_stop(self):
        orphans = orphan_patterns()
        pkill, status = flight_patterns()
        for home in HOMES:
            for name, line in sds_command_lines(home):
                self.assertEqual(killer_hits(line, orphans, pkill + status), [], "%s: %s" % (name, line))

    def test_no_airflow_process_title_is_killed_either(self):
        orphans = orphan_patterns()
        pkill, status = flight_patterns()
        for title in AIRFLOW_TITLES:
            self.assertEqual(killer_hits(title, orphans, pkill + status), [], title)

    def test_canary_the_matcher_sees_a_flight_process(self):
        orphans = orphan_patterns()
        pkill, _ = flight_patterns()
        for line in ("/root/doom/payload-venv/bin/python /x/payload/doom_payload.py --fps 10",
                     "/x/ground/.venv/bin/python ground/pilot.py --duration 120",
                     "/root/doom/DoomSat/build-artifacts/Linux/DoomSat/bin/DoomSat -p 50000",
                     "/root/doom/payload-venv/lib/python3.11/site-packages/vizdoom/vizdoom -iwad x"):
            self.assertTrue(killer_hits(line, orphans, pkill), line)

    def test_the_sds_stop_matches_no_process_by_name(self):
        """sds.sh stop signals only the sessions it started (a pid file checked against the start time recorded
        beside it), never a pattern, so it cannot hit a flight process, a test run from the SDS venv, or a pid the
        kernel handed to something else after a reboot. (It once ended with pkill -KILL -f "$VENV/bin/".)"""
        sds_text = read("scripts/sds.sh")
        self.assertEqual(pkill_patterns(sds_text), [])
        self.assertNotRegex(sds_text, r"\bp(kill|grep)\b[^\n]*\s-f\b")
        stop_one = re.search(r"stop_one\(\) \{.*?\n\}", sds_text, re.S).group(0)
        self.assertIn('pid="$(sds_pid "$1")"', stop_one)
        sds_pid = re.search(r"sds_pid\(\) \{.*?\n\}", sds_text, re.S).group(0)
        self.assertIn("lstart", sds_pid)



# ================================================================================================ 6. DAG imports
class SdsImportsNothingFromThePilot(unittest.TestCase):
    def test_no_dag_imports_a_pilot_side_module(self):
        dags = sorted(glob.glob(os.path.join(SDS, "dags", "*.py")))
        self.assertTrue(dags)
        bad = {rel(p): pilot_imports(Path(p).read_text(encoding="utf-8")) for p in dags}
        self.assertEqual({k: v for k, v in bad.items() if v}, {})

    def test_no_package_module_imports_a_pilot_side_module(self):
        bad = {path: pilot_imports(text) for path, text in sds_sources().items() if "/doomsat_sds/" in path}
        self.assertTrue(bad)
        self.assertEqual({k: v for k, v in bad.items() if v}, {})

    def test_canary_planted_pilot_imports_are_caught(self):
        for line in ("from pilot import Pilot", "import decision_graph as dg", "def t():\n    import targeting\n",
                     "from providers import Jev", "import after_action, graph_config", "from ground import pilot",
                     "import world_model", "mod = importlib.import_module('pilot')"):
            self.assertTrue(pilot_imports(line), "not caught: %r" % line)
        self.assertEqual(pilot_imports("from airflow.providers.standard.operators.trigger_dagrun import X\n"
                                       "from doomsat_sds import pipeline, products\nfrom . import config"), [])


if __name__ == "__main__":
    unittest.main()
