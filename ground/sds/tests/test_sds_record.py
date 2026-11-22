"""Phase D on the ground: naming the payload's record, waiting for its CFDP downlink, reading it from Yamcs's bucket,
and holding it against the L1 built from the archive.

Two things here would fail silently on the flight side, so they are pinned: F' accepts command strings of at most
40 characters on board whatever the dictionary says (a longer path is a FORMAT_ERROR; the SDS keeps to 39), and the
record is named by episode and last tic, which the ground must work out from L1 exactly as the payload does.

The wait is judged from what Yamcs and the archive show, in canned shapes taken from a live class 2 downlink on
4 October 2026 (docs/plans/sds-airflow.md): the transfer list, cfdpManager's events and the dispatcher's answer.
The comparison is the point of the phase: every disagreement must show up as agree=False, and things the archive
can legitimately miss (a late start, a lost status) must not.
"""
import ast
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import config, episodes, pipeline, products, record  # noqa: E402
from doomsat_sds.archive import RecordedArchive, iso  # noqa: E402
from doomsat_sds.catalog import Catalog  # noqa: E402
from doomsat_sds.store import canonical_json  # noqa: E402

REPO = os.path.dirname(os.path.dirname(SDS))

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
WINDOW = [1791074773533, 1791074807183]
CONTEXT = {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "seed": 7, "pilot_mode": "test", "repo_commit": "test",
           "level_set": "dev", "sources": {}}


def episode_two_l1():
    arch = RecordedArchive(FIXTURE)
    evs = arch.events(0, 2 ** 62, types=[config.EVENT_PREFIX + n for n in config.EPISODE_EVENTS])
    ep2 = [e for e in episodes.closed_episodes(evs) if e.number == 2][0]
    settings = config.Settings(home=Path("/nonexistent/sds"), doomsat_home=Path("/nonexistent"))
    doc, _ = pipeline.make_l1(settings, arch, dict(ep2.as_conf(), window=WINDOW), CONTEXT)
    return doc


def record_from(l1, first_tic=None):
    """The record the payload would have written for this episode, had every status reached the ground."""
    col = lambda n: products.column(l1, n)
    tics = [v for _, v in col("TIC")]
    path, last = [], None
    cols = l1["telemetry"]["columns"]
    it, ix, iy = cols.index("TIC"), cols.index("POS_X"), cols.index("POS_Y")
    for r in l1["telemetry"]["rows"]:
        if r[it] is not None and r[ix] is not None and r[iy] is not None:
            if last is None or r[it] - last >= 35:
                path.append([r[it], r[ix], r[iy]])
                last = r[it]
    final = {"tic": tics[-1], "kills": col("KILLS")[-1][1], "explored": col("EXPLORED_CELLS")[-1][1],
             "health": col("HEALTH")[-1][1], "x": col("POS_X")[-1][1], "y": col("POS_Y")[-1][1], "keys": 0,
             "level": 1, "dead": 1, "level_done": 0}
    return {"record": {"format": "doomsat-episode-record", "version": 1}, "episode": 2, "outcome": "died",
            "context": {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "seed": 7},
            "statuses_sent": len(set(tics)), "first_tic": tics[0] if first_tic is None else first_tic,
            "last_tic": record.last_tic(l1), "final": final,
            "health_min": min(v for _, v in col("HEALTH")), "path_every_tics": 35, "path": path}


class TestNaming(unittest.TestCase):
    def test_absolute_path_when_it_fits(self):
        s = config.Settings(home=Path("/root/doom/sds"), doomsat_home=Path("/root/doom"))
        self.assertEqual(record.source_path(s, "3-7788.json"), "/root/doom/run/rec/3-7788.json")

    def test_relative_to_the_flight_binary_when_the_absolute_path_is_too_long(self):
        s = config.Settings(home=Path("/x"), doomsat_home=Path("/home/a-rather-long-user-name/doom"))
        p = record.source_path(s, "3-7788.json")
        self.assertEqual(p, "../../../../../run/rec/3-7788.json")
        self.assertLessEqual(len(p), record.MAX_CMD_STRING)
        # bin -> DoomSat -> <OS> -> build-artifacts -> project -> DOOMSAT_HOME
        binary_cwd = Path("/home/a-rather-long-user-name/doom/DoomSat/build-artifacts/Linux/DoomSat/bin")
        self.assertEqual(os.path.normpath(binary_cwd / p), "/home/a-rather-long-user-name/doom/run/rec/3-7788.json")

    def test_nothing_fits(self):
        s = config.Settings(home=Path("/x"), doomsat_home=Path("/home/a-rather-long-user-name/doom"))
        with self.assertRaises(ValueError):
            record.source_path(s, "12345-4294967295-and-more.json")

    def test_sendfile_takes_all_seven_arguments_and_keeps_the_file(self):
        args = record.sendfile_args({"source": "/root/doom/run/rec/2-1180.json", "dest": "rec_x.json"})
        self.assertEqual(args, {"channelId": 0, "destId": 100, "cfdpClass": "CLASS_2", "keep": "KEEP", "priority": 0,
                                "sourceFileName": "/root/doom/run/rec/2-1180.json", "destFileName": "rec_x.json"})

    def test_destination_names_fit_and_are_flat(self):
        d = record.dest_name(episodes.episode_id(1791074807184, 65535))
        self.assertLessEqual(len(d), record.MAX_CMD_STRING)
        self.assertNotIn("/", d)


