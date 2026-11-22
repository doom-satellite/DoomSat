"""The dashboard's LOAD_WAD form on a lossy link: it waits for the spacecraft's answer and sends again if none comes.

The page's own script runs under node, taken from ground/dashboard/index.html as it is: `loadAnswer` (what counts
as an answer) on its own, and the whole Load handler against a fake Yamcs, with a clock that moves only when the
page waits. The command's arguments and the payload's proof time are checked as text against the flight software
and the payload. No network, no browser, no game: without node the node tests are skipped.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


PAGE = read("ground/dashboard/index.html")


def js_function(name):
    """The page's `function name(...) {...}`, ending at the first line that is just `}`."""
    m = re.search(rf"^function {name}\(.*?^\}}$", PAGE, re.S | re.M)
    if not m:
        raise AssertionError(f"no function {name} in the dashboard")
    return m.group(0)


class TestTheLoadForm(unittest.TestCase):
    def test_it_sends_the_commands_real_arguments(self):
        fpp = read("flight/Components/Doom/Doom.fpp")
        block = re.search(r"async command LOAD_WAD\((.*?)\)\s*opcode", fpp, re.S).group(1)
        wanted = {m.lstrip("$") for m in re.findall(r"^\s*(\$?\w+)\s*:", block, re.M)}
        sent = re.search(r"const args = \{(.*?)\};", PAGE[PAGE.index('$("wadform").addEventListener'):]).group(1)
        self.assertEqual(set(re.findall(r"\b(\w+):", sent)), wanted)

    def test_its_wait_outlasts_the_payloads_proof(self):
        tries = int(re.search(r"const LOAD_TRIES = (\d+)", PAGE).group(1))
        wait_ms = int(re.search(r"LOAD_ANSWER_MS = (\d+)", PAGE).group(1))
        proof_s = float(re.search(r"^WAD_PROBE_TIMEOUT_S = ([\d.]+)", read("payload/doom_payload.py"), re.M).group(1))
        self.assertGreaterEqual(tries, 2)
        self.assertGreater(wait_ms, proof_s * 1000 + 5000, "a slow refusal must not be taken for no answer")


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class TestWhatCountsAsAnAnswer(unittest.TestCase):
    """`loadAnswer(before, events, tlm, args)` from the page, run under node."""

    ARGS = {"iwad": "freedoom2.wad", "pwad": "basic.wad", "map": "MAP01"}

    def answer(self, seen, loads0, events, tlm, args=None):
        script = js_function("loadAnswer") + f"""
const before = {{seen: new Set({json.dumps(seen)}), loads: {json.dumps(loads0)}}};
const args = {json.dumps(args or self.ARGS)};
console.log(JSON.stringify(loadAnswer(before, {json.dumps(events)}, {json.dumps(tlm)}, args)));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def ev(self, key, message):
        return {"key": key, "message": message}

    def test_already_flying_is_an_answer(self):
        # A repeat of a load whose answer was lost, or a load of what flies: the payload answers WadAlreadyFlying
        msg = "[WadAlreadyFlying] Already flying basic.wad over freedoom2.wad on MAP01: LOAD_WAD changed nothing"
        self.assertEqual(self.answer([], 0, [self.ev("b", msg)], None), msg)
        self.assertIsNone(self.answer(["b"], 0, [self.ev("b", msg)], None), "seen before it was sent: old news")

    def test_it_asks_the_archive_for_every_answer(self):
        fetcher = PAGE[PAGE.index("async function wadEvents()"):PAGE.index("function loadAnswer(")]
        for q in ("WadLoad", "WadAlreadyFlying"):
            self.assertIn(f'"{q}"', fetcher)
        fpp = read("flight/Components/Doom/Doom.fpp")
        for event in ("WadLoaded", "WadLoadFailed"):
            self.assertRegex(fpp, rf"event {event}\(")

    def test_nothing_new_is_no_answer(self):
        old = self.ev("a", "[WadLoaded] Now flying x.wad on E1M1")
        self.assertIsNone(self.answer(["a"], 0, [old], {"iwad": "freedoom1.wad", "pwad": "", "loads": 0}))

    def test_a_new_refusal_or_load_is_the_answer(self):
        for msg in ("[WadLoadFailed] Could not load basic.wad over freedoom2.wad: no",
                    "[WadLoaded] Now flying basic.wad over freedoom2.wad on MAP01"):
            with self.subTest(msg=msg):
                got = self.answer(["a"], 0, [self.ev("b", msg), self.ev("a", "[WadLoaded] old")], None)
                self.assertEqual(got, msg)

    def test_the_earliest_new_event_wins(self):
        events = [self.ev("c", "[WadLoaded] Now flying basic.wad over freedoom2.wad on MAP01"),  # newest first, as
                  self.ev("b", "[WadLoadFailed] Could not load basic.wad over freedoom2.wad: no")]  # wadEvents sorts
        self.assertEqual(self.answer([], 0, events, None), events[1]["message"])

    def test_other_wad_events_are_not_answers(self):
        up = self.ev("b", "[WadUplinked] Uplinked WAD ready to load: /x/basic.wad")
        self.assertIsNone(self.answer([], 0, [up], None))

    def test_another_loads_answer_is_not_this_ones(self):
        # Another tab, or tools/wad_uplink_demo.py, loading while this form waits: each event names its load
        for msg in ("[WadLoadFailed] Could not load other.wad: another LOAD_WAD is still being checked",
                    "[WadLoaded] Now flying other.wad over freedoom2.wad on MAP01",
                    "[WadAlreadyFlying] Already flying freedoom2.wad on MAP01: LOAD_WAD changed nothing"):
            with self.subTest(msg=msg):
                self.assertIsNone(self.answer([], 0, [self.ev("b", msg)], None))
        # A bare IWAD's name is inside "PWAD over IWAD": the whole name must match, not a part of it
        bare = {"iwad": "freedoom2.wad", "pwad": "", "map": "MAP01"}
        over = self.ev("b", "[WadLoaded] Now flying basic.wad over freedoom2.wad on MAP01")
        self.assertIsNone(self.answer([], 0, [over], None, bare))
        mine = self.ev("c", "[WadLoaded] Now flying freedoom2.wad on map01")
        self.assertEqual(self.answer([], 0, [mine, over], None, bare), mine["message"])

    def test_a_name_outside_ascii_is_answered_as_the_payload_spells_it(self):
        # payload/wad_uplink.py reads each text as ASCII and writes it back with "?" for every byte it could not read
        args = {"iwad": "fr\u00e9e.wad", "pwad": "", "map": "MAP01"}   # UTF-8: two bytes
        msg = "[WadLoadFailed] Could not load fr??e.wad: IWAD name may use only letters, digits and _ . + -"
        self.assertEqual(self.answer([], 0, [self.ev("b", msg)], None, args), msg)

    def test_the_channels_stand_in_only_for_this_load(self):
        this = {"iwad": "freedoom2.wad", "pwad": "basic.wad", "loads": 1}
        self.assertIn("event not seen", self.answer([], 0, [], this))
        self.assertIsNone(self.answer([], 0, [], dict(this, iwad="other.wad")), "another load's count")
        self.assertIsNone(self.answer([], 1, [], this), "the count has not moved")
        self.assertIsNone(self.answer([], None, [], this), "no count from before: the channels prove nothing")


# The page's script under node. Stubs: the elements the script touches, and a Yamcs behind fetch that takes every
# command, adds the events a scenario says the spacecraft sends for each one, and answers archive queries the way
# Yamcs does (newest first, and {} for no events). setTimeout moves the clock by its delay, so 75 s pass at once.
HARNESS = r"""
const {script, sc} = JSON.parse(require("fs").readFileSync(0, "utf8"));
let now = Date.UTC(2026, 9, 5, 12, 0, 0), seq = 100;
const realSetTimeout = setTimeout;
Date.now = () => now;
global.setTimeout = (fn, ms) => { now += ms || 0; return realSetTimeout(fn, 0); };
global.setInterval = () => 0;
global.performance = {now: () => now};
const els = {};
const el = id => els[id] || (els[id] = {id, value: "", textContent: "", innerHTML: "", style: {}, disabled: false,
  classList: {add() {}, remove() {}, toggle() {}}, firstChild: {textContent: ""},
  addEventListener(type, f) { this["on" + type] = f; }, querySelector(s) { return el(`${id} ${s}`); }});
