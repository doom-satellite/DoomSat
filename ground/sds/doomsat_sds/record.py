"""Phase D, the file seam: ask the payload for its own record of an episode, downlink it, check it against L1.

    plan      which file to ask for and what to call it on the ground
    request   the one command the data system ever sends: cfdpManager SendFile, through Yamcs
    observe   how the downlink stands, read only: SendFile's answer, cfdpManager's events, Yamcs's CFDP transfers
    ingest    the downlinked file, read from Yamcs's bucket cfdpDown, becomes the L0 product `l0_record`
    compare   the record against the L1 episode built from the archive; every disagreement is a finding

The payload (payload/episode_record.py, with --records on) writes $DOOMSAT_HOME/run/rec/<episode>-<last tic>.json
when an episode ends. The ground knows both numbers without asking: the episode from L1, the last tic from the
PlayerDied/LevelFinished event (or, after a reset or a WAD switch, from the last TIC sample, which only works if
that status reached the ground). On main the file goes down as a CCSDS CFDP class 2 transfer: cfdpManager reads
it, Yamcs's CfdpService rebuilds it, checks its checksum and saves it in the bucket cfdpDown under the
destination name, with "(1)", "(2)"... added when that name is taken. There is no file on disk to watch, so the
wait polls Yamcs's transfer list with a GET; nothing here POSTs to /filetransfer, so nothing here starts, pauses
or cancels a transfer. Once ingested, the object is deleted: cfdpDown holds 1000 objects and 100 MB.

What the flight software and Yamcs do, which shapes the code:
- F' command strings hold at most 40 characters on board (FW_CMD_STRING_MAX_SIZE), although the dictionary says
  200; a longer path is a FORMAT_ERROR. Paths are kept to 39. The absolute path is used when it fits, otherwise
  the same file relative to the F' binary's working directory, which is build-artifacts/<OS>/DoomSat/bin.
- cfdpManager answers SendFile OK when it has queued the transfer, and fails later, asynchronously, if the file
  cannot be opened: TxFileOpenFailed or TxZeroLengthFile, then TxFileTransferFailed. No metadata PDU is sent
  then, so Yamcs never lists a transfer, and those events end the wait at once.
- In class 2 Yamcs completes a transfer only when F' acknowledges its Finished PDU. A transfer that ends FAILED
  with "File was received OK but the Finished PDU has not been acknowledged" has left its checksum-verified
  object in the bucket all the same: it is ingested, and the lost acknowledgement is recorded as a finding.
- F' events carry the spacecraft's time, which ran about 0.9 s ahead of Yamcs's clock (which stamps the command
  and the transfer) until tools/yamcs_time_patch.py, and may again on an unpatched install; F' numbers transactions from 1 at every boot. So a transfer is found by its source path and the
  time Yamcs created it, and the event window opens a little before the command. Yamcs creates a downlink when
  its first PDU arrives, after F' has the command, so this request's transfer is never older than the command.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from . import config, products
from .archive import iso
from .catalog import Catalog
from .config import Settings
from .store import episode_product_path, read_json

MAX_CMD_STRING = 39                 # 40 on board (FW_CMD_STRING_MAX_SIZE), kept one short
BIN_TO_HOME = "../../../../../"     # bin -> DoomSat -> <OS> -> build-artifacts -> DoomSat (project) -> $DOOMSAT_HOME
# SendFile's other five arguments. Yamcs requires all seven: the mission database gives none a default.
SENDFILE_ARGS = {
    "channelId": 0,                 # channel 1 is wired too, but must not carry class 2
    "destId": 100,                  # the ground's CFDP entity (Yamcs); the spacecraft is 42
    "cfdpClass": "CLASS_2",         # acknowledged: lost PDUs are sent again (ACK, NAK and FIN go up on TC VC 2)
    "keep": "KEEP",                 # DELETE (enum value 0) would remove the payload's record on board once sent
    "priority": 0,
}
CFDP_SERVICE = "cfdp"               # Yamcs's CfdpService in instance fprime-project
BUCKET = "cfdpDown"                 # where it saves downlinked files
RECEIVED_OK = "File was received OK"    # how Yamcs's failure reason starts when only the FIN's ACK was lost
TRANSFER_SLACK_MS = 5_000           # the transfer list is asked from this long before the command
EVENT_SLACK_MS = 2_000              # and the events
# A poke's three reads (events, command history, transfer list) each give up after this long without a byte, so a
# stalled Yamcs fails the poke, which the sensor logs and repeats, well inside the poke's execution_timeout.
POKE_READ_S = 15
DOWNLINK_EVENTS = tuple("DoomSat.cfdpManager." + n for n in (
    "TxFileQueued", "TxFileTransferStarted", "TxFileTransferCompleted", "TxFileTransferFailed", "TxFileOpenFailed",
    "TxZeroLengthFile", "TxFileSeekFailed", "TxSendMetadataFailed", "TxAckLimitReached", "TxInactivityTimeout",
    "SendFileInitiateFail", "MaxTxTransactionsReached", "InvalidChannel", "InvalidDestinationEid"))
# The F' dispatcher's answer to a command: OK, or the error (VALIDATION_ERROR for a bad channel, EXECUTION_ERROR
# when ten sends are already outstanding, FORMAT_ERROR for a string over 40 characters).
ANSWER_EVENTS = ("CdhCore.cmdDisp.OpCodeCompleted", "CdhCore.cmdDisp.OpCodeError")
UNAVAILABLE = ("TxFileOpenFailed", "TxZeroLengthFile", "SendFileInitiateFail")   # the file was never sent
SOURCE_ARGS = ("sourceFileName", "srcFile", "filename")    # how the cfdpManager events name the file
TRANSACTION_ARGS = ("transactionSeq", "seqNum")             # and the transaction
YAMCS_FAILED = ("NOK", "TIMEOUT", "CANCELLED")              # Yamcs acknowledgements that say it was never sent


def record_name(number: int, last_tic: int) -> str:
    return "%d-%d.json" % (number, last_tic)


def source_path(settings: Settings, name: str) -> str:
    absolute = str(settings.doomsat_home / "run" / "rec" / name)
    if len(absolute) <= MAX_CMD_STRING:
        return absolute
    relative = BIN_TO_HOME + "run/rec/" + name
    if len(relative) <= MAX_CMD_STRING:
        return relative
    raise ValueError("no path to %s fits F' command strings (%d characters)" % (name, MAX_CMD_STRING))


def dest_name(episode_id: str) -> str:
    return "rec_%s.json" % episode_id


def last_tic(l1: dict) -> int | None:
    """The episode's final tic as the ground knows it: the closing event's, else the last TIC sample's."""
    for e in reversed(l1["events"]):
        if e["type"].endswith((".PlayerDied", ".LevelFinished")) and "tic" in e["extra"]:
            return int(e["extra"]["tic"])
    tic = products.column(l1, "TIC")
    return tic[-1][1] if tic else None