class TestAgainstL1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.l1 = episode_two_l1()

    def test_last_tic_comes_from_the_closing_event(self):
        self.assertEqual(record.last_tic(self.l1), 1180)

    def test_a_faithful_record_agrees(self):
        out = record.compare(record_from(self.l1), self.l1, CONTEXT)
        bad = [c for c in out["checks"] if c["agree"] is False]
        self.assertEqual(bad, [])
        self.assertTrue(out["agree"])
        self.assertEqual(out["product"]["version"], products.version("qa_record_check"))

    def test_each_kind_of_disagreement_is_caught(self):
        base = record_from(self.l1)
        for name, change in (
                ("kills", lambda r: r["final"].__setitem__("kills", r["final"]["kills"] + 1)),
                ("final position", lambda r: r["final"].__setitem__("x", r["final"]["x"] + 1.0)),
                ("outcome", lambda r: r.__setitem__("outcome", "level_finished")),
                ("episode number", lambda r: r.__setitem__("episode", 7)),
                ("context map", lambda r: r["context"].__setitem__("map", "E1M2")),
                ("path points", lambda r: r["path"][3].__setitem__(1, r["path"][3][1] + 5.0))):
            r = copy.deepcopy(base)
            change(r)
            checks = {c["check"]: c for c in record.compare(r, self.l1, CONTEXT)["checks"]}
            self.assertIs(checks[name]["agree"], False, name)

    def test_what_the_archive_may_legitimately_miss(self):
        first_archived = products.column(self.l1, "TIC")[0][1]
        r = record_from(self.l1, first_tic=first_archived - 30)   # statuses sent before the archive was listening
        r["statuses_sent"] += 200
        out = record.compare(r, self.l1, CONTEXT)
        checks = {c["check"]: c for c in out["checks"]}
        self.assertIs(checks["first tic"]["agree"], False)    # reported, because it is worth knowing
        self.assertIs(checks["statuses sent vs received"]["agree"], True)
        self.assertLess(out["statuses"]["delivery"], 1.0)

    def test_a_position_lost_on_board_is_unattributable_not_a_disagreement(self):
        # Seen live (episode 20261004T020515Z-e0002): two statuses, tics 29308 and 29311, shared one F' time tag;
        # the archive kept both TICs but only the second position. Pairing that position with tic 29308 gave a
        # false 30-unit "disagreement". A time tag with fewer positions than tics must not be compared.
        ep = episodes.ClosedEpisode(4, 2_000, "PlayerDied", "died", None)
        science = {n: [] for n in config.SCIENCE}
        science["TIC"] = [(1_000, 900, 10), (1_000, 950, 13), (1_100, 1_000, 16)]
        science["POS_X"] = [(1_000, 950, 5.0), (1_100, 1_000, 7.0)]
        science["POS_Y"] = [(1_000, 950, 6.0), (1_100, 1_000, 8.0)]
        l1 = products.build_l1(ep, (1_000, 1_100), science, {}, [], [], CONTEXT, [])
        rec = {"episode": 4, "outcome": "died", "first_tic": 10, "last_tic": 16, "statuses_sent": 3,
               "health_min": 0, "context": {}, "path": [[10, 2.0, 3.0], [16, 7.0, 8.0]],
               "final": {"kills": None, "explored": None, "health": None, "x": 7.0, "y": 8.0}}
        checks = {c["check"]: c for c in record.compare(rec, l1, CONTEXT)["checks"]}
        self.assertIs(checks["path points"]["agree"], True)       # tic 16 compared and equal; tic 10 not compared
        self.assertEqual(checks["path points"]["archive"], 1)
        self.assertIn("1 unattributable", checks["path points"]["note"])
        rec["path"][1][1] = 7.5                                   # a real disagreement is still caught
        checks = {c["check"]: c for c in record.compare(rec, l1, CONTEXT)["checks"]}
        self.assertIs(checks["path points"]["agree"], False)

    def test_context_the_ground_never_learnt_is_not_a_disagreement(self):
        out = record.compare(record_from(self.l1), self.l1, {"wad": None, "map": None})
        checks = {c["check"]: c for c in out["checks"]}
        self.assertIsNone(checks["context wad"]["agree"])
        self.assertTrue(out["agree"])

    def test_a_wad_switch_is_the_payloads_reset(self):
        # The payload's recorder sees only the episode number change when LOAD_WAD rebuilds the game.
        switched = copy.deepcopy(self.l1)
        switched["episode"]["outcome"] = "wad_switch"
        r = record_from(self.l1)
        r["outcome"] = "reset"
        checks = {c["check"]: c for c in record.compare(r, switched, CONTEXT)["checks"]}
        self.assertIs(checks["outcome"]["agree"], True)
        self.assertIn("WAD switch", checks["outcome"]["note"])
        r["outcome"] = "died"
        self.assertIs({c["check"]: c for c in record.compare(r, switched, CONTEXT)["checks"]}["outcome"]["agree"],
                      False)
        r["outcome"] = "reset"                         # and the ground's reset is still only a reset
        reset = copy.deepcopy(self.l1)
        reset["episode"]["outcome"] = "reset"
        self.assertIs({c["check"]: c for c in record.compare(r, reset, CONTEXT)["checks"]}["outcome"]["agree"], True)
        self.assertIs({c["check"]: c for c in record.compare(r, self.l1, CONTEXT)["checks"]}["outcome"]["agree"],
                      False)

    def test_the_patch_wad_is_compared_when_both_sides_carry_it(self):
        def pwad_check(theirs, ctx):
            r = record_from(self.l1)
            if theirs is not KeyError:
                r["context"]["pwad"] = theirs
            return {c["check"]: c for c in record.compare(r, self.l1, ctx)["checks"]}["context pwad"]

        on_main = dict(CONTEXT, pwad=None, wad_loads=0)
        self.assertIs(pwad_check(None, on_main)["agree"], True)                 # no patch WAD on either side
        self.assertIs(pwad_check("basic.wad", on_main)["agree"], False)
        self.assertIs(pwad_check("basic.wad", dict(on_main, pwad="basic.wad"))["agree"], True)
        self.assertIsNone(pwad_check(KeyError, on_main)["agree"])               # a record from before main
        self.assertIn("record predates", pwad_check(KeyError, on_main)["note"])
        self.assertIsNone(pwad_check(None, CONTEXT)["agree"])                   # a context from before main
        self.assertIsNone(pwad_check(None, dict(on_main, wad=None))["agree"])   # the ground never learnt the WAD

    def test_the_wad_compared_is_the_one_flown(self):
        flown = dict(CONTEXT, wad="freedoom2.wad", pwad=None, wad_loads=1, wad_launch="freedoom1.wad",
                     sources={"wad": "telemetry WAD_IWAD, WAD_PWAD, WAD_LOADS: the first sample during the episode"})
        r = record_from(self.l1)
        r["context"].update(wad="freedoom2.wad", pwad=None)     # after a LOAD_WAD, as the payload writes it
        checks = {c["check"]: c for c in record.compare(r, self.l1, flown)["checks"]}
        self.assertIs(checks["context wad"]["agree"], True)
        self.assertEqual(checks["context wad"]["note"], "the WAD flown, from telemetry")
        self.assertEqual({c["check"]: c for c in record.compare(r, self.l1, CONTEXT)["checks"]}["context wad"]["note"],
                         "the payload's --wad")

    def test_the_check_is_byte_stable_at_its_version(self):
        out = canonical_json(record.compare(record_from(self.l1), self.l1, CONTEXT))
        key = "l1_episode@%s qa_record_check@%s" % (products.version("l1_episode"), products.version("qa_record_check"))
        if key not in GOLDEN:
            self.skipTest("no recorded checksum for %s yet; add it to GOLDEN" % key)
        self.assertEqual(hashlib.sha256(out).hexdigest(), GOLDEN[key],
                         "qa_record_check changed without a version bump in products.ALGORITHMS")


