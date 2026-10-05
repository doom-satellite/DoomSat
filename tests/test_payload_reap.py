"""Stopping the payload leaves no ViZDoom engine behind.

The engine ignores SIGTERM (research/reap.py), so a payload that hangs inside it, or dies without closing it,
used to leave it running: one more orphan per restart, competing for the cores of whatever ran next. The run
script now reaps what is left of the payload's process group, and still kills by name a payload that leads no
group. Its functions run here against a stand-in payload and engine under per-test names, so no real payload or
engine can ever match; the launcher's own stop() and payload arms are only read, never run.
"""
import ast
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


RUN_SCRIPT = _read("scripts", "wsl_run_flight.sh")
PAYLOAD_PATTERN = "doom_payloa[d].py --fps"
ENGINE_PATTERN = "/vizdoom/vizdoo[m]"


def _function(text, name):
    """A shell function's definition, from `name() {` to its closing brace."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(name + "() {"):
            if line.rstrip().endswith("}"):
                return line + "\n"
            end = lines.index("}", i)
            return "\n".join(lines[i:end + 1]) + "\n"
    raise AssertionError(f"{name}() is not in scripts/wsl_run_flight.sh")


def _arm(text, name):
    """The body of a top-level `case` arm, up to its `;;`."""
    m = re.search(r"^  %s\)\n(.*?);;" % re.escape(name), text, re.M | re.S)
    if m is None:
        raise AssertionError(f"no '{name})' arm in scripts/wsl_run_flight.sh")
    return m.group(1)


def _gone(pid):
    """Not running: no such process, or a zombie nobody has reaped yet."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