def plan(settings: Settings, catalog: Catalog, episode_id: str) -> dict:
    l1row = catalog.current(episode_id, "l1_episode")
    if l1row is None:
        raise LookupError("no L1 for %s" % episode_id)
    l1 = read_json(l1row["path"])
    tic = last_tic(l1)
    if tic is None:
        raise LookupError("%s has no TIC to name its record by" % episode_id)
    name = record_name(l1["episode"]["number"], tic)
    dest = dest_name(episode_id)
    if len(dest) > MAX_CMD_STRING:
        raise ValueError("destination name too long for F': " + dest)
    return {"episode_id": episode_id, "name": name, "source": source_path(settings, name), "dest": dest,
            "l1_product_id": l1row["product_id"]}


def sendfile_args(p: dict) -> dict:
    """All seven SendFile arguments for the plan `p`."""
    return dict(SENDFILE_ARGS, sourceFileName=p["source"], destFileName=p["dest"])


def request(settings: Settings, p: dict) -> dict:
    """Send SendFile through Yamcs. The only command the SDS sends."""
    from yamcs.client import YamcsClient
    args = sendfile_args(p)
    client = YamcsClient(settings.yamcs)
    try:
        cmd = client.get_processor(settings.instance, config.PROCESSOR).issue_command(
            config.SENDFILE, args=args, comment="doomsat-sds: record of episode %s" % p["episode_id"])
        issued = int(cmd.generation_time.timestamp() * 1000)
        return {"command_id": cmd.id, "command": config.SENDFILE, "issued_utc": iso(issued), "issued_ms": issued,
                "args": args}
    finally:
        client.close()