class TestPlan(unittest.TestCase):
    def test_plan_from_the_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = config.Settings(home=Path(tmp) / "sds", doomsat_home=Path("/root/doom"))
            arch = RecordedArchive(FIXTURE)
            evs = arch.events(0, 2 ** 62, types=[config.EVENT_PREFIX + n for n in config.EPISODE_EVENTS])
            ep2 = [e for e in episodes.closed_episodes(evs) if e.number == 2][0]
            with Catalog(s.catalog) as c, mock.patch("doomsat_sds.context.capture", return_value=CONTEXT), \
                    mock.patch("doomsat_sds.pipeline._commit", return_value="test"):
                pipeline.l1(s, arch, c, dict(ep2.as_conf(), window=WINDOW))
                p = record.plan(s, c, ep2.episode_id)
            self.assertEqual(p["name"], "2-1180.json")
            self.assertEqual(p["source"], "/root/doom/run/rec/2-1180.json")
            self.assertEqual(p["dest"], "rec_%s.json" % ep2.episode_id)
            self.assertNotIn("mirror_path", p)      # on main nothing mirrors a downlinked file to disk

    def test_the_watch_asks_for_every_episode_the_payload_wrote_a_record_for(self):
        # Read from the DAG's source: importing it needs Airflow. An interrupted episode never wrote a record; a WAD
        # switch did (the payload writes it as a reset). Each name must be one episodes.py spells.
        tree = ast.parse(Path(SDS, "dags", "sds_record.py").read_text(encoding="utf-8"))
        found = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "REQUESTABLE" for t in n.targets)]
        self.assertEqual(found, [("died", "level_finished", "reset", "wad_switch")])
        spelled = {n.value for n in ast.walk(ast.parse(Path(SDS, "doomsat_sds", "episodes.py").read_text(
            encoding="utf-8"))) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertLessEqual(set(found[0]), spelled)


# qa_record_check's bytes for the faithful record of episode 2 in the fixture, by version. 1.2.0 was recorded from
# the code before 1.3.0 added the patch WAD check and the note on where the ground's WAD came from.
GOLDEN = {
    "l1_episode@1.1.0 qa_record_check@1.2.0": "a100bc65b9c87323aeed162ac28d18aa19ec5e135d7cd9d9a9a698b879a5cde1",
    "l1_episode@1.1.0 qa_record_check@1.3.0": "db349266011b01000de3a6e8a009cf72663c0c8d0e474755391cbfbf2c4b2ad9",
}


# ------------------------------------------------------------------------------------------------ the CFDP downlink
def ms(text):
    return int(round(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000))


SRC = "/root/doom/run/rec/2-98749.json"
DEST = "rec_probe_2-98749.json"
ISSUED = ms("2026-10-04T15:59:39.900Z")       # Yamcs's clock, as cmd.generation_time gives it
SINCE = ISSUED                                # a transfer created before the command is not this request's
COMMAND = {"command_id": "1791129579900-127.0.0.1-1", "issued_ms": ISSUED}
OPCODE = str(config.SENDFILE_OPCODE)          # the event's Opcode argument, as Yamcs's event extra holds it
# The transfer Yamcs listed for the live downlink (int64 fields come back as JSON strings)
LIVE_TRANSFER = {
    "id": "0", "startTime": "2026-10-04T15:59:39.943Z", "state": "COMPLETED", "bucket": "cfdpDown",
    "objectName": DEST, "remotePath": SRC, "direction": "DOWNLOAD", "totalSize": "82038",
    "sizeTransferred": "82038", "reliable": True, "failureReason": "",
    "transactionId": {"sequenceNumber": 1, "initiatorEntity": "42"}, "creationTime": "2026-10-04T15:59:39.941Z",
    "transferType": "FILE TRANSFER", "localEntity": {"name": "ground", "id": "100"},
    "remoteEntity": {"name": "doomsat", "id": "42"}}


def event(when, name, message="", **extra):
    kind = name if "." in name else "DoomSat.cfdpManager." + name
    return {"t": ms(when), "rt": None, "source": config.EVENT_SOURCE, "type": kind, "message": message,
            "extra": {k: str(v) for k, v in extra.items()}, "seq": None}


# F' events carry TM time, about 0.9 s ahead of the ground clock that stamped the command and the transfer
LIVE_EVENTS = [
    event("2026-10-04T15:59:40.830Z", "CdhCore.cmdDisp.OpCodeCompleted", "Opcode 0x10006000 completed",
          Opcode=OPCODE),
    event("2026-10-04T15:59:40.844Z", "TxFileQueued", "TX file queued for %s (transaction 1)" % SRC,
          sourceFileName=SRC, transactionSeq=1),
    event("2026-10-04T15:59:40.880Z", "TxFileTransferStarted", "TX CLASS_2 transaction 1 transfer started",
          cfdpClass="CLASS_2", seqNum=1, srcEid=42, srcFile=SRC, destEid=100, destFile=DEST, fileSize=82038),
    event("2026-10-04T15:59:42.913Z", "TxFileTransferCompleted", "TX CLASS_2 transaction 1 completed",
          cfdpClass="CLASS_2", seqNum=1, srcEid=42, srcFile=SRC, destEid=100, destFile=DEST, fileSize=82038),
]
OPEN_FAILED = [
    event("2026-10-04T15:59:40.830Z", "CdhCore.cmdDisp.OpCodeCompleted", "completed", Opcode=OPCODE),
    event("2026-10-04T15:59:40.844Z", "TxFileQueued", "queued", sourceFileName=SRC, transactionSeq=2),
    event("2026-10-04T15:59:41.850Z", "TxFileOpenFailed", "TX class CLASS_2 transaction 42:2 failed to open file",
          cfdpClass="CLASS_2", srcEid=42, seqNum=2, filename=SRC, status=-2),
    event("2026-10-04T15:59:41.851Z", "TxFileTransferFailed", "TX CLASS_2 transaction 2 FAILED, error code 4",
          cfdpClass="CLASS_2", seqNum=2, srcEid=42, srcFile=SRC, destEid=100, destFile=DEST, conditionCode=4),
]


def judge(transfers=(), events=(), commands=()):
    return record.verdict(SRC, SINCE, COMMAND["command_id"], list(commands), list(events), list(transfers))


def transfer(**changes):
    return dict(LIVE_TRANSFER, **changes)


class TestTheWait(unittest.TestCase):
    def test_a_completed_transfer_is_received(self):
        v = judge([LIVE_TRANSFER], LIVE_EVENTS)
        self.assertEqual((v["state"], v["finding"], v["answer"]), ("received", None, "OK"))
        self.assertEqual(v["transfer"]["objectName"], DEST)
        self.assertEqual([e["type"] for e in v["events"]], ["OpCodeCompleted", "TxFileQueued", "TxFileTransferStarted",
                                                            "TxFileTransferCompleted"])

    def test_a_file_received_ok_with_its_finished_pdu_unacknowledged_is_received_and_said(self):
        # Yamcs saved the checksum-verified object, then gave up waiting for F' to acknowledge its FIN (3 s x 10).
        t = transfer(state="FAILED", failureReason="File was received OK but the Finished PDU has not been "
                                                   "acknowledged")
        v = judge([t], LIVE_EVENTS[:3])
        self.assertEqual((v["state"], v["finding"]), ("received", "record_fin_unacknowledged"))
        self.assertIn("Finished PDU", v["why"])

    def test_any_other_failed_transfer_fails(self):
        for reason in ("Checksum does not match", "inactivity timeout", ""):
            v = judge([transfer(state="FAILED", failureReason=reason)], LIVE_EVENTS[:3])
            self.assertEqual((v["state"], v["finding"]), ("failed", "record_transfer_failed"), reason)

    def test_a_transfer_in_progress_is_waited_for_and_yamcs_decides(self):
        for state in ("RUNNING", "QUEUED", "PAUSED", "CANCELLING"):
            v = judge([transfer(state=state)], LIVE_EVENTS[:3])
            self.assertEqual((v["state"], v["finding"]), ("waiting", None), state)
        # F' giving up on a transfer Yamcs lists is Yamcs's to settle (it fails it on inactivity)
        v = judge([transfer(state="RUNNING")], OPEN_FAILED[:2] + OPEN_FAILED[3:])
        self.assertEqual(v["state"], "waiting")

    def test_nothing_yet_is_waiting(self):
        self.assertEqual(judge()["state"], "waiting")
        v = judge([], LIVE_EVENTS[:2])
        self.assertEqual((v["state"], v["answer"]), ("waiting", "OK"))

    def test_a_stale_transfer_of_the_same_file_is_not_this_one(self):
        # An earlier request for the same path, or an earlier flight's (transactions restart at 1 every boot)
        stale = transfer(creationTime=iso(SINCE - 1), startTime=iso(SINCE - 1))
        self.assertIsNone(record.find_transfer([stale], SRC, SINCE))
        self.assertEqual(judge([stale], LIVE_EVENTS[:2])["state"], "waiting")
        self.assertEqual(judge([stale, LIVE_TRANSFER])["transfer"]["creationTime"], LIVE_TRANSFER["creationTime"])

    def test_a_taken_name_gets_a_suffix_and_the_name_comes_from_the_transfer(self):
        older = transfer(id="7", state="FAILED", failureReason="Checksum does not match",
                         creationTime=iso(SINCE + 10), transactionId={"sequenceNumber": 1, "initiatorEntity": "42"})
        newer = transfer(id="8", objectName=DEST + "(1)", creationTime=iso(SINCE + 2_000),
                         transactionId={"sequenceNumber": 2, "initiatorEntity": "42"})
        v = judge([newer, older])
        self.assertEqual((v["state"], v["transfer"]["objectName"]), ("received", DEST + "(1)"))

    def test_other_files_uploads_and_other_spellings_are_not_this_one(self):
        others = [transfer(remotePath="/root/doom/run/rec/12-98749.json"), transfer(direction="UPLOAD"),
                  transfer(remotePath="../../../../../run/rec/2-98749.json")]
        self.assertEqual(judge(others)["state"], "waiting")

    def test_a_file_that_cannot_be_sent_fails_at_once(self):
        v = judge([], OPEN_FAILED)
        self.assertEqual((v["state"], v["finding"]), ("failed", "record_unavailable"))
        self.assertIn("failed to open", v["why"])
        zero = [event("2026-10-04T15:59:41.850Z", "TxZeroLengthFile", "cannot transfer zero-length file",
                      cfdpClass="CLASS_2", srcEid=42, seqNum=2, filename=SRC)]
        self.assertEqual(judge([], zero)["finding"], "record_unavailable")
        initiate = [event("2026-10-04T15:59:41.850Z", "SendFileInitiateFail", "Failed to initiate",
                          sourceFileName=SRC)]
        self.assertEqual(judge([], initiate)["finding"], "record_unavailable")

    def test_a_transfer_failed_on_board_with_no_yamcs_transfer_fails(self):
        v = judge([], [OPEN_FAILED[1], event("2026-10-04T15:59:41.840Z", "TxSendMetadataFailed", "no metadata",
                                             cfdpClass="CLASS_2", srcEid=42, seqNum=2), OPEN_FAILED[3]])
        self.assertEqual((v["state"], v["finding"]), ("failed", "record_transfer_failed"))
        self.assertIn("TxSendMetadataFailed", [e["type"] for e in v["events"]])     # attributed by its transaction

    def test_events_about_another_file_or_transaction_are_not_this_ones(self):
        other = [event("2026-10-04T15:59:41.850Z", "TxFileOpenFailed", "failed to open", cfdpClass="CLASS_2",
                       srcEid=42, seqNum=5, filename="/root/doom/run/rec/1-7.json", status=-2),
                 event("2026-10-04T15:59:41.860Z", "TxAckLimitReached", "ACK limit", cfdpClass="CLASS_2", srcEid=42,
                       seqNum=5)]
        v = judge([], LIVE_EVENTS[:2] + other)
        self.assertEqual(v["state"], "waiting")
        self.assertNotIn("TxFileOpenFailed", [e["type"] for e in v["events"]])
        self.assertEqual(record.about(LIVE_EVENTS[:2] + other, SRC), [LIVE_EVENTS[1]])

    def test_a_refused_command_fails_at_once(self):
        refused = [event("2026-10-04T15:59:40.830Z", "MaxTxTransactionsReached", "Maximum number of commanded TX"),
                   event("2026-10-04T15:59:40.831Z", "CdhCore.cmdDisp.OpCodeError",
                         "Opcode 0x10006000 completed with error EXECUTION_ERROR", Opcode=OPCODE,
                         error="EXECUTION_ERROR")]
        v = judge([], refused)
        self.assertEqual((v["state"], v["finding"], v["answer"]), ("failed", "record_unavailable", "EXECUTION_ERROR"))
        self.assertEqual([e["type"] for e in v["events"]], ["MaxTxTransactionsReached", "OpCodeError"])
        # another command's error is not SendFile's answer
        other = [event("2026-10-04T15:59:40.831Z", "CdhCore.cmdDisp.OpCodeError", "error", Opcode="268455941",
                       error="VALIDATION_ERROR")]
        self.assertEqual((judge([], other)["state"], judge([], other)["answer"]), ("waiting", None))

    def test_a_command_yamcs_never_sent_fails_at_once(self):
        sent = [{"id": COMMAND["command_id"], "acks": {"Queued": "OK", "Released": "OK", "Sent": "NOK"}}]
        v = judge([], [], sent)
        self.assertEqual((v["state"], v["finding"], v["answer"]), ("failed", "record_unavailable", "NOK"))
        fine = [{"id": COMMAND["command_id"], "acks": {"Queued": "OK", "Released": "OK", "Sent": "OK"}},
                {"id": "someone-else", "acks": {"Sent": "NOK"}}]
        self.assertEqual(judge([], [], fine)["state"], "waiting")

    def test_yamcs_times(self):
        self.assertEqual(record._ms("2026-10-04T15:59:39.941Z"), ms("2026-10-04T15:59:39.941Z"))
        self.assertEqual(record._ms("2026-10-04T15:59:39Z"), ms("2026-10-04T15:59:39.000Z"))
        self.assertEqual(record._ms("2026-10-04T15:59:39.9415Z"), ms("2026-10-04T15:59:39.941Z"))
        self.assertIsNone(record._ms(None))
        self.assertIsNone(record._ms("yesterday"))


# ------------------------------------------------------------------------------------------------ Yamcs, faked
class Response:
    def __init__(self, status_code=200, body=b"", data=None):
        self.status_code, self.content, self.data = status_code, body, data

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("%d Client Error" % self.status_code)


class FakeYamcs:
    """The two REST paths record.py uses, GET and DELETE only: the CFDP transfer list and the cfdpDown bucket."""

    def __init__(self, transfers=(), objects=None):
        self.transfers, self.objects, self.calls = list(transfers), dict(objects or {}), []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if url.endswith("/api/filetransfer/fprime-project/cfdp/transfers"):
            return Response(data={"transfers": self.transfers})
        m = re.search(r"/api/storage/buckets/cfdpDown/objects/([^/]+)$", url)
        name = unquote(m.group(1)) if m else None
        return Response(body=self.objects[name]) if name in self.objects else Response(404)

    def delete(self, url, timeout=None):
        self.calls.append(("DELETE", url, None))
        m = re.search(r"/api/storage/buckets/cfdpDown/objects/([^/]+)$", url)
        return Response() if m and self.objects.pop(unquote(m.group(1)), None) is not None else Response(404)


class FakeArchive:
    def __init__(self, events=(), commands=()):
        self._events, self._commands, self.reads = list(events), list(commands), []

    def events(self, start_ms, stop_ms, types=None):
        self.reads.append(("events", start_ms, stop_ms, tuple(types or ())))
        return [e for e in self._events if start_ms <= e["t"] < stop_ms and (not types or e["type"] in types)]

    def commands(self, start_ms, stop_ms):
        self.reads.append(("commands", start_ms, stop_ms))
        return [c for c in self._commands if start_ms <= c["t"] < stop_ms]


class TestObserve(unittest.TestCase):
    def test_it_reads_the_right_windows_and_only_reads(self):
        s = config.Settings(home=Path("/x/sds"), doomsat_home=Path("/root/doom"))
        yamcs = FakeYamcs([LIVE_TRANSFER])
        archive = FakeArchive(LIVE_EVENTS, [{"t": ISSUED, "id": COMMAND["command_id"], "acks": {"Sent": "OK"}}])
        now = ms("2026-10-04T15:59:45.000Z")
        v = record.observe(s, archive, {"source": SRC}, COMMAND, now, http=yamcs)
        self.assertEqual(v["state"], "received")
        self.assertEqual(yamcs.calls, [("GET", "http://localhost:8090/api/filetransfer/fprime-project/cfdp/transfers",
                                        {"direction": "DOWNLOAD", "start": iso(ISSUED - record.TRANSFER_SLACK_MS)})])
        kinds = {r[0]: r for r in archive.reads}
        self.assertEqual(kinds["events"][1:3], (ISSUED - record.EVENT_SLACK_MS, now + 5_000))
        self.assertEqual(set(kinds["events"][3]), set(record.DOWNLINK_EVENTS + record.ANSWER_EVENTS))
        self.assertEqual(kinds["commands"][1:], (ISSUED - 1_000, ISSUED + 1_000))


    def test_a_transfer_the_list_returns_from_before_the_command_is_not_this_one(self):
        # The list is asked from TRANSFER_SLACK_MS before the command, but Yamcs creates this request's transfer only
        # when its first PDU arrives, after F' has the command: an earlier one of the same path in that slack (a
        # request cleared and sent again) must not settle this one.
        s = config.Settings(home=Path("/x/sds"), doomsat_home=Path("/root/doom"))
        earlier = transfer(id="5", state="FAILED", failureReason="Checksum does not match",
                           creationTime=iso(ISSUED - 100), startTime=iso(ISSUED - 98))
        now = ms("2026-10-04T15:59:45.000Z")
        v = record.observe(s, FakeArchive(LIVE_EVENTS[:2]), {"source": SRC}, COMMAND, now, http=FakeYamcs([earlier]))
        self.assertEqual((v["state"], v["transfer"]), ("waiting", None))
        v = record.observe(s, FakeArchive(LIVE_EVENTS), {"source": SRC}, COMMAND, now,
                           http=FakeYamcs([earlier, LIVE_TRANSFER]))
        self.assertEqual((v["state"], v["transfer"]["id"]), ("received", "0"))

    def test_a_poke_gives_up_on_a_stalled_transfer_list_quickly(self):
        seen = []

        class Http:
            def get(self, url, params=None, timeout=None):
                seen.append(timeout)
                return Response(data={"transfers": []})

        record.list_transfers(config.Settings(home=Path("/x/sds"), doomsat_home=Path("/root/doom")), ISSUED, Http())
        self.assertEqual(seen, [(5, record.POKE_READ_S)])


def dag_function(name):
    """A task function of the record DAG, from its source: importing the DAG needs Airflow."""
    tree = ast.parse(Path(SDS, "dags", "sds_record.py").read_text(encoding="utf-8"))
    return next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == name)