global.document = {getElementById: el, addEventListener() {}, activeElement: null, hidden: false, body: {style: {}}};
global.window = global; global.addEventListener = () => {}; global.innerWidth = 1920; global.innerHeight = 1080;
global.location = {search: ""}; global.confirm = () => true;
console.log = () => {};
const archive = sc.archive.map(m => ({generationTime: "2026-10-04T09:00:00.000Z", seqNumber: seq++, message: m}));
const out = {posts: [], reads: 0, busyAtPost: []};
const reply = (status, body) =>
  ({ok: status < 300, status, json: async () => JSON.parse(body), text: async () => body});
global.fetch = async (url, opts) => {
  if (url.includes("/commands/")) {
    out.posts.push(JSON.parse(opts.body).args);
    out.busyAtPost.push(el("wadform button").disabled);
    if (sc.refuse) return reply(400, '{"code":400,"msg":"refused"}');
    (sc.answers[out.posts.length] || []).forEach((m, i) =>
      archive.push({generationTime: new Date(now + 100 * (i + 1)).toISOString(), seqNumber: seq++, message: m}));
    return reply(200, "{}");
  }
  if (url.includes("/archive/fprime-project/events")) {
    if (++out.reads <= sc.failReads)
      return sc.failAs === "json" ? reply(503, '{"code":503,"msg":"no archive"}')   // Yamcs's own error
                                  : reply(502, "<html>Bad Gateway</html>");          // the proxy's page
    const q = decodeURIComponent(url.match(/[?&]q=([^&]*)/)[1]);
    const hits = archive.filter(e => e.message.includes(q))
      .sort((a, b) => (a.generationTime < b.generationTime ? 1 : -1)).slice(0, 20);
    return reply(200, JSON.stringify(hits.length ? {events: hits} : {}));
  }
  if (url.includes("parameters:batchGet"))   // WAD_LOADS never moves: only events answer here
    return reply(200, JSON.stringify({value: [{id: {name: "/x/WAD_LOADS"}, engValue: {uint32Value: 4}}]}));
  return reply(200, "{}");
};
eval(script);
Object.entries(sc.args).forEach(([k, v]) => { el({iwad: "wadIwad", pwad: "wadPwad", map: "wadMap"}[k]).value = v; });
(async () => {
  const t0 = now;
  await el("wadform").onsubmit({preventDefault() {}});
  out.seconds = (now - t0) / 1000;
  out.text = el("wadsent").textContent;
  out.busyAfter = el("wadform button").disabled;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class TestTheLoadHandler(unittest.TestCase):
    """The form's whole submit handler, run under node against a fake Yamcs."""

    ARGS = {"iwad": "freedoom2.wad", "pwad": "basic.wad", "map": "MAP01"}
    LOADED = "[WadLoaded] Now flying basic.wad over freedoom2.wad on MAP01"
    # In the archive before every send: this very load's answer from an earlier flight (the archive persists)
    OLD = [LOADED, "[WadLoadFailed] Could not load basic.wad over freedoom2.wad: no"]
    TRIES = int(re.search(r"const LOAD_TRIES = (\d+)", PAGE).group(1))
    WAIT_S = int(re.search(r"LOAD_ANSWER_MS = (\d+)", PAGE).group(1)) / 1000

    def run_form(self, answers=None, archive=None, fail_reads=0, fail_as="html", refuse=False):
        script = PAGE[PAGE.index("<script>") + len("<script>"):PAGE.index("</script>")]
        polling = "pollLoop(); setInterval(pollArchive, 1000); pollArchive();"
        self.assertIn(polling, script)
        sc = {"args": self.ARGS, "answers": {str(k): v for k, v in (answers or {}).items()},
              "archive": self.OLD if archive is None else archive,
              "failReads": fail_reads, "failAs": fail_as, "refuse": refuse}
        given = json.dumps({"script": script.replace(polling, ""), "sc": sc})
        out = subprocess.run(["node", "-e", HARNESS], input=given, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertTrue(all(got["busyAtPost"]), "the button is off while a load is under way")
        self.assertFalse(got["busyAfter"], "the button comes back whatever the outcome")
        self.assertTrue(all(p == self.ARGS for p in got["posts"]), got["posts"])
        return got

    def test_silence_is_sent_three_times_then_said(self):
        got = self.run_form()
        self.assertEqual(len(got["posts"]), 3)
        self.assertGreaterEqual(got["seconds"], self.TRIES * self.WAIT_S)
        self.assertIn(f"no answer after {self.TRIES} tries", got["text"])

    def test_an_answer_ends_it(self):
        got = self.run_form(answers={1: [self.LOADED]}, archive=[])   # and an empty archive answers {}
        self.assertEqual(len(got["posts"]), 1)
        self.assertTrue(got["text"].endswith(f"(try 1): {self.LOADED}"), got["text"])

    def test_a_lost_command_is_sent_again(self):
        again = "[WadAlreadyFlying] Already flying basic.wad over freedoom2.wad on MAP01: LOAD_WAD changed nothing"
        got = self.run_form(answers={2: [again]})
        self.assertEqual(len(got["posts"]), 2)
        self.assertTrue(got["text"].endswith(f"(try 2): {again}"), got["text"])

    def test_an_answer_from_before_the_send_is_not_this_ones(self):
        # self.OLD names this very load; it was in the archive before the send, so it answers nothing
        got = self.run_form(archive=self.OLD)
        self.assertEqual(len(got["posts"]), self.TRIES)
        self.assertNotIn("Could not load", got["text"])

    def test_another_loads_answer_is_not_this_ones(self):
        other = "[WadLoadFailed] Could not load other.wad: another LOAD_WAD is still being checked"
        got = self.run_form(answers={1: [other], 2: [self.LOADED]})
        self.assertEqual(len(got["posts"]), 2)
        self.assertTrue(got["text"].endswith(f"(try 2): {self.LOADED}"), got["text"])

    def test_the_earliest_answer_wins_across_both_queries(self):
        # One poll finds two new answers to this load, one from each archive query: the first one said is the answer
        refused = "[WadLoadFailed] Could not load basic.wad over freedoom2.wad: the game had not loaded it"
        later = "[WadAlreadyFlying] Already flying basic.wad over freedoom2.wad on MAP01: LOAD_WAD changed nothing"
        got = self.run_form(answers={1: [refused, later]})
        self.assertTrue(got["text"].endswith(f"(try 1): {refused}"), got["text"])

    def test_no_send_without_the_events_from_before(self):
        for fail_as, status in (("html", "502"), ("json", "503")):
            with self.subTest(fail_as=fail_as):
                got = self.run_form(answers={1: [self.LOADED]}, fail_reads=1000, fail_as=fail_as)
                self.assertEqual(got["posts"], [])
                self.assertEqual(got["reads"], 6, "3 reads, each asking both queries")
                self.assertGreaterEqual(got["seconds"], 2, "about 1 s between reads")
                self.assertIn("MAP01 not sent: the event archive did not answer", got["text"])
                self.assertIn(f"HTTP {status}", got["text"])

    def test_the_events_from_before_are_read_again_if_the_archive_misses_once(self):
        got = self.run_form(fail_reads=1)   # still not fooled by the old answers it then reads
        self.assertEqual(len(got["posts"]), self.TRIES)
        self.assertGreaterEqual(got["seconds"], 1 + self.TRIES * self.WAIT_S, "1 s before it reads again")
        self.assertIn(f"no answer after {self.TRIES} tries", got["text"])

    def test_a_refusal_from_yamcs_is_said_once(self):
        got = self.run_form(refuse=True)
        self.assertEqual(len(got["posts"]), 1)
        self.assertIn("refused by Yamcs, 400", got["text"])


if __name__ == "__main__":
    unittest.main()
