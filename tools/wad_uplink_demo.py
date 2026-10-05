# /// script
# requires-python = ">=3.10"
# dependencies = ["yamcs-client==2.1.0"]
# ///
"""Send a level to the running spacecraft and switch the game to it, printing the evidence as it goes.

    ground/.venv/bin/python tools/wad_uplink_demo.py --wad PATH [--iwad NAME] --map MAP
    uv run tools/wad_uplink_demo.py ...                      (the same, with the PEP 723 header above)

1. The file goes into a Yamcs bucket, then up the command link through the F' file transfer service
   (fprime-yamcs's FprimeFilePacketService: Fw::FilePacket Start, Data, End on APID 3) to
   <remote-dir>/NAME.<nonce>.part on the spacecraft.
2. FileUplink checks the checksum (FileReceived); the Doom component then renames it to NAME (WadUplinked).
   Only that event says the file is there whole: the service calls an upload complete once it has SENT it.
   On the CFDP build (docs/plans/cfdp-stage2-spike.md: Yamcs runs a cfdp service, the flight software
   cfdpManager) the file goes up as CCSDS CFDP class 2 instead (--cfdp 1 or 2 to choose), and COMMIT_WAD does
   the rename: in class 2 once the spacecraft's FIN says the file is whole, in class 1 once
   RxFileTransferCompleted comes with no RxCrcMismatch (class 1 has no retransmission: not for WADs).
   COMMIT_WAD carries the size and CFDP checksum of what was sent, and the Doom component renames the file
   only if it has both (WadCommitRefused otherwise). --checksum FILE prints the two, for a commit by hand.
3. LOAD_WAD; the payload proves the game starts on it in a child process, then switches (WadLoaded) or keeps
   flying what it had (WadLoadFailed). The tool shows WAD_IWAD / WAD_PWAD / WAD_LOADS, EPISODE and
   FRAMES_SENT, and saves the first whole frame from after the switch in out/.

What is loaded: with --iwad, the uplinked file is the PWAD over that IWAD; without, it is the IWAD. With no
--wad, nothing is uplinked and LOAD_WAD names files already on board (--iwad, --pwad).

Negative cases: --truncate N sends only the first N bytes (the game must refuse it); --load-early issues
LOAD_WAD while the file is still arriving (it must be refused, and the uplink still completes);
--expect-fail makes a refusal the success. --latency N measures CONTROL round trips (command up, the onboard
CMDS_RECEIVED count back down) N times before and N times during the uplink: run it without a pilot, whose
own commands would be counted too.

Uplinked-WAD flights are demonstrations. They are never benched or graded: the dev set is Freedoom Phase 1
and the test set the shareware episode (charter 2.5), and this tool is not part of either.
"""
import argparse
import array
import base64
import os
import posixpath
import statistics
import struct
import sys
import threading
import time
from pathlib import Path