def calls_in(func):
    return [n for n in ast.walk(func) if isinstance(n, ast.Call)]


class TestAfterTheWait(unittest.TestCase):
    """What the record DAG decides around the wait: which finding report_downlink_events records
    (record.downlink_finding, pure), and, read from the DAG's source, the sensor's settings, its one raise and the
    delete after ingest."""

    def test_the_finding_for_each_way_the_wait_can_end(self):
        received = judge([LIVE_TRANSFER], LIVE_EVENTS)
        unacked = judge([transfer(state="FAILED", failureReason="File was received OK but the Finished PDU has not "
                                                                "been acknowledged")], LIVE_EVENTS)
        failed = judge([transfer(state="FAILED", failureReason="Checksum does not match")], LIVE_EVENTS[:3])
        unavailable = judge([], OPEN_FAILED)
        waiting = judge([], LIVE_EVENTS[:2])
        # the sensor failed on a verdict and left no answer: the verdict, observed again, says why
        self.assertEqual(record.downlink_finding(failed, None), "record_transfer_failed")
        self.assertEqual(record.downlink_finding(unavailable, None), "record_unavailable")
        # the record came: clean, or with F' never acknowledging the Finished PDU
        self.assertIsNone(record.downlink_finding(received, received))
        self.assertEqual(record.downlink_finding(unacked, unacked), "record_fin_unacknowledged")
        # the wait timed out with no answer: nothing settled yet, or the transfer finished since (not ingested)
        self.assertEqual(record.downlink_finding(waiting, None), "record_not_received")
        self.assertEqual(record.downlink_finding(received, None), "record_not_received")

    def test_the_sensor_pokes_again_after_a_failed_read_and_stops_on_a_verdict(self):
        f = dag_function("wait_for_downlinked_file")
        [deco] = f.decorator_list
        self.assertEqual(ast.unparse(deco.func), "task.sensor")
        self.assertEqual({k.arg: ast.unparse(k.value) for k in deco.keywords},
                         {"poke_interval": "5", "timeout": "120", "mode": "'poke'", "silent_fail": "True",
                          "execution_timeout": "timedelta(minutes=4)"})
        # Airflow raises an execution_timeout past silent_fail: a poke's three reads, each giving up after
        # POKE_READ_S without a byte (connect 10 s for the archive's two, 5 s for the list), must end well before it.
        [arch] = [c for c in calls_in(f) if ast.unparse(c.func) == "YamcsArchive"]
        self.assertEqual({k.arg: ast.unparse(k.value) for k in arch.keywords}, {"read_s": "record.POKE_READ_S"})
        self.assertLess(2 * (10 + record.POKE_READ_S) + (5 + record.POKE_READ_S), 120)
        ifs = [n for n in ast.walk(f) if isinstance(n, ast.If)]
        self.assertEqual([ast.unparse(n.test) for n in ifs], ["seen['state'] == 'failed'"])
        self.assertIsInstance(ifs[0].body[0], ast.Raise)
        self.assertEqual(ast.unparse(ifs[0].body[0].exc.func), "AirflowFailException")
        [ret] = [n for n in ast.walk(f) if isinstance(n, ast.Return)]
        self.assertEqual(ast.unparse(ret.value),
                         "PokeReturnValue(is_done=seen['state'] == 'received', xcom_value=seen)")

    def test_the_finding_is_recorded_after_the_wait_whatever_its_end(self):
        f = dag_function("report_downlink_events")
        trigger = [k for k in f.decorator_list[0].keywords if k.arg == "trigger_rule"]
        self.assertEqual([ast.unparse(k.value) for k in trigger], ["'all_done'"])
        found = [c for c in calls_in(f) if ast.unparse(c.func) == "record.downlink_finding"]
        self.assertEqual([[ast.unparse(a) for a in c.args] for c in found], [["seen", "received"]])
        adds = [c for c in calls_in(f) if ast.unparse(c.func) == "catalog.add_finding"]
        self.assertEqual([ast.unparse(c.args[0]) for c in adds], ["finding"])

    def test_the_ingested_object_and_only_after_ingest_is_deleted(self):
        f = dag_function("ingest_record")
        order = [ast.unparse(c.func) for c in sorted(calls_in(f), key=lambda c: (c.lineno, c.col_offset))]
        self.assertIn("record.forget_downlinked", order)
        self.assertLess(order.index("record.ingest"), order.index("record.forget_downlinked"))
        [forget] = [c for c in calls_in(f) if ast.unparse(c.func) == "record.forget_downlinked"]
        self.assertEqual(ast.unparse(forget.args[1]), "received['transfer']")


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = config.Settings(home=Path(self.tmp.name) / "sds", doomsat_home=Path("/root/doom"))
        self.rec = json.dumps({"record": {"format": "doomsat-episode-record", "version": 1}, "episode": 2},
                              sort_keys=True).encode()
        self.p = {"episode_id": "20261004T160006Z-e0002", "source": SRC, "dest": DEST}
        self.t = transfer(id="8", objectName=DEST + "(1)", totalSize=str(len(self.rec)),
                          transactionId={"sequenceNumber": 2, "initiatorEntity": "42"})

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self):
        c = Catalog(self.s.catalog)
        c.add_episode({"episode_id": self.p["episode_id"], "number": 2, "outcome": "died",
                       "closing_event": "PlayerDied", "closing_ms": ISSUED - 60_000, "context": CONTEXT})
        return c

    def test_the_transfers_object_is_ingested_and_only_it_deleted(self):
        yamcs = FakeYamcs(objects={DEST: b"an older object of the same name", DEST + "(1)": self.rec})
        with self.catalog() as c, mock.patch("doomsat_sds.pipeline._commit", return_value="test"):
            ref = record.ingest(self.s, c, self.p, COMMAND, self.t, run_id="rec__x", http=yamcs)
            row = c.current(self.p["episode_id"], "l0_record")
        self.assertEqual(ref["status"], "new")
        self.assertEqual(Path(ref["path"]).read_bytes(), self.rec)
        self.assertEqual(row["inputs"], [{"source": "cfdp", "bucket": "cfdpDown", "object": DEST + "(1)",
                                          "transfer_id": "8",
                                          "transaction_id": {"sequenceNumber": 2, "initiatorEntity": "42"},
                                          "transfer_state": "COMPLETED", "transfer_created": self.t["creationTime"],
                                          "on_board": SRC, "command_id": COMMAND["command_id"]}])
        self.assertTrue(yamcs.calls[0][1].endswith("/api/storage/buckets/cfdpDown/objects/" + DEST + "%281%29"))
        self.assertTrue(record.forget_downlinked(self.s, self.t, http=yamcs))
        self.assertEqual(sorted(yamcs.objects), [DEST])                 # the other object is not touched
        self.assertFalse(record.forget_downlinked(self.s, self.t, http=yamcs))
        self.assertEqual({m for m, _, _ in yamcs.calls}, {"GET", "DELETE"})

    def test_an_object_of_another_size_or_not_a_record_is_refused(self):
        with self.catalog() as c, mock.patch("doomsat_sds.pipeline._commit", return_value="test"):
            longer = FakeYamcs(objects={DEST + "(1)": self.rec + b"\n"})     # still a record, but not what was sent
            with self.assertRaises(ValueError):
                record.ingest(self.s, c, self.p, COMMAND, self.t, http=longer)
            foreign = b'{"something": "else"}'
            with self.assertRaises(ValueError):
                record.ingest(self.s, c, self.p, COMMAND, dict(self.t, totalSize=str(len(foreign))),
                              http=FakeYamcs(objects={DEST + "(1)": foreign}))
            self.assertIsNone(c.current(self.p["episode_id"], "l0_record"))