# Spawns the engine (a child stays in its parent's process group), writes both pids, then waits. In 'hung'
# mode it ignores SIGTERM, as a payload stuck inside ViZDoom does (the engine holds the GIL, so the handler
# never runs); in 'orphaning' mode it dies on it and leaves the engine behind. What it needs comes in the
# environment, so its command line holds the payload's name and nothing of the engine's.
PAYLOAD = """import os, signal, subprocess, sys, time
engine = subprocess.Popen([sys.executable, os.environ["STANDIN_ENGINE"]])
if os.environ["STANDIN_MODE"] == "hung":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pids = os.environ["STANDIN_PIDS"]
with open(pids + ".tmp", "w") as f:
    f.write("%d %d" % (os.getpid(), engine.pid))
os.rename(pids + ".tmp", pids)
while True:
    time.sleep(1)
"""
ENGINE = """import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""


@unittest.skipUnless(os.name != "nt" and shutil.which("bash") and shutil.which("setsid") and shutil.which("pkill")
                     and shutil.which("pgrep") and shutil.which("ps") and os.path.isdir("/proc"),
                     "needs bash, setsid, pkill, pgrep, ps and /proc")
class TestTheStopReapsTheEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        nonce = uuid.uuid4().hex[:12]
        self.groups = []
        # Matches this test's stand-ins (the nonce is in both paths) and nothing else; tearDown's last resort
        self.nonce_pattern = f"{nonce[:-1]}[{nonce[-1]}]"
        self.payload = os.path.join(self.tmp.name, f"payload_{nonce}.py")
        engine_dir = os.path.join(self.tmp.name, f"eng_{nonce}")
        os.makedirs(engine_dir)
        self.engine = os.path.join(engine_dir, f"engine_{nonce}")
        for path, text in ((self.payload, PAYLOAD), (self.engine, ENGINE)):
            with open(path, "w") as f:
                f.write(text)
        # Bracketed like the originals, so the script running them does not match itself
        payload_pattern = f"payload_{nonce[:-1]}[{nonce[-1]}].py --fps"
        engine_pattern = f"/eng_{nonce}/engine_{nonce[:-1]}[{nonce[-1]}]"
        lifted = "".join(_function(RUN_SCRIPT, name)
                         for name in ("kill_hung_payload", "payload_groups", "reap_payload"))
        self.assertIn(PAYLOAD_PATTERN, lifted)
        self.assertIn(ENGINE_PATTERN, lifted)
        lifted = lifted.replace(PAYLOAD_PATTERN, payload_pattern).replace(ENGINE_PATTERN, engine_pattern)
        self.assertNotIn("doom_payload", lifted.replace("[", "").replace("]", ""))
        self.assertNotIn("/vizdoom/", lifted)
        # The sequence of the launcher's stop() and payload arms (pinned by test_both_stops_use_it below)
        self.stop_script = os.path.join(self.tmp.name, "stop.sh")
        with open(self.stop_script, "w") as f:
            f.write(lifted + f'groups=$(payload_groups)\npkill -f "{payload_pattern}" 2>/dev/null\n'
                             'reap_payload "$groups"\n')

    def tearDown(self):
        for group in self.groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        # A stand-in that never wrote its pids left no group to kill
        subprocess.run(["pkill", "-KILL", "-f", self.nonce_pattern], stdin=subprocess.DEVNULL, timeout=10)

    def start(self, mode, leader=True):
        """Start the stand-in as start_payload does: setsid -f, exec, the payload leading its own group. Without
        `leader`, a shell whose command line names no payload leads the group and starts it, as under nohup."""
        pids = os.path.join(self.tmp.name, "pids")
        log = os.path.join(self.tmp.name, "payload.log")
        env = dict(os.environ, STANDIN_ENGINE=self.engine, STANDIN_PIDS=pids, STANDIN_MODE=mode,
                   STANDIN_PYTHON=sys.executable, STANDIN_PAYLOAD=self.payload, STANDIN_LOG=log)
        if leader:
            command = f"exec '{sys.executable}' '{self.payload}' --fps 10 > '{log}' 2>&1"
        else:
            command = '"$STANDIN_PYTHON" "$STANDIN_PAYLOAD" --fps 10 > "$STANDIN_LOG" 2>&1 & wait'
        # The payload writes to its log; the shell's own "Killed" job notice would land in the test output
        subprocess.run(["setsid", "-f", "bash", "-c", command], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=env, check=True, timeout=10)
        deadline = time.time() + 15
        while not os.path.exists(pids) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(pids), "the stand-in payload never started")
        with open(pids) as f:
            payload, engine = map(int, f.read().split())
        group = os.getpgid(payload)
        self.groups.append(group)
        if leader:
            self.assertEqual(group, payload, "the payload leads its own process group")
        else:
            self.assertNotEqual(group, payload, "the payload leads no group")
        self.assertEqual(os.getpgid(engine), group, "the engine is in the payload's group")
        return payload, engine

    def stop(self, *pids):
        subprocess.run(["bash", self.stop_script], stdin=subprocess.DEVNULL, timeout=30)
        deadline = time.time() + 3
        while not all(_gone(p) for p in pids) and time.time() < deadline:
            time.sleep(0.05)

    def test_a_hung_payload_and_its_engine_are_both_killed(self):
        payload, engine = self.start("hung")
        self.stop(payload, engine)
        self.assertTrue(_gone(payload), "the hung payload is still running")
        self.assertTrue(_gone(engine), "the hung payload's engine was left behind")

    def test_the_engine_of_a_payload_that_died_on_sigterm_is_killed(self):
        payload, engine = self.start("orphaning")
        self.stop(payload, engine)
        self.assertTrue(_gone(payload))
        self.assertTrue(_gone(engine), "the engine outlived its payload")

    def test_a_hung_payload_that_leads_no_group_is_still_killed_by_name(self):
        # The macOS case: there is no group to reap, so the engine stays (for research/reap.py, as before), but
        # the payload must not keep holding its port. Only groups a payload leads are touched.
        payload, engine = self.start("hung", leader=False)
        self.stop(payload)
        self.assertTrue(_gone(payload), "a hung payload outside a group of its own is still running")
        self.assertFalse(_gone(engine), "an engine in a group no payload leads was killed")


class TestTheLauncherSource(unittest.TestCase):
    def test_both_stops_use_it(self):
        # stop() and the payload restart arm take the groups before the first SIGTERM (a payload that dies on it
        # takes its pid with it) and reap them after
        for where, body in (("stop()", _function(RUN_SCRIPT, "stop")), ("payload)", _arm(RUN_SCRIPT, "payload"))):
            with self.subTest(where):
                taken = body.find("=$(payload_groups)")
                sigterm = body.find(f'pkill -f "{PAYLOAD_PATTERN}"')
                reaped = body.find('reap_payload "$groups"')
                self.assertGreaterEqual(taken, 0)
                self.assertLess(taken, sigterm)
                self.assertLess(sigterm, reaped)
                self.assertNotIn("kill_hung_payload", body, "reap_payload calls it, after the groups")

    def test_the_reap_waits_then_kills_the_groups_then_by_name(self):
        lines = _function(RUN_SCRIPT, "reap_payload").splitlines()[1:-1]
        waits = [i for i, line in enumerate(lines)
                 if f'pgrep -f "{PAYLOAD_PATTERN}"' in line and "sleep" in line and "break" in line]
        kills = [i for i, line in enumerate(lines) if "pkill -KILL -g" in line]
        self.assertTrue(waits and kills, lines)
        self.assertLess(waits[0], kills[0], "the payload gets its time to close the game before the SIGKILL")
        self.assertEqual(lines[-1].strip(), "kill_hung_payload", "the only kill for a payload that leads no group")

    def test_the_payload_leads_its_group(self):
        start = _function(RUN_SCRIPT, "start_payload")
        self.assertRegex(start, r'detach "exec \S+ \S+/payload/doom_payload\.py --fps')
        self.assertIn("setsid -f", _function(RUN_SCRIPT, "detach"))


class TestThePayloadsSigtermHandler(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(_read("payload", "doom_payload.py"))
        self.main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        self.stop = next(n for n in self.main.body if isinstance(n, ast.FunctionDef) and n.name == "_stop")

    def test_it_is_armed_before_the_game_is_built(self):
        lines = [ast.unparse(n) for n in self.main.body]
        armed = lines.index("signal.signal(signal.SIGTERM, _stop)")
        built = lines.index("payload = Payload(args)")
        self.assertLess(armed, built, "a SIGTERM while the engine starts would kill the payload and leave it")
        self.assertLess(lines.index("payload = None"), armed)

    def test_it_always_exits(self):
        tries = [n for n in self.stop.body if isinstance(n, ast.Try)]
        self.assertEqual(len(tries), 1)
        body = "\n".join(ast.unparse(n) for n in tries[0].body)
        final = "\n".join(ast.unparse(n) for n in tries[0].finalbody)
        self.assertIn("payload.game.close()", body)
        self.assertIn("os.killpg(payload.wad_job[0].pid, signal.SIGKILL)", body)
        self.assertIn("sys.exit(0)", final, "a game that fails to close must not keep the payload alive")


if __name__ == "__main__":
    unittest.main()