# ------------------------------------------------------------------------------------------- watching the downlink
def _http(http):
    if http is None:
        import requests
        return requests
    return http


def _int(v) -> int | None:
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return None


def _ms(text) -> int | None:
    """Yamcs's ISO 8601 time, as in "2026-10-04T15:59:39.941Z", as epoch milliseconds; None if it is not one."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z$", text or "")
    if not m:
        return None
    whole = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return int(whole.timestamp()) * 1000 + int((m.group(2) or "0")[:3].ljust(3, "0"))


def _event_name(e: dict) -> str:
    return e["type"].rsplit(".", 1)[-1]


def about(events: list[dict], source: str) -> list[dict]:
    """The cfdpManager events about this file: those naming it, and those naming a transaction one of them names
    (TxSendMetadataFailed and the timers give only the transaction). Only meaningful within one request's window:
    transaction numbers start again at 1 with every boot."""
    def names_it(e):
        return any((e.get("extra") or {}).get(k) == source for k in SOURCE_ARGS)

    seqs = {str(e["extra"][k]) for e in events if names_it(e) for k in TRANSACTION_ARGS if k in e["extra"]}
    return [e for e in events if e["type"].startswith("DoomSat.cfdpManager.")
            and (names_it(e) or any(str((e.get("extra") or {}).get(k)) in seqs for k in TRANSACTION_ARGS))]


def command_answer(command_id: str, commands: list[dict], events: list[dict]) -> tuple[str | None, str | None]:
    """(answer, how) for the SendFile with this command id, or (None, None) while there is none yet.

    A Yamcs acknowledgement (Queued, Released, Sent) that failed means the command never left the ground: "NOK".
    Otherwise it is the F' dispatcher's first OpCodeCompleted ("OK") or OpCodeError (its error, as EXECUTION_ERROR)
    for SendFile's opcode in the time-ordered events. Only one record request runs at a time."""
    for c in commands:
        if c.get("id") == command_id:
            for ack, status in sorted((c.get("acks") or {}).items()):
                if status in YAMCS_FAILED:
                    return "NOK", "Yamcs acknowledgement %s %s" % (ack, status)
    for e in events:
        extra = e.get("extra") or {}
        if e["type"] in ANSWER_EVENTS and _int(extra.get("Opcode")) == config.SENDFILE_OPCODE:
            if _event_name(e) == "OpCodeCompleted":
                return "OK", e["message"]
            return extra.get("error") or "ERROR", e["message"]
    return None, None


def find_transfer(transfers: list[dict], source: str, since_ms: int) -> dict | None:
    """The newest download Yamcs lists for `source`, created at or after since_ms (the command's time), or None.

    remotePath is the source path exactly as sent. An older transfer of the same path (an earlier request, or an
    earlier flight) must not stand in for this one, whatever its transaction number."""
    mine = [t for t in transfers if t.get("direction") == "DOWNLOAD" and t.get("remotePath") == source
            and (_ms(t.get("creationTime")) or 0) >= since_ms]
    return max(mine, key=lambda t: (_ms(t.get("creationTime")), _int(t.get("id")) or 0), default=None)