class TestTheFlightSoftware(unittest.TestCase):
    def test_the_sendfile_opcode_is_cfdp_managers_first_command(self):
        # The dispatcher's answer names the command by opcode: cfdpManager's base id plus SendFile's, 0.
        with open(os.path.join(REPO, "flight", "DoomSat", "Top", "instances.fpp"), encoding="utf-8") as f:
            m = re.search(r"instance\s+cfdpManager\s*:\s*[\w.]+\s+base\s+id\s+(0x[0-9A-Fa-f]+)", f.read())
        self.assertIsNotNone(m)
        self.assertEqual(config.SENDFILE_OPCODE, int(m.group(1), 16))

    def test_the_names_are_in_the_built_dictionary(self):
        home = os.environ.get("DOOMSAT_HOME") or os.path.expanduser("~/doom")
        found = sorted(Path(home, "DoomSat", "build-artifacts").glob("*/DoomSat/dict/DoomSatTopologyDictionary.json"))
        if not found:
            self.skipTest("no built F' dictionary under %s" % home)
        d = json.loads(found[0].read_text(encoding="utf-8"))
        commands = {c["name"]: c for c in d["commands"]}
        if "DoomSat.cfdpManager.SendFile" not in commands:
            self.skipTest("the built flight software is from before main (no cfdpManager)")
        names = {e["name"] for e in d["events"]}
        self.assertEqual([n for n in record.DOWNLINK_EVENTS + record.ANSWER_EVENTS if n not in names], [])
        send = commands["DoomSat.cfdpManager.SendFile"]
        self.assertEqual(send["opcode"], config.SENDFILE_OPCODE)
        self.assertEqual([a["name"] for a in send["formalParams"]], list(record.sendfile_args({"source": "s",
                                                                                              "dest": "d"})))


if __name__ == "__main__":
    unittest.main()