from yamcs.client import YamcsClient
from yamcs.client.core.exceptions import YamcsError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "payload"))
import wad_uplink as wu  # noqa: E402  the payload's own name rules, so a bad name fails before the uplink
# Yamcs 5.12.8 caps a bucket upload over HTTP at the larger of the server's maxContentLength and
# UploadObject's own 5 MiB (buckets.proto). ground/yamcs/etc/yamcs.yaml raises the first to 32 MiB, room for a
# whole IWAD; the multipart wrapping takes a little of it.
BUCKET_UPLOAD_MAX = 32 * 1024 * 1024 - 64 * 1024
DOOM = "/DoomSat_DoomSat/DoomSat/doom"
CHUNK_HEADER = struct.Struct("!IHHH")   # seq, index, count, length: the FrameChunk header (Doom.fpp)
# How long to wait for the answer to a command. With --tries above 1 (a lossy link) a command whose answer has
# not come is sent again; a lost answer can then be stood in for by telemetry that is sent every second.
COMMIT_ANSWER_S = 8.0
LOAD_ANSWER_S = 40.0
LOAD_RETRY_ANSWER_S = 25.0
TLM_STANDIN_S = 3.0
# An uplink is given up when neither its state nor its byte count has moved for this long. No budget from the size:
# the pace (--pdu-delay, or sleepBetweenPdus in yamcs.yaml) decides how long it takes. Yamcs counts only first-pass
# bytes, so a CFDP upload at full size waiting on NAK resends and the FIN has its own, longer limit (tail_limit).
STALL_S = 90.0
# How long an upload may wait QUEUED for Yamcs's one upload slot (maxNumPendingUploads: 1) behind another one
QUEUED_S = 900.0
# The CFDP pace when --pdu-delay is not given (sleepBetweenPdus in ground/yamcs/etc/yamcs.fprime-project.yaml), and
# file data per PDU (its maxPduSize 1009 less a 12-byte header and a 4-byte offset)
DEFAULT_PDU_DELAY_MS = 40
CFDP_DATA_PER_PDU = 993
# Below this pace uplinked PDUs can pile up in commsBufferManager faster than cfdpManager takes them, and an empty
# pool stops the downlink (docs/plans/cfdp-stage2-spike.md, risk 4).
MIN_PDU_DELAY_MS = 5
WATCHED = ["WAD_IWAD", "WAD_PWAD", "WAD_LOADS", "EPISODE", "FRAMES_SENT", "CMDS_RECEIVED", "PAYLOAD_LINK",
           "FRAME_CHUNK"]


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def part_name(name):
    """NAME.<milliseconds>.part: a fresh name for every uplink, which COMMIT_WAD (or FileUplink) renames to NAME."""
    return f"{name}.{time.time_ns() // 1000000}.part"