def verdict(source: str, since_ms: int, command_id: str, commands: list[dict], events: list[dict],
            transfers: list[dict]) -> dict:
    """Where the downlink of `source` stands, from what observe() read. Pure.

    state: "received" (the object is in cfdpDown), "failed" (it never will be) or "waiting". finding: what a person
    should hear of, or None: record_unavailable (SendFile was refused, or the file could not be opened or was
    empty), record_transfer_failed (the transfer began and failed), record_fin_unacknowledged (received, but F'
    never acknowledged Yamcs's Finished PDU). A transfer Yamcs lists decides; without one, the events do.
    """
    mine = about(events, source)
    answer, how = command_answer(command_id, commands, events)
    shown = [{"utc": iso(e["t"]), "type": _event_name(e), "message": e["message"]} for e in events
             if e in mine or (e["type"] in ANSWER_EVENTS
                              and _int((e.get("extra") or {}).get("Opcode")) == config.SENDFILE_OPCODE)
             or (e["type"] in DOWNLINK_EVENTS and not any(k in (e.get("extra") or {})
                                                          for k in SOURCE_ARGS + TRANSACTION_ARGS))]
    t = find_transfer(transfers, source, since_ms)
    out = {"state": "waiting", "finding": None, "why": "", "answer": answer, "transfer": t, "events": shown}
    if t is not None:
        state, reason = t.get("state"), t.get("failureReason") or ""
        if state == "COMPLETED":
            out.update(state="received", why="Yamcs transfer %s COMPLETED" % t.get("id"))
        elif state == "FAILED" and reason.startswith(RECEIVED_OK):
            out.update(state="received", finding="record_fin_unacknowledged",
                       why="Yamcs transfer %s FAILED: %s" % (t.get("id"), reason))
        elif state == "FAILED":
            out.update(state="failed", finding="record_transfer_failed",
                       why="Yamcs transfer %s FAILED: %s" % (t.get("id"), reason))
        else:
            out["why"] = "Yamcs transfer %s %s" % (t.get("id"), state)
        return out
    if answer not in (None, "OK"):
        out.update(state="failed", finding="record_unavailable", why="SendFile answered %s: %s" % (answer, how))
        return out
    for names, finding in ((UNAVAILABLE, "record_unavailable"), (("TxFileTransferFailed",), "record_transfer_failed")):
        hit = [e for e in mine if _event_name(e) in names]
        if hit:
            out.update(state="failed", finding=finding, why="%s, and Yamcs lists no transfer" % hit[0]["message"])
            return out
    out["why"] = "SendFile answered %s; Yamcs lists no transfer yet" % (answer or "nothing yet")
    return out


def list_transfers(settings: Settings, since_ms: int, http=None) -> list[dict]:
    """Yamcs's CFDP downloads created since since_ms: a GET, and the only call this module makes to /filetransfer."""
    r = _http(http).get("%s/api/filetransfer/%s/%s/transfers" % (settings.yamcs_url, settings.instance, CFDP_SERVICE),
                        params={"direction": "DOWNLOAD", "start": iso(since_ms)}, timeout=(5, POKE_READ_S))
    r.raise_for_status()
    return r.json().get("transfers", [])


def observe(settings: Settings, archive, p: dict, command: dict, now_ms: int, http=None) -> dict:
    """verdict() on what the archive and Yamcs show now, read only: the command's acknowledgements, the events
    from just before the command to now (F' time can run ahead of the ground's: docs/plans/fprime-yamcs-time.md),
    and the transfer list. Of the
    transfers listed, only one created at or after the command can be this request's."""
    issued = command["issued_ms"]
    events = archive.events(issued - EVENT_SLACK_MS, now_ms + 5_000, types=list(DOWNLINK_EVENTS + ANSWER_EVENTS))
    commands = archive.commands(issued - 1_000, issued + 1_000)
    transfers = list_transfers(settings, issued - TRANSFER_SLACK_MS, http)
    return verdict(p["source"], issued, command["command_id"], commands, events, transfers)


def downlink_finding(seen: dict, received: dict | None) -> str | None:
    """The finding report_downlink_events records once the wait is over, or None. `seen` is observe() after the
    wait, `received` the sensor's answer: its verdict when it saw the record, None when it failed or timed out.

    A verdict that failed is its own finding; a record received with a finding (record_fin_unacknowledged) keeps
    it. A wait that ended without the record is record_not_received, even if the transfer has finished since: the
    run does not ingest it, and its object stays in cfdpDown."""
    if seen["state"] == "failed" or (received and seen["finding"]):
        return seen["finding"]
    if not received:
        return "record_not_received"
    return None


def object_url(settings: Settings, name: str) -> str:
    from urllib.parse import quote
    return "%s/api/storage/buckets/%s/objects/%s" % (settings.yamcs_url, BUCKET, quote(name, safe=""))


def ingest(settings: Settings, catalog: Catalog, p: dict, command: dict, transfer: dict, run_id: str | None = None,
           http=None) -> dict:
    """The transfer's object in cfdpDown, checked to be the payload's record, becomes L0 `l0_record`.

    The object's name comes from the transfer, not the plan: a name already taken gets "(1)" added."""
    from .pipeline import _register
    name = transfer["objectName"]
    r = _http(http).get(object_url(settings, name), timeout=(10, 60))
    r.raise_for_status()
    data = r.content
    size = transfer.get("totalSize")         # an int64, which Yamcs's JSON gives as a string
    if size is not None and len(data) != int(size):
        raise ValueError("%s/%s holds %d bytes; transfer %s sent %s" % (BUCKET, name, len(data), transfer.get("id"), size))
    rec = json.loads(data)
    if rec.get("record", {}).get("format") != "doomsat-episode-record":
        raise ValueError("%s/%s is not an episode record" % (BUCKET, name))
    ver = products.version("l0_record")
    eid = p["episode_id"]
    return _register(settings, catalog, product_type="l0_record", episode_id=eid, product_id=f"{eid}/l0_record@{ver}",
                     path=episode_product_path(settings, eid, "l0_record", ver, "json"), data=data,
                     inputs=[{"source": "cfdp", "bucket": BUCKET, "object": name, "transfer_id": transfer.get("id"),
                              "transaction_id": transfer.get("transactionId"), "transfer_state": transfer.get("state"),
                              "transfer_created": transfer.get("creationTime"), "on_board": p["source"],
                              "command_id": command.get("command_id")}], run_id=run_id)


def forget_downlinked(settings: Settings, transfer: dict, http=None) -> bool:
    """Delete the ingested object from cfdpDown (the product store has it now): that object, no other. True if it
    was there. The bucket holds 1000 objects and 100 MB."""
    r = _http(http).delete(object_url(settings, transfer["objectName"]), timeout=10)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def _check(name: str, record_value, archive_value, agree, note: str = "") -> dict:
    return {"check": name, "record": record_value, "archive": archive_value, "agree": agree, "note": note}