def tail_limit(size, pdu_delay_ms):
    """Seconds a CFDP upload may sit at full size before its FIN. Allows NAKs for a tenth of the PDUs, 58 to a NAK
    (CfdpCfg NakMaxSegments), each round about 58 PDUs at the pace plus cfdpManager's 2 s ack timer on a 1 Hz tick,
    and a CRC pass at the stock 64 KiB a tick (PrmDb.json sets 16 MiB; this covers flying on the defaults)."""
    pdus = -(-size // CFDP_DATA_PER_PDU) + 2
    rounds = -(-pdus // 580)
    return STALL_S + rounds * (58 * pdu_delay_ms / 1000 + 4) + size / 65536


def cfdp_checksum(data):
    """The CFDP modular checksum (CCSDS 727.0-B-5 4.2.2; F' CFDP::Checksum, the one a CFDP EOF carries): the file as
    big-endian 32-bit words from its start, the last one padded with zeros, summed modulo 2**32."""
    words = array.array("I")
    assert words.itemsize == 4
    words.frombytes(bytes(data) + b"\0" * (-len(data) % 4))
    if sys.byteorder == "little":
        words.byteswap()
    return sum(words) & 0xFFFFFFFF


def wad_name(value):
    """WAD_IWAD / WAD_PWAD: zero-padded ASCII bytes (F' string telemetry does not survive fprime-xtce)."""
    if isinstance(value, str):
        value = base64.b64decode(value)
    return bytes(value or b"").rstrip(b"\0").decode("ascii", "replace")


class Link:
    """Events and the watched channels, live, from one Yamcs connection."""

    def __init__(self, url, instance):
        self.client = YamcsClient(url)
        self.instance = instance
        self.processor = self.client.get_processor(instance, "realtime")
        self.lock = threading.Condition()
        self.events = []                # (arrival time, message)
        self.values = {}                # channel -> (arrival time, value)
        self.counts = {}                # channel -> updates seen
        self.partial, self.frames = {}, []   # frame reassembly: seq -> parts; whole frames (time, seq, jpeg)
        self.client.create_event_subscription(instance, on_data=self._on_event)
        self.processor.create_parameter_subscription([f"{DOOM}/{c}" for c in WATCHED], on_data=self._on_data)

    def _on_event(self, event):
        with self.lock:
            self.events.append((time.time(), event.message or ""))
            self.lock.notify_all()

    def _on_data(self, data):
        with self.lock:
            for pv in data.parameters:
                name = pv.name.rsplit("/", 1)[-1]
                if name == "FRAME_CHUNK":
                    self._chunk(pv.eng_value)
                    continue
                v = pv.eng_value
                if name in ("WAD_IWAD", "WAD_PWAD"):
                    v = wad_name(v)
                self.values[name] = (time.time(), v)
                self.counts[name] = self.counts.get(name, 0) + 1
            self.lock.notify_all()

    def _chunk(self, value):
        try:
            d = dict(value)
            data = d["data"] if isinstance(d["data"], (bytes, bytearray)) else base64.b64decode(d["data"])
            seq, index, count, length = int(d["seq"]), int(d["index"]), int(d["count"]), int(d["length"])
        except (KeyError, TypeError, ValueError):
            return
        if seq & 0x80000000:
            return   # the payload's own map, not a frame
        frame = self.partial.setdefault(seq, {})
        frame[index] = bytes(data[:length])
        if len(frame) == count:
            self.frames.append((time.time(), seq, b"".join(frame[i] for i in range(count))))
            del self.partial[seq]
            for old in [s for s in self.partial if s < seq - 50]:
                del self.partial[old]

    def value(self, name):
        with self.lock:
            return self.values.get(name, (0, None))[1]

    def wait_event(self, since, *needles, timeout=60.0):
        """The first event after `since` whose message contains any needle; None on timeout."""
        end = time.time() + timeout
        with self.lock:
            while True:
                for t, msg in self.events:
                    if t >= since and any(n in msg for n in needles):
                        return msg
                left = end - time.time()
                if left <= 0:
                    return None
                self.lock.wait(min(left, 0.5))

    def wait(self, predicate, timeout):
        end = time.time() + timeout
        with self.lock:
            while not predicate():
                left = end - time.time()
                if left <= 0:
                    return False
                self.lock.wait(min(left, 0.5))
            return True

    def command(self, name, **args):
        return self.processor.issue_command(f"{DOOM}/{name}", args=args)


def control_round_trip(link, timeout=5.0):
    """Seconds from issuing CONTROL to the onboard command count arriving back on the ground one higher."""
    with link.lock:
        before = link.values.get("CMDS_RECEIVED", (0, None))[1]
    if before is None:
        return None
    t0 = time.time()
    link.command("CONTROL", move=0, strafe=0, turn=0.0, fire=False, use=False, weapon="FIST")
    ok = link.wait(lambda: (link.values.get("CMDS_RECEIVED", (0, -1))[1] or 0) > before, timeout)
    return time.time() - t0 if ok else None


def settle(link, quiet=5.0, limit=30.0):
    """Wait until CMDS_RECEIVED has not moved for `quiet` seconds: after a CONTROL timed out, its late count
    (if it ever comes) must land before the next sample reads its starting count, not after."""
    end, last, since = time.time() + limit, link.value("CMDS_RECEIVED"), time.time()
    while time.time() < end:
        time.sleep(0.2)
        now = link.value("CMDS_RECEIVED")
        if now != last:
            last, since = now, time.time()
        elif time.time() - since >= quiet:
            return


def latency_summary(label, samples):
    got = [s for s in samples if s is not None]
    if not got:
        return f"{label}: no answers in {len(samples)} tries"
    got.sort()
    return (f"{label}: median {statistics.median(got) * 1000:.0f} ms, min {got[0] * 1000:.0f}, "
            f"max {got[-1] * 1000:.0f} ms over {len(got)}/{len(samples)} answered")


def measure_latency(link, n, label, gap=0.5, stop=None):
    samples = []
    for _ in range(n):
        if stop is not None and stop():
            break
        rtt = control_round_trip(link)
        samples.append(rtt)
        if rtt is None:
            settle(link)
        time.sleep(gap)
    say(latency_summary(label, samples))
    return samples


def flight_home():
    """Where the flight side is installed, found the way scripts/common.sh finds it: the environment, then the
    repo's .env, then ~/doom. On a Windows ground with the flight side in WSL, give --remote-dir instead."""
    if os.environ.get("DOOMSAT_HOME"):
        return os.environ["DOOMSAT_HOME"]
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "DOOMSAT_HOME" and v.strip().strip("'\""):
                return v.strip().strip("'\"")
    return os.path.expanduser("~/doom")


def uplink(a, link, bucket, part, remote, name, content):
    """Send the bucket object `part` to `remote` and see it put in place on board. (exit code or 0, unconfirmed)."""
    service = link.client.get_file_transfer_client(a.instance).get_service(a.service or ("cfdp" if a.cfdp else
                                                                                        "FprimeFilePacketService"))
    updates = service.create_transfer_subscription()
    t0 = time.time()
    if a.cfdp:
        options = {"reliable": a.cfdp == 2}
        if a.pdu_delay is not None:
            options["pduDelay"] = a.pdu_delay
        transfer = service.upload(a.bucket, part, remote, source_entity="ground", destination_entity="doomsat",
                                  options=options)
    else:
        transfer = service.upload(a.bucket, part, remote)
    say(f"uplink started: {len(content)} bytes -> {remote} (transfer {transfer.id})")

    def refresh():   # a Transfer is a snapshot: the subscription has the live one
        return updates.get_transfer(transfer.id) or transfer

    try:
        if a.load_early and not a.no_load:
            for _ in range(60):
                transfer = refresh()
                if transfer.transferred_size > 0 or transfer.is_complete():
                    break
                time.sleep(0.5)
            iwad, pwad = (a.iwad, name) if a.iwad and a.iwad != name else (name, "")
            say(f"LOAD_WAD {iwad} {pwad!r} {a.map} while the uplink is at {transfer.transferred_size}/{len(content)} bytes")
            t_cmd = time.time()
            link.command("LOAD_WAD", iwad=iwad, pwad=pwad, map=a.map)
            said = link.wait_event(t_cmd, "[WadLoaded]", "[WadLoadFailed]", timeout=LOAD_ANSWER_S)
            say(f"event: {said}")
            if not said or "[WadLoadFailed]" not in said:
                say("FAIL: a load during the uplink was not refused")
                return 1, False

        stop = threading.Event()
        if a.latency:
            th = threading.Thread(target=lambda: measure_latency(link, a.latency, "CONTROL round trip, during the uplink",
                                                                 gap=a.latency_gap, stop=stop.is_set), daemon=True)
            th.start()
        tail_s = tail_limit(len(content), a.pdu_delay or DEFAULT_PDU_DELAY_MS) if a.cfdp else STALL_S
        last_say, seen, moved, limit = 0, None, time.time(), STALL_S
        while not transfer.is_complete():
            now = (transfer.state, transfer.transferred_size)
            if transfer.state == "QUEUED":
                limit = QUEUED_S   # behind another upload in Yamcs's one slot: not a stall
            else:
                limit = tail_s if transfer.transferred_size >= len(content) else STALL_S
            if now != seen:
                seen, moved = now, time.time()
                if transfer.state == "QUEUED":
                    say("  queued: another upload holds Yamcs's one upload slot (maxNumPendingUploads)")
            elif time.time() - moved > limit:
                break
            time.sleep(1)
            transfer = refresh()
            if time.time() - last_say > 15:
                last_say = time.time()
                say(f"  uplink {transfer.state}: {transfer.transferred_size}/{len(content)} bytes, "
                    f"{transfer.transferred_size / max(time.time() - t0, 1e-3) / 1000:.1f} KB/s")
        stop.set()
        if a.latency:
            th.join(timeout=30)
        if not transfer.is_complete():
            say(f"FAIL: transfer {transfer.state} at {transfer.transferred_size}/{len(content)} bytes, no progress for "
                f"{limit:.0f} s")
            return 1, False
    finally:
        # However this ends (a stall, a failed check, an exception, Ctrl-C), a transfer left running would hold
        # Yamcs's one upload slot and the next run would queue behind it
        transfer = refresh()
        if not transfer.is_complete():
            try:
                service.cancel_transfer(transfer.id)
                say("cancelled the transfer")
            except Exception as e:   # noqa: BLE001  never mask what brought us here
                say(f"could not cancel the transfer ({e}); it may still complete")
    if not transfer.is_success():
        say(f"FAIL: transfer {transfer.state} {transfer.error or ''}")
        return 1, False
    sent = time.time() - t0
    if a.cfdp == 2:
        say(f"CFDP class 2 transfer finished (FIN) in {sent:.1f} s ({len(content) / sent / 1000:.1f} KB/s): "
            "the spacecraft has the whole file")
    else:
        say(f"all packets sent in {sent:.1f} s ({len(content) / sent / 1000:.1f} KB/s); waiting for the spacecraft")
    placed = f"[WadUplinked] Uplinked WAD ready to load: {a.remote_dir.rstrip('/')}/{name}"
    if not a.cfdp:
        said = link.wait_event(t0, placed, "[BadChecksum]", "[WadUplinkFailed]", "[FileWriteError]", "[FileOpenError]",
                               timeout=30)
        say(f"event: {link.wait_event(t0, '[FileReceived]', timeout=1)}")
        say(f"event: {said}")
        if said and "[BadChecksum]" in said:
            say("FAIL: the file arrived damaged. F' file packets are not retransmitted, so one packet lost on the "
                "command link fails the whole file; it stays a .part and cannot be loaded. Uplink it again")
            return 1, False
        if not said or "[WadUplinked]" not in said:
            say("FAIL: the spacecraft did not confirm the file")
            return 1, False
        return 0, False

    # cfdpManager writes the file in place and has no fileAnnounce: commit it once it is known whole.
    # Class 1 has no FIN, and F' v4.3.0 reports a class 1 file whose CRC failed as completed anyway.
    if a.cfdp == 1:
        done = link.wait_event(t0, "[RxFileTransferCompleted]", "[RxFileTransferFailed]", "[RxCrcMismatch]",
                               timeout=30)
        say(f"event: {done}")
        if not done or "[RxFileTransferCompleted]" not in done or link.wait_event(t0, "[RxCrcMismatch]", timeout=1):
            say("FAIL: the class 1 file did not arrive whole (no retransmission in class 1); not committing it")
            return 1, False
    # COMMIT_WAD carries what was sent; the Doom component renames the .part only if the file on board has the same
    # size and CFDP checksum (WadCommitRefused otherwise). That, not the events above, is what keeps a damaged or
    # unfinished file out of place.
    # A command is one unprotected frame (no COP-1 here): on a lossy link, ask again if there is no answer.
    # A second COMMIT_WAD after a first that worked finds no .part and says WadUplinkFailed, harmlessly.
    size, checksum = len(content), cfdp_checksum(content)
    said, commits, t_first = None, 0, time.time()
    for _ in range(a.tries):
        t_commit = time.time()
        link.command("COMMIT_WAD", part=part, fileSize=size, checksum=checksum)
        commits += 1
        said = link.wait_event(t_commit, placed, "[WadUplinkFailed]", "[WadCommitRefused]", timeout=COMMIT_ANSWER_S)
        if said:
            break
    said = said or link.wait_event(t_first, placed, "[WadUplinkFailed]", "[WadCommitRefused]", timeout=2)
    say(f"event: {said}")
    if said and "[WadCommitRefused]" in said:
        say("FAIL: the file on board is not the file sent (its size or checksum differs); it stays a .part")
        return 1, False
    if said is None:
        # Nothing came down: the command may never have arrived, and LOAD_WAD would then fly (or refuse) any older
        # file of this name already in place, not this upload
        say(f"FAIL: no answer to {commits} COMMIT_WAD; the file may still be {part}, and LOAD_WAD would use any older "
            f"{name} on board in its place. Commit it by hand (--checksum FILE) or run again with more --tries")
        return 1, False
    if commits > 1 and "[WadUplinkFailed]" in said:
        # A retry finding no .part: an earlier COMMIT_WAD, whose answer was lost, most likely moved it
        if a.no_load:
            say(f"FAIL: COMMIT_WAD not confirmed ({said} after {commits}); LOAD_WAD would tell whether the file is "
                "in place")
            return 1, False
        say(f"{said} (after {commits} COMMIT_WAD; an earlier one's answer lost on the way down?); LOAD_WAD will "
            "say whether the file is there")
        return 0, True
    if "[WadUplinked]" not in said:
        say("FAIL: the spacecraft did not put the file in place")
        return 1, False
    return 0, False


def main():
    home = flight_home()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wad", help="local WAD file to uplink (none: load files already on board)")
    p.add_argument("--as", dest="name", help="its name on the spacecraft (default: its own file name)")
    p.add_argument("--iwad", help="the IWAD to load; with --wad, the uplinked file becomes the PWAD over it")
    p.add_argument("--pwad", default="", help="with no --wad: a PWAD already on board")
    p.add_argument("--map", help="the map to start on (required unless --no-load)")
    p.add_argument("--no-load", action="store_true", help="uplink only")
    p.add_argument("--truncate", type=int, help="uplink only the first N bytes (a negative case)")
    p.add_argument("--load-early", action="store_true", help="issue LOAD_WAD while the file is still arriving")
    p.add_argument("--expect-fail", action="store_true", help="succeed only if LOAD_WAD is refused")
    p.add_argument("--latency", type=int, default=0, help="CONTROL round trips to time before and during the uplink")
    p.add_argument("--tries", type=int, default=1,
                   help="send COMMIT_WAD and LOAD_WAD up to this many times while they get no answer (a lossy link)")
    p.add_argument("--pdu-delay", type=int, help=f"with --cfdp: milliseconds between PDUs (Yamcs's pduDelay option; "
                                                 f"at least {MIN_PDU_DELAY_MS})")
    p.add_argument("--latency-gap", type=float, default=0.5,
                   help="seconds between round trips during the uplink (spread them over it; default %(default)s)")
    p.add_argument("--remote-dir", default=posixpath.join(home, "wads", "uplink"),
                   help="the uplink directory on the spacecraft, as an absolute path (default %(default)s)")
    p.add_argument("--yamcs", default="localhost:8090")
    p.add_argument("--instance", default="fprime-project")
    p.add_argument("--service", help="Yamcs file transfer service (default: cfdp if Yamcs runs it, else "
                                     "FprimeFilePacketService)")
    p.add_argument("--cfdp", type=int, choices=(1, 2),
                   help="send the file with CCSDS CFDP class 1 or 2 (the CFDP build: the flight software runs "
                        "cfdpManager), then COMMIT_WAD it into place once the transfer is known whole. Default: class 2 "
                        "when Yamcs runs a cfdp service. Class 1 has no retransmission: for trying it, not for WADs")
    p.add_argument("--bucket", default="wadUplink")
    p.add_argument("--out", default=str(ROOT / "out"), help="where the frame from after the switch goes")
    p.add_argument("--checksum", metavar="FILE", help="print the fileSize and checksum COMMIT_WAD needs for FILE (a "
                                                      "commit by hand, in the Yamcs web UI), and nothing else")
    a = p.parse_args()
    if a.checksum:
        data = Path(a.checksum).read_bytes()
        print(f"fileSize {len(data)} checksum {cfdp_checksum(data)}")
        return 0
    if a.tries < 1:
        p.error("--tries must be at least 1")
    if a.pdu_delay is not None and a.pdu_delay < MIN_PDU_DELAY_MS:
        p.error(f"--pdu-delay below {MIN_PDU_DELAY_MS} ms can drain commsBufferManager and stop the downlink "
                "(docs/plans/cfdp-stage2-spike.md, risk 4)")
    if a.cfdp and a.service and a.service != "cfdp":
        p.error(f"--cfdp needs the cfdp service, not {a.service}")
    if not a.no_load and not a.map:
        p.error("--map is required (or --no-load)")
    if not a.wad and not a.iwad:
        p.error("give --wad (to uplink) and/or --iwad (to load)")
    if a.wad and a.truncate is not None and not a.name:
        a.name = "trunc-" + os.path.basename(a.wad)   # never under the real name, where it would shadow the good copy
    name = (a.name or os.path.basename(a.wad)) if a.wad else None
    # The payload's own rules, before anything goes up (an uplink can take minutes); --expect-fail is for
    # sending bad names on purpose, so the payload can be seen refusing them
    if not a.expect_fail:
        loading = (a.iwad, name) if a.wad and a.iwad and a.iwad != name else ((name, "") if a.wad else (a.iwad, a.pwad))
        problem = ((name and wu.name_problem(name, "uplinked file")) or (a.iwad and wu.name_problem(a.iwad, "IWAD"))
                   or (loading[1] and wu.name_problem(loading[1], "PWAD")) or (not a.no_load and wu.map_problem(a.map)))
        if problem:
            p.error(problem)

    def check_cfdp_name():
        # With CFDP, COMMIT_WAD names the .part on board, in a command string of at most 38 characters (FPP size 40
        # less Yamcs's 2-byte length): the name part_name() makes
        if a.cfdp and name and len(part_name(name)) > wu.NAME_MAX:
            p.error(f"with CFDP the uplinked file name may be at most {wu.NAME_MAX - len(part_name(''))} characters "
                    f"(COMMIT_WAD carries NAME.<ms>.part in {wu.NAME_MAX}); --as a shorter one")
    check_cfdp_name()

    link = Link(a.yamcs.replace("http://", ""), a.instance)
    if not link.wait(lambda: "CMDS_RECEIVED" in link.values and "WAD_IWAD" in link.values, 15):
        say("no telemetry from the spacecraft within 15 s: is the flight side up (scripts/flight.sh status)?")
        return 2
    if a.wad and a.cfdp is None:
        # The build decides: the CFDP build's Yamcs runs a cfdp service, the native one FprimeFilePacketService.
        # --service cfdp says the same, and gets class 2 and COMMIT_WAD with it.
        if a.service:
            use_cfdp = a.service == "cfdp"
        else:
            use_cfdp = "cfdp" in [s.name for s in link.client.get_file_transfer_client(a.instance).list_services()]
        if use_cfdp:
            a.cfdp = 2
            say("Yamcs runs CFDP (the CFDP build): the file goes up as CFDP class 2")
            check_cfdp_name()
    say(f"on board now: {link.value('WAD_IWAD')!r} {link.value('WAD_PWAD')!r}, loads {link.value('WAD_LOADS')}, "
        f"episode {link.value('EPISODE')}, frames sent {link.value('FRAMES_SENT')}")

    if a.latency:
        measure_latency(link, a.latency, "CONTROL round trip, link idle")

    unconfirmed = False   # with CFDP: a retried COMMIT_WAD found the .part gone; LOAD_WAD tells whether it is in place
    if a.wad:
        content = Path(a.wad).read_bytes()          # ground tooling: the bytes go into the bucket, nothing more
        if a.truncate is not None:
            content = content[:a.truncate]
        if len(content) > BUCKET_UPLOAD_MAX:
            say(f"{a.wad} is {len(content)} bytes; Yamcs takes at most {BUCKET_UPLOAD_MAX} in one bucket upload "
                "over HTTP (maxContentLength in ground/yamcs/etc/yamcs.yaml), so it cannot be uplinked this way")
            return 2
        part = part_name(name)
        remote = a.remote_dir.rstrip("/") + "/" + part
        storage = link.client.get_storage_client()
        if a.bucket not in [b.name for b in storage.list_buckets()]:
            storage.create_bucket(a.bucket)
        bucket = storage.get_bucket(a.bucket)
        try:
            bucket.upload_object(part, content, content_type="application/octet-stream")
        except YamcsError as e:
            say(f"Yamcs would not take {len(content)} bytes into bucket {a.bucket}: {e}. A bucket upload is capped at "
                "maxContentLength (ground/yamcs/etc/yamcs.yaml) and the bucket at 100 MiB / 1000 objects")
            return 2
        try:
            rc, unconfirmed = uplink(a, link, bucket, part, remote, name, content)
        finally:
            try:
                bucket.delete_object(part)   # however it went: a bucket holds 1000 objects and 100 MiB. Yamcs read
            except YamcsError:               # the whole object when the transfer started, so this never cuts one short
                pass
        if rc:
            return rc

    if a.no_load:
        return 0
    if a.wad:
        iwad, pwad = (a.iwad, name) if a.iwad and a.iwad != name else (name, "")
    else:
        iwad, pwad = a.iwad, a.pwad
    episode0, frames0, loads0 = link.value("EPISODE"), link.value("FRAMES_SENT"), link.value("WAD_LOADS")
    say(f"LOAD_WAD iwad={iwad} pwad={pwad!r} map={a.map}")

    def this_load_shows():
        # The WAD channels show this load: a count above the one before, and the names asked for
        return ((link.value("WAD_LOADS") or 0) > loads0 and link.value("WAD_IWAD") == iwad
                and link.value("WAD_PWAD") == (pwad or ""))

    t_cmd = time.time()
    said = None
    for attempt in range(a.tries):
        link.command("LOAD_WAD", iwad=iwad, pwad=pwad, map=a.map)
        said = link.wait_event(t_cmd, "[WadLoaded]", "[WadLoadFailed]",
                               timeout=LOAD_ANSWER_S if a.tries == 1 else LOAD_RETRY_ANSWER_S)
        # The answer is one event frame; on a lossy link the WAD channels (sent every second) can stand for it,
        # but only against a count known from before the command
        if said is None and loads0 is not None and link.wait(this_load_shows, TLM_STANDIN_S):
            said = f"[WadLoaded] (event not seen; WAD_LOADS {loads0} -> {link.value('WAD_LOADS')})"
        if said is not None:
            break
        say(f"no answer to LOAD_WAD (attempt {attempt + 1}); sending it again")
    say(f"event: {said}")
    if said is None:
        say("FAIL: no WadLoaded or WadLoadFailed")
        return 1
    failed = "[WadLoadFailed]" in said
    # The old game flies on while the new WAD is proven, and the event can overtake that game's last frame
    # or two on the way down (events and frames queue separately): skip those.
    with link.lock:
        seq0 = max([s for _, s, _ in link.frames], default=-1) + 2
    time.sleep(3)   # the once-a-second WAD channels, and some frames on the new game
    say(f"telemetry: WAD_IWAD={link.value('WAD_IWAD')!r} WAD_PWAD={link.value('WAD_PWAD')!r} "
        f"WAD_LOADS {loads0} -> {link.value('WAD_LOADS')}, EPISODE {episode0} -> {link.value('EPISODE')}, "
        f"FRAMES_SENT {frames0} -> {link.value('FRAMES_SENT')}")
    if failed:
        if a.expect_fail:
            if unconfirmed and any(why in said for why in (wu.NOT_FINISHED, wu.NOT_FOUND)):
                say("FAIL: refused because the file was never put in place (COMMIT_WAD did not run on board), so the "
                    "uplinked file itself was not tested")
                return 1
            say("OK: refused, as expected; the game carries on with what it had")
            return 0
        say("FAIL: LOAD_WAD was refused")
        return 1
    if a.expect_fail:
        say("FAIL: LOAD_WAD was expected to be refused")
        return 1
    if (link.value("WAD_IWAD"), link.value("WAD_PWAD")) != (iwad, pwad or ""):
        say(f"FAIL: WadLoaded, but the WAD channels name {link.value('WAD_IWAD')!r} {link.value('WAD_PWAD')!r}, "
            f"not {iwad!r} {pwad!r}: that answer was not for this load")
        return 1
    if not link.wait(lambda: any(s > seq0 for _, s, _ in link.frames), 10):
        say("FAIL: no whole frame from after the switch within 10 s")
        return 1
    with link.lock:
        t, seq, jpeg = next((t, s, j) for t, s, j in link.frames if s > seq0)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"wad_uplink_{(pwad or iwad).replace('.wad', '')}_{a.map}.jpg"
    path.write_bytes(jpeg)
    say(f"frame {seq} from after the switch: {path} ({len(jpeg)} bytes)")
    say("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