def compare(record: dict, l1: dict, context: dict | None = None) -> dict:
    """Every way the payload's record and the archive's L1 can be held against each other. Pure.

    `agree` is True, False, or None where the two cannot be compared (say, a context the ground never learnt).
    A False is a disagreement worth a person's attention; the counts of statuses sent and received are a
    measurement and only disagree if the ground somehow received more than was sent.
    """
    ep = l1["episode"]
    tic = products.column(l1, "TIC")
    last = lambda name: (products.column(l1, name) or [(None, None)])[-1][1]
    fin = record["final"]
    # A LOAD_WAD that switches the game starts the next episode, and the payload's recorder, which sees only the
    # episode number change, closes the one it cut short as "reset", as it does after a RESET_GAME.
    switched = (record["outcome"], ep["outcome"]) == ("reset", "wad_switch")
    checks = [
        _check("episode number", record["episode"], ep["number"], record["episode"] == ep["number"]),
        _check("outcome", record["outcome"], ep["outcome"], record["outcome"] == ep["outcome"] or switched,
               "the payload writes a WAD switch as a reset" if switched else ""),
        _check("last tic (closing event)", record["last_tic"], last_tic(l1), record["last_tic"] == last_tic(l1)),
        _check("last tic (last TIC sample)", record["last_tic"], last("TIC"), record["last_tic"] == last("TIC"),
               "a mismatch means the final status never reached the archive"),
        _check("kills", fin["kills"], last("KILLS"), fin["kills"] == last("KILLS")),
        _check("cells explored", fin["explored"], last("EXPLORED_CELLS"), fin["explored"] == last("EXPLORED_CELLS")),
        _check("final health", fin["health"], last("HEALTH"), fin["health"] == last("HEALTH")),
        _check("final position", [fin["x"], fin["y"]], [last("POS_X"), last("POS_Y")],
               [fin["x"], fin["y"]] == [last("POS_X"), last("POS_Y")]),
        _check("lowest health", record["health_min"], min((v for _, v in products.column(l1, "HEALTH")), default=None),
               record["health_min"] <= min((v for _, v in products.column(l1, "HEALTH")), default=record["health_min"]),
               "the archive can only miss a low point, never invent one"),
    ]
    first_archived = tic[0][1] if tic else None
    checks.append(_check("first tic", record["first_tic"], first_archived, record["first_tic"] == first_archived,
                         "the archive starts later when statuses were sent before Yamcs was listening"))
    received = len({v for _, v in tic})
    sent = record["statuses_sent"]
    checks.append(_check("statuses sent vs received", sent, received, received <= sent,
                         "%d of %d statuses reached the archive (%.1f%%)" % (received, sent, 100.0 * received / sent)
                         if sent else ""))
    # Statuses go out on a fixed cadence of tics, so the ones sent while the archive was listening can be counted
    # from the record's first and last tic; that separates loss on the link from a late start of the archive.
    cadence = (record["last_tic"] - record["first_tic"]) / (sent - 1) if sent > 1 else None
    listening = None
    if cadence and first_archived is not None:
        listening = int(round((record["last_tic"] - first_archived) / cadence)) + 1
        checks.append(_check("statuses while the archive was listening", listening, received, received <= listening,
                             "%d of about %d statuses sent from tic %d on reached the archive (%.1f%%)" % (
                                 received, listening, first_archived, 100.0 * received / listening)))
    # The record's path, a position per second of game time, against the archived position with the same time tag
    # as that tic. Two statuses can share an F' time tag, and a channel can lose one of the pair on board, so which
    # X, Y and TIC samples came from the same status is not always knowable on the ground. A time tag is compared
    # only when it holds as many TIC samples as X and Y samples (one status, or a pair that arrived whole); a tic
    # at any other time tag is unattributable, which is neither agreement nor disagreement.
    cols = l1["telemetry"]["columns"]
    it, ix, iy = cols.index("TIC"), cols.index("POS_X"), cols.index("POS_Y")
    by_time: dict[int, dict] = {}
    for r in l1["telemetry"]["rows"]:
        slot = by_time.setdefault(r[0], {"tic": [], "x": [], "y": []})
        for key, i in (("tic", it), ("x", ix), ("y", iy)):
            if r[i] is not None:
                slot[key].append(r[i])
    at_tic: dict[int, list] = {}
    unattributable = set()
    for slot in by_time.values():
        whole = len(slot["tic"]) == len(slot["x"]) == len(slot["y"])
        for t in slot["tic"]:
            if whole:
                at_tic.setdefault(t, []).extend(zip(slot["x"], slot["y"]))
            else:
                unattributable.add(t)
    matched = [(t, x, y, at_tic[t]) for t, x, y in record["path"] if at_tic.get(t)]
    off = [min(abs(x - a[0]) + abs(y - a[1]) for a in cands) for _, x, y, cands in matched]
    in_window = sum(1 for t, _, _ in record["path"] if first_archived is not None and t >= first_archived)
    vague = sum(1 for t, _, _ in record["path"] if t in unattributable and not at_tic.get(t))
    checks.append(_check("path points", len(record["path"]), len(matched), all(d == 0 for d in off) if off else None,
                         "%d of %d record positions (%d after the archive began) compared with the archived position "
                         "at the same tic, largest difference %s units; %d unattributable (their time tag lost a "
                         "position on board)" % (len(matched), len(record["path"]), in_window,
                                                  max(off) if off else None, vague)))
    ctx = context or {}
    for key in ("wad", "map", "skill", "seed"):
        mine = ctx.get(key)
        theirs = record["context"].get(key)
        note = "the ground never learnt it; the record supplies it" if mine is None else ""
        if key == "wad" and mine is not None:
            # On main the ground's WAD is the one flown, from telemetry, so it agrees after a LOAD_WAD; the
            # payload's --wad stands in only on a flight build without WAD telemetry.
            telemetry = (ctx.get("sources") or {}).get("wad", "").startswith("telemetry")
            note = "the WAD flown, from telemetry" if telemetry else "the payload's --wad"
        checks.append(_check("context " + key, theirs, mine, None if mine is None else theirs == mine, note))
    # No patch WAD is None on both sides, so the patch WAD is compared only when both carry the key (a record or a
    # context from before main has none) and the ground learnt the WAD at all.
    theirs = record["context"].get("pwad")
    if "pwad" not in record["context"] or "pwad" not in ctx:
        checks.append(_check("context pwad", theirs, ctx.get("pwad"), None,
                             "the %s predates the patch WAD" % ("record" if "pwad" not in record["context"]
                                                                else "ground's context")))
    elif ctx.get("wad") is None:
        checks.append(_check("context pwad", theirs, None, None, "the ground never learnt the WAD"))
    else:
        checks.append(_check("context pwad", theirs, ctx["pwad"], theirs == ctx["pwad"]))
    disagreements = [c for c in checks if c["agree"] is False]
    return {"product": {"type": "qa_record_check", "level": "QA", "version": products.version("qa_record_check")},
            "episode_id": ep["id"], "agree": not disagreements, "disagreements": len(disagreements),
            "statuses": {"sent": sent, "received": received,
                         "delivery": round(received / sent, 4) if sent else None,
                         "sent_while_archiving": listening,
                         "delivery_while_archiving": round(received / listening, 4) if listening else None},
            "checks": checks}


def compare_and_register(settings: Settings, catalog: Catalog, p: dict, l0_ref: dict, run_id: str | None = None) -> dict:
    from .pipeline import _register
    from .store import canonical_json
    record = read_json(l0_ref["path"])
    l1row = catalog.current(p["episode_id"], "l1_episode")
    l1 = read_json(l1row["path"])
    ep = catalog.episode(p["episode_id"])
    result = compare(record, l1, ep["context"] if ep else None)
    ver = products.version("qa_record_check")
    eid = p["episode_id"]
    ref = _register(settings, catalog, product_type="qa_record_check", episode_id=eid,
                    product_id=f"{eid}/qa_record_check@{ver}",
                    path=episode_product_path(settings, eid, "qa_record_check", ver, "json"),
                    data=canonical_json(result), run_id=run_id,
                    inputs=[{"product_id": l0_ref["product_id"], "sha256": l0_ref["sha256"]},
                            {"product_id": l1row["product_id"], "sha256": l1row["sha256"]}])
    if ref["status"] != "unchanged":
        for c in result["checks"]:
            if c["agree"] is False:
                catalog.add_finding("record_disagrees", c, episode_id=eid, product_id=ref["product_id"])
    ref.update(agree=result["agree"], disagreements=result["disagreements"], statuses=result["statuses"])
    return ref
