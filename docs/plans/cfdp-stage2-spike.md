# Task: CFDP for DoomSat file transfer, Stage 2 (time-boxed spike)

Stage 1 gave DoomSat in-flight WAD uplink over F Prime's native file packets, which have no retransmission. This stage replaces that transfer with CCSDS CFDP so a file survives a lossy link. It is a spike: the outcome is either a working Class 2 uplink or a clear findings report. Both are acceptable.

Read `CLAUDE.md` and `docs/plans/wad-uplink-stage1.md` first. Branch from `feature/wad-uplink` as `feature/cfdp-spike`. Save this message as `docs/plans/cfdp-stage2-spike.md`, commit it, and keep a findings section in it up to date.

## What I found by reading (not by running)

Checked on 3 October 2026 against the versions DoomSat pins. Confirm each before relying on it.

- **F Prime v4.3.0 ships a CFDP engine.** `Svc/Ccsds/CfdpManager` implements Class 1 and Class 2. Per channel it has `dataIn` and `dataInReturn` for incoming PDUs, `dataOut` and `dataReturnIn` for outgoing PDUs, `bufferAllocate` and `bufferDeallocate`, a `run1Hz` port that drives transmit and timers, a `SendFile` command, and a `fileIn` port of type `Svc.SendFileRequest` so another component can ask for a downlink. Compile-time settings are in `default/config/CfdpCfg.fpp`.
- **There is a ready subtopology.** `Svc/Subtopologies/FileHandlingCfdp` contains `cfdpManager`, `fileManager`, and `prmDb`. It replaces `FileHandling`; both define `fileManager` and `prmDb`, so a topology imports one or the other.
- **The com side is not pre-wired.** At v4.3.0 the `ComCcsds` subtopology's buffer queue enum has only `FILE`. The `FileHandlingCfdp` design doc shows an example using dedicated CFDP ports on `ComCcsds` that I did not find at that tag, and I found no reference deployment that wires `cfdpManager`. Check whether a newer F Prime tag or `devel` has one to copy from. Upgrading F Prime is out of scope unless it is trivial.
- **fprime-yamcs has no CFDP bridge.** Version 0.2.1 (pinned) and 0.2.4 (latest on 30 September 2026) carry only `FprimeFilePacketService` for `Fw::FilePacket`. The launcher does accept extra plugin jars through `--yamcs-plugin-jars`.
- **Yamcs 5.12.8 has a native `CfdpService`** that exchanges raw PDUs on streams.
- The subtopology doc warns that CFDP transfers are not sandboxed: a ground-commanded transaction can read or write any path the process can reach.

## Questions the spike must answer

1. What exactly does `cfdpManager` put in a `dataOut` buffer and expect in a `dataIn` buffer: a bare PDU, or a PDU behind an F Prime packet descriptor? How does `FprimeRouter` tell a CFDP PDU from an `Fw::FilePacket`, and on which APID?
2. What must the ground shim do so Yamcs's `CfdpService` and F Prime agree: wrap and unwrap the space packet, and match entity id length, sequence number length, checksum type, and maximum PDU size against the 1024-byte TC and TM frames in `ground/yamcs/etc/yamcs.fprime-project.yaml`?
3. Can the shim be a small Yamcs plugin jar loaded with `--yamcs-plugin-jars`, or stream configuration alone, without forking fprime-yamcs?
4. What throughput do you get? Transmit is driven at 1 Hz with a per-cycle PDU limit, so measure it rather than guess.

## Steps

1. **Flight.** Swap `FileHandling` for `FileHandlingCfdp` in `flight/DoomSat/Top/topology.fpp`, wire the PDU, buffer, and 1 Hz ports, and connect `DataProducts.dpCat.fileOut` to `cfdpManager.fileIn`. Remember `scripts/wsl_sync.sh` copies an explicit file list.
2. **Ground.** Configure `CfdpService` in the Yamcs instance and build the shim.
3. **Class 1 first**, one small scenario WAD up, then one file down.
4. **Class 2**, same files.
5. **Lossy link harness.** Put a small UDP relay between Yamcs and the F Prime comm bridge (TM on port 50000, TC on port 50001) that drops a configurable percentage of datagrams.
6. **The demonstration.** Uplink a WAD at 5 percent loss both ways. The native Stage 1 path should fail its checksum; CFDP Class 2 should complete through NAK retransmission. Then `LOAD_WAD` it and show the level running.

## Constraints

Everything in the Stage 1 constraints still holds: the honesty suite passes, no edits to `research/`, `docs/CHARTER.md`, `knowledge/`, or pilot decision logic, no WADs or keys in git, existing line endings kept, and no graded runs on uplinked WADs. Keep the Stage 1 native path recoverable (a branch is enough). Restrict CFDP destination paths to the uplink directory where the component allows it, and document what it does not allow.

## Finish

Push `feature/cfdp-spike` to this fork and open a draft pull request inside the fork only. Do not open anything against `Devonance/DoomSat`, `nasa/fprime`, or `fprime-community/fprime-yamcs`; draft the text of any upstream issue or contribution in the findings file and I will file it. The final report answers the four questions, separates what ran from what was only read, and says plainly whether CFDP is ready to merge, needs upstream work first, or should wait for a later F Prime release.

Out of scope here, noted for later: F Prime v4.3.0 also ships a `ComCcsdsSdls` subtopology for SDLS on the command link (its default decryptor is clear text).

---

## Findings

_Written by Claude on 4 October 2026, at the end of the spike. Branch `feature/cfdp-spike`, built from
`feature/wad-uplink` at `cddba1b`; Stage 1's native path stays there (draft PR PaulMRamirez/DoomSat#1)._

Each claim is marked with how it is known:

- **ran**: ran on the DoomSat stack in this container (F´ v4.3.0, fprime-yamcs 0.2.1, Yamcs 5.12.8). Times are
  wall clock on the ground unless they say "on board". The flight logs are on the spike's container, not in git:
  `$DOOMSAT_HOME/DoomSat/logs/fprime-yamcs-2026_10_04-*` (`00_02_51` no loss; `00_11_14` and `00_42_42` at 5 %
  loss; `00_15_20` latency and 2 ms; `00_40_41` paths and boot parameters; `00_51_38` the README command and 263
  downlinks; `02_00_48` the receive-chunk, temp-directory and first data-cap runs; `02_29_13` the final
  configuration: the data cap at 981 and the README command again), and
  `…-2026_10_03-23_59_21` for the native run at 5 % loss.
- **ran (harness)**: ran in an isolated scratch harness during the spike (a separate F´ build, an in-process
  Yamcs), not on the stack.
- **read**: read in source or docs, not executed.
- **inferred**: reasoned from what was read or ran, not tested.

Source paths: `F´:` is `lib/fprime` in the F´ project (v4.3.0, `7d8f579`), `Y:` is Yamcs 5.12.8
`yamcs-core/src/main/java/org/yamcs/`, `FY:` is the installed `fprime_yamcs` 0.2.1 package.

### Verdict

**Ready to merge, Class 2 only. No upstream work is needed first, and waiting for a later F´ release would not
help.** On the pinned versions the whole demonstration ran: CFDP Class 1 and Class 2 up and down, a 4.2 MB IWAD
both ways, native file packets failing 3 of 3 at 5 % frame loss each way while CFDP Class 2 delivered every file
whole, then `COMMIT_WAD` and `LOAD_WAD` with the level flying. The suite (473 tests) and the honesty suite pass.

What makes that true is six local workarounds, each small. Five are pinned by tests; the local instances only by
the build:

- local instances in place of the `FileHandlingCfdp` subtopology, which does not compile;
- `MaxPduSize` 1001 in place of a stock 1024 that asserts;
- `OutgoingFileChunkSize` 981, because F´ sizes a PDU's data from an uninitialised header (F10);
- 2048 receive chunks in place of 58, without which a lossy upload resends most of the file (F11);
- a small launcher wrapper (`ground/yamcs/launch.py`), because fprime-yamcs sorts the keys of the instance YAML;
- `COMMIT_WAD`, because `cfdpManager` has no `fileAnnounce`. It carries the size and CFDP checksum of what was
  sent, and the Doom component renames only a file that has both. (The commit could also move on board with no
  upstream work, through a guard component that records each upload's destination from its Metadata and commits
  on the receiver's FIN: risk 3.)

The upstream drafts below would retire them, but none blocks. F´ v4.4.0 and `devel` (`55f597d`, 2 October 2026)
still carry the `configure` bug, still have no reference wiring and still have no CFDP sandbox. For this work an
upgrade buys the framer fix (risk 4), a FileManager sandbox, and a PrmDb sandbox widened from parameter loads
(which v4.3.0 already confines) to all its file access; `cfdpManager` gets none, and the upgrade
costs a port (v4.4.0 removes `ComCfg.AggregationSize`, which the PDU size here is set against) [read]. Revisit at
the next planned F´ bump.

Two conditions come with the merge:

1. **WADs go Class 2.** Class 1 has no retransmission, and F´ keeps a Class 1 file whose checksum failed and
   reports it completed [ran]. The demo defaults to Class 2 on this build and refuses to commit a Class 1 file
   after `RxCrcMismatch`. That event can itself be lost on the downlink, but the file still does not get its
   name: `COMMIT_WAD` checks its size and checksum on board and refuses it (`WadCommitRefused`).
2. **Destination paths are not sandboxed.** They were not on the Stage 1 build either, though FileUplink could
   have been (it has `configure(directory)`; Stage 1 never called it). What is and is not restricted is set out
   below. That is acceptable for a demonstration stack; anything more needs the guard
   component or an upstream sandbox first.

Merging replaces the native file packets: FileUplink and FileDownlink leave the topology, because `FprimeRouter`
has one file output and CFDP and `Fw::FilePacket` both arrive on APID 3 (Q1). The native path stays on
`feature/wad-uplink`.

### What ran

| Run | Result | Evidence |
|---|---|---|
| Class 1 up, `basic.wad` (2,704 B), no loss | Byte-identical on board | ran: `MetadataReceived`, `RxFileTransferCompleted`; md5 |
| Class 2 up, `basic.wad` and `doom1.wad` (4,196,020 B) at 40, 10, 5 and 2 ms per PDU, no loss | All byte-identical | ran; md5 on board |
| Class 1 down (`cfdpManager.SendFile`), `basic.wad` | Byte-identical in the `cfdpDown` bucket | ran |
| Class 2 down, `doom1.wad`, no loss | Byte-identical, 67.1 s on board | ran |
| Native file packets at 5 % loss each way (Stage 1 build), `cig.wad` (214,730 B) ×3 | 3 of 3 `BadChecksum`; 59 `PacketOutOfOrder` in all, about 20 lost packets a file. The same file through the relay at 0 % arrived whole | ran: `$DOOMSAT_HOME/DoomSat/logs/fprime-yamcs-2026_10_03-23_59_21` |
| Class 2 up at 5 % each way, `cig.wad` ×2 and `doom1.wad` | All byte-identical | ran |
| Class 2 down at 5 % each way, `doom1.wad` | Byte-identical, 74.6 s | ran |
| Class 2 up at 5 % each way, `doom1.wad`, 10 ms, before and after the receive-chunk override | One run each: 121.1 s and about 9,820 PDUs sent (about 5,590 of them resends, for 238 lost); then 61.1 s and 4,476 PDUs sent: 248 after the first pass, so at most 247 resends (the FIN's ACK is among them). Both byte-identical | ran: `00_11_14`; `02_00_48` and Yamcs's vc2 frame count, matching the relay's 4,229 + 247 |
| Uploads at 50 % TC loss, `basic.wad` ×4 | All byte-identical. Twice the metadata was lost and `cfdpManager` staged the data in `<uplink>/.cfdp-tmp/` (`RxTempFileCreated`), then moved it in place | ran: `02_00_48` |
| Downlink transaction numbers past 255 in one boot | 263 downlinks; a 2,704-byte Class 2 downlink as transaction 263 went down whole with no assert, with 981 and then 989 bytes of file data per PDU (its second PDU a full 1001 bytes) where one-byte numbers had carried 990 (F10) | ran: `00_51_38`; packet sizes from the Yamcs archive |
| The data cap (`OutgoingFileChunkSize` 981) | File data per PDU in a Class 1 downlink of `basic.wad`: 981, 981, 742 bytes. (At 987, the first setting: 981, 987, 736.) | ran: `02_29_13` (`02_00_48`); Yamcs archive |
| Class 1 up at 5 % each way, `cig.wad` ×4 | 0 of 4 whole. Twice the metadata PDU was lost: nothing written on board, only `RxInactivityTimeout` (WARNING_LO). Twice data PDUs were lost: `RxCrcMismatch` (WARNING_LO), then `RxFileTransferCompleted` (ACTIVITY_HI), and the corrupt file kept at full size. Yamcs reported all four COMPLETED | ran |
| `COMMIT_WAD` then `LOAD_WAD` after a Class 2 upload at 5 % each way | `WadUplinked`, `WadLoaded`, a frame from the new level (`cig.wad` over `freedoom2.wad` on MAP02) | ran, twice (flights `00_11_14` and `00_42_42`) |
| The README's own command, unchanged, on this build | Detected CFDP, Class 2 up, committed, loaded, frame saved | ran: `00_51_38`, and again on the final configuration (`02_29_13`) |
| `COMMIT_WAD` refusals | `../etc/x.wad.1.part` VALIDATION_ERROR; a missing `.part` EXECUTION_ERROR | ran |
| Destination paths outside the uplink directory | Written; see "Destination paths" | ran |
| Boot parameters (`flight/config/PrmDb.json` → `PrmDb.dat`) | `PrmFileLoadComplete`, 10 records, no warnings; the fast CRC pass with no `PRM_SET` sent | ran |
| CONTROL round trips during Class 2 uplinks | Median 100 ms during the uplink at 10 ms and at 5 ms pacing; 100 and 108 ms with the link idle | ran |
| Unit tests | 473 pass (412 before this stage, 434 before the review); honesty 8 checks and 17 with `--canary`, 0 failed | ran |

The relay's drops are seeded and reproducible. In the Class 1 runs on 4 October its TC drop list, replayed
offline (`random.Random("11-TC")`), matched the `UnexpectedSequenceCount` gaps the flight software logged one for
one, including the two lost metadata PDUs (frames 0 and 438): every uplink loss was the relay's, none the
stack's. The corrupt Class 1 files have exactly the lost PDUs' bytes zeroed (13 and 10 PDUs).

### Q1. What `cfdpManager` sends and expects, and how the router tells it apart

**A bare CFDP PDU behind the two-byte F´ packet descriptor `0x0003` (`FW_PACKET_FILE`), with no PDU CRC, both
ways, on APID 3. `FprimeRouter` cannot tell it from an `Fw::FilePacket`.**

- Down: `sendPduBuffer` writes `00 03` and then the PDU (`F´:Svc/Ccsds/CfdpManager/CfdpManager.cpp:290-309`)
  [read]. ComQueue takes the APID from the descriptor and `SpacePacketFramer` keeps the descriptor in the data
  field [read]. Every APID 3 packet starts `00 03`, and a full file-data PDU is 2 + 1001 bytes [ran (harness)];
  the stack's downlink filter depends on that descriptor, and every downlink arrived [ran].
- Up: `dataIn` drops any buffer whose first U16 is not `0x0003`, then strips it (`CfdpManager.cpp:155-191`)
  [read]. Every upload here went up as `1003 C000 LLLL 0003 <PDU>` and was accepted [ran].
- Routing: `FprimeRouter` switches on the APID alone (`F´:Svc/FprimeRouter/FprimeRouter.cpp:32-90`), APID 3 goes
  to its one `fileOut`, and `ComCfg.Apid` has no CFDP entry [read]. So on board CFDP and `Fw::FilePacket` are
  rival consumers of the same output, and this build wires it to `cfdpManager.dataIn[0]`
  (`flight/DoomSat/Top/topology.fpp:77`). The ground can tell them apart: a CFDP header's first byte carries
  version `001` in its top three bits, while a FilePacket's first byte is a type 0 to 3; `cfdp_in` filters on
  that (`ground/yamcs/etc/cfdp_streams.sql:6`) [ran].
- A side effect: ApidManager keeps one sequence counter per APID for both checking uplink and stamping downlink,
  and logs every mismatch unthrottled (`F´:Svc/Ccsds/ApidManager/ApidManager.cpp:24-35`) [read]. It does not drop
  the packet [read]. During a lossy 4.2 MB upload that was 486 `UnexpectedSequenceCount` WARNING_LO events [ran].
  They make a fair uplink-loss counter.

### Q2. What the ground shim must do, and the parameters that must agree

**Wrap and unwrap the space packet and the descriptor, in stream SQL; nothing else.** Down: keep APID 3 packets
with descriptor 3 and CFDP version 1, strip 6 + 2 bytes. Up: prepend `1003 C000 0000 0003`; fprime-yamcs's
`FprimeCommandPostprocessor` fills the length and the per-APID count [ran]. Feeding a TC virtual channel from
`cfdp_out` directly fails in Yamcs's `PreparedCommand.fromTuple`, so the SQL copies into a TC-shaped
`cfdp_tc` stream [ran (harness)].

| Item | Yamcs `CfdpService` | F´ `cfdpManager` | Set here |
|---|---|---|---|
| Entity ids | `ground` 100, `doomsat` 42 | `LocalEid` 42, `FileInDefaultDestEntityId` 100; a PDU for another destination is dropped [read] | Both sides from the repo: the Yamcs YAML and `flight/config/PrmDb.json`, pinned equal by a test [ran] |
| Entity id length | `entityIdLength` sets outgoing only; incoming is read from each header | Sends the fewest bytes the value needs; reads 1 to 8 [read] | 2 [ran] |
| Sequence number length | `sequenceNrLength` | Fewest bytes; kept in a U32 [read] | 4 [ran] |
| Checksum | MODULAR or NULL; no CRC32 [read] | Always modular, whatever the metadata says [read] | MODULAR; NULL would fail on board [read] |
| PDU CRC, large file | Never set; refuses large-file PDUs [read] | Ignores both flags on receipt instead of refusing them [read] | Off |
| Largest PDU up | `maxPduSize` | No limit checked on receipt [read] | 1009 = 1024 − 5 (TC frame header) − 2 (FECF) − 6 (space packet header) − 2 (descriptor). 1010 is refused as "cmd size 1018" [ran (harness)]; 1009 ran |
| Largest PDU down | Any | `ComAggregator` asserts on a space packet over `AggregationSize` 1009 (`F´:Svc/ComAggregator/ComAggregator.cpp:124`), so `MaxPduSize` ≤ 1001 [read]; the stock 1024 is FATAL on the first full file-data PDU [ran (harness)] | `MaxPduSize = 1001` (`flight/config/CfdpCfg.fpp:69`); 4.2 MB down with no assert [ran]. But F´ sizes file data from a header it has not filled in yet (`TransactionTx.cpp:346-350`; F10), so `MaxPduSize` alone does not bound the PDU: one more byte once the transaction number needs two would make 1010 [read]. `OutgoingFileChunkSize` 981 caps the data so a PDU is at most 1001 bytes whatever the entity ids (`SendFile` takes any `destId`) and transaction number, up to four bytes each [inferred, from the header arithmetic; ran: file data ≤ 981 per PDU] |
| NAK segments | Up to 124 in one NAK | Keeps the first 58 [read] | Harmless: the rest are asked for again |
| Closure requested (Class 1) | Bit 0x40, as the CFDP standard lays out the byte | Bit 0x80 (`F´:.../Types/MetadataPdu.cpp:123,180`) [read] | Not used |
| Proxy put, directory listing | On by default | Not implemented [read] | `hasDownloadCapability` and `hasFileListingCapability` false: downlinks start on board with `cfdpManager.SendFile` |
| Packets per TC frame | Packs several by default | `SpacePacketDeframer` keeps only the first (Stage 1) [ran] | `multiplePacketsPerFrame: false` on both TC channels |
| Commands beside a transfer | One TC link, virtual channels multiplexed FIFO by default [read] | Any VCID | `priorityScheme: ABSOLUTE`, commands on vc1 (priority 10), PDUs on vc2 (priority 1): CONTROL stays at 100 ms during an upload [ran] |
| Finished transfers | Answer PDUs by (entity, sequence) for `pendingAfterCompletion`, 10 min by default [read] | Numbers its transactions from 1 at every boot [read] | 60 s, still longer than F´'s FIN retries (2 s × 10). After a flight-only restart a new downlink lands on an old transfer only if it starts within 60 s of a finished one with the same number, not 10 min [inferred] |
| On-board CRC pass | n/a | `RxCrcCalcBytesPerCycle` default 64 KiB a 1 Hz tick: about a minute for `doom1.wad` [ran (harness)] | 16 MiB in `PrmDb.json`, loaded at boot [ran] |

### Q3. Plugin jar, or stream configuration alone?

**Stream configuration alone, with no fork and no Java, but not with fprime-yamcs's launcher as it is.** The
launcher rewrites the instance YAML with `yaml.safe_dump`, which sorts keys (`FY:__main__.py:561`) [read]. Yamcs
creates `streamConfig` entries in the order it reads them, so the sort puts `sqlFile` ahead of `tc` and `tm`, its
SQL reads `tm_realtime` before that stream exists, and Yamcs fails with `RESOURCE_NOT_FOUND 'tm_realtime'`
[ran (harness)].
`ground/yamcs/launch.py` imports fprime-yamcs with `safe_dump` set to keep key order, and the scripts start
Yamcs through it [ran]. The SQL path is absolute (`${env.DOOMSAT_REPO}`) because Yamcs resolves it against its
working directory, the F´ project [ran].

A plugin jar should work too, as a fallback [inferred]: a small `AbstractYamcsService` that creates its own
streams compiled with `javac` against the bundled Yamcs jars, and its byte transforms passed checks offline
[ran (harness)], but it was never loaded into Yamcs. It would not depend on key order, at the cost of a JDK at build time (fprime-yamcs's bundled JRE has
no `javac`, and CLAUDE.md promises Java is not needed).

### Q4. Throughput, measured

| Direction | Configuration | Size | Time | Rate | |
|---|---|---|---|---|---|
| Up, Class 2 | 40 ms per PDU (the default), no loss | 4,196,020 B | 171.1 s (170.7 on board) | 24.5 kB/s | ran |
| Up, Class 2 | 10 ms | 4,196,020 B | 44.1 s (44.07 on board in another run) | 95 kB/s | ran |
| Up, Class 2 | 5 ms | 4,196,020 B | 23.1 and 24.0 s (22.22 on board in a third run) | 175 to 182 kB/s | ran |
| Up, Class 2 | 2 ms | 4,196,020 B | 10.0 s | 418 kB/s | ran, once |
| Up, Class 2 | 10 ms, 5 % loss each way, stock 58 receive chunks | 4,196,020 B | 121.1 s: about 42 s for the first pass, then 79 s of NAK-driven resending | 34.6 kB/s | ran |
| Up, Class 2 | the same, 2048 receive chunks (this branch) | 4,196,020 B | 61.1 s (60.4 on board) | 68.7 kB/s | ran |
| Up, Class 2 | 5 % loss each way, `cig.wad` | 214,730 B | 5.1 s at 10 ms; 12.0 s at 20 ms | 42; 18 kB/s | ran |
| Down, Class 2 | 64 PDUs a 1 Hz tick (default), no loss | 4,196,020 B | 67.1 s on board | 62.6 kB/s | ran |
| Down, Class 2 | the same, 5 % loss each way | 4,196,020 B | 74.6 s | 56.3 kB/s | ran |
| Up, native file packets (Stage 1) | 512 B chunks, 20 ms apart | 4,196,020 B | 167.7 s | 25.0 kB/s | ran |

kB is 1000 bytes. How to read it:

- Uplink time is about (PDUs × the delay between them) + 1 to 2 s. `doom1.wad` is 4,226 file-data PDUs of 993
  bytes plus metadata and EOF: 4,228 × 40 ms = 169.1 s against 170.7 s on board [ran]. Yamcs paces every PDU,
  retransmissions included, so the delay sets the rate. The offered delays are 5 to 100 ms
  (`pduDelayPredefinedValues`). 2 ms ran clean once; nothing faster was tried on the stack. In the harness, an
  unpaced burst ran the shared `commsBufferManager` dry: the frame accumulator dropped uplink frames
  (`NoBufferAvailable`), the framer dropped one downlink packet, and the downlink then stopped (risk 4)
  [ran (harness)].
- Downlink is about `max_outgoing_pdus_per_cycle` (64) × 990 bytes per `run1Hz` tick [read]. Raising it in
  `PrmDb.json` should raise the ceiling, but file PDUs go out ahead of telemetry (FILE has queue priority 1,
  TELEMETRY 2), so a big downlink holds up game frames while it runs [read; not measured].
- Under loss, the tail is resending. Stock F´ v4.3.0 tracks at most 58 received runs per transaction
  (`F´:default/config/CfdpCfg.hpp:37`) and, once that list is full, drops a new run unless it is larger than the
  smallest it holds (`Chunk.cpp:275-298`), so it forgets data it has already written. Its first NAK leaves that out;
  a later one, once the gaps it can still see are refilled, asks for much of the rest of the file, and Yamcs
  resends every PDU inside a NAK segment (`Y:cfdp/CfdpOutgoingTransfer.java:309-318`) [read]. At 5 % loss the
  4.2 MB upload lost 238 PDUs on its first pass and Yamcs then sent about 5,590 more, 1.3 times the file, mostly in
  two continuous runs (about 3,050 PDUs over 30 s, then about 1,870 over 19 s), with shorter runs between and
  pauses of 2 to 4 s [ran]. With 2048 receive chunks (`flight/config/CfdpCfg.hpp`) the same upload sent at most 247
  resends and took half the time [ran]. Each NAK carries at most 58 gaps (`NakPdu::addSegment` refuses more), and
  the next goes out only after an `ack_timer` (2 s) with no data, so 238 gaps take about five rounds: most of
  that run's 18 s tail is waiting [read; the tail ran]. A 28.8 MB IWAD at 5 % (about 1,400 gaps) would need about
  24 rounds [inferred].
- At the default 40 ms, CFDP Class 2 matches the native rate; at 5 ms it is seven times faster, and it survives
  loss.

### The demonstration at 5 % loss each way

Behind `tools/lossy_relay.py --loss 5` (Yamcs started with `DOOMSAT_RELAY=1`; `ground/yamcs/launch.py` moves its
frame links to 51000/51001 and the relay forwards to the comm bridge's 50000/50001):

- **Native file packets** (Stage 1 build): `cig.wad` failed 3 of 3 with `BadChecksum` [ran].
- **CFDP Class 2**: `cig.wad` and `doom1.wad` arrived byte-identical; `doom1.wad` also came down identical [ran].
- **`COMMIT_WAD` and `LOAD_WAD`**: on 4 October, `cig.wad` as `cigc2.wad` (FIN after 12.0 s), then
  `WadUplinked`, `WadLoaded: Now flying cigc2.wad over freedoom2.wad on MAP02`, WAD_LOADS 0 → 1, EPISODE 1 → 2
  and a frame from MAP02 [ran]. Both times the first `COMMIT_WAD` worked on board but its `WadUplinked` never
  reached the ground, so the demo sent it again, and the retry found no `.part` and said `WadUplinkFailed` [ran].
  The first time (`cig.wad`, before `--tries` handled that case) the demo stopped there with FAIL, and `LOAD_WAD`
  went up from a second run, which had to resend it once. The second time the demo carried on, and `LOAD_WAD`
  showed the file was in place [ran].
- **CFDP Class 1** at the same loss: 0 of 4 whole (see "What ran"). The demo refused to commit [ran].

Commands are single unprotected TC frames: there is no COP-1 here. That is why the demo resends them, and it is
the next thing to look at for a lossy link, not CFDP.

### Destination paths: what is restricted and what is not

Restricted [ran]:

- `COMMIT_WAD(part, fileSize, checksum)` renames only a bare `NAME.wad[.<nonce>].part` inside
  `$DOOMSAT_HOME/wads/uplink`, and only when the file's size and CFDP modular checksum are the ones the ground
  sent (`flight/Components/Doom/Doom.cpp:115-180`): any `/` is a VALIDATION_ERROR, a missing file an
  EXECUTION_ERROR with `WadUplinkFailed`, and a file that differs an EXECUTION_ERROR with `WadCommitRefused`.
- `LOAD_WAD` names only bare `.wad` files in `wads/uplink` and `wads/`, refuses `.part` names, and proves the
  file in a child process before the game switches (Stage 1).

Not restricted, because `cfdpManager` uses the metadata's destination path as it stands
(`F´:Svc/Ccsds/CfdpManager/Engine.cpp:405-406`) and opens it to create or overwrite
(`TransactionRx.cpp:368`) [read]:

- An upload to `/tmp/doomsat_cfdp_escape.bin` and one to `/root/doom/wads/uplink/../escape_dotdot.part` were
  both written, byte-identical [ran].
- An upload to an existing file overwrote it in place [ran].
- `cfdpManager.SendFile` downlinked `/etc/hostname` [ran]. With `keep` DELETE it deletes the source after
  sending, and so do downlinks asked for through `fileIn` unless `FileInDefaultKeep` says KEEP, as `PrmDb.json`
  now does [read]. `PlaybackDirectory` and `PollDirectory` do the same for a whole directory: playback deletes with
  `keep` DELETE, and polling always deletes once a file is sent. A failed poll file is meant to move to
  `fail_dir`, but F´ v4.3.0 renames it onto the `fail_dir` path itself (`Engine.cpp:1155-1156`), so with a
  directory there the move fails and the file is deleted (`1164-1169`); `move_dir` has the same bug (`1135`,
  `1143-1148`; F12) [read]. Neither applies to a received file. Nothing removes one either: `m_keep` is KEEP for
  every receive (`TransactionRx.cpp:71, 96`), so `handleNotKeepFile` never runs for one, and a receive that fails
  stays where it is, as a `.part` at its destination or as `<eid>:<seq>.tmp` in `tmp_dir` [read].
- All test files were removed afterwards.

The component offers no hook to restrict the destination path. The one receive path it does let a deployment
choose is `ChannelConfig.tmp_dir`: when Class 2 file data arrives before its metadata, the file is written there as
`<eid>:<seq>.tmp` and moved to the destination once the metadata arrives (`F´:Svc/Ccsds/CfdpManager/TransactionRx.cpp:347-362,
1094-1097`) [read]. That happened once at 5 % loss, into `/tmp` (`RxTempFileCreated ... /tmp/100:23.tmp`) [ran].
`PrmDb.json` now sets `tmp_dir` to `<uplink>/.cfdp-tmp` (filled in per machine at start), where it worked at 50 %
TC loss [ran], and leaves `fail_dir` and `move_dir` empty, which F´ treats as "delete" (DoomSat runs no polls).
F´ never removes a temp file whose receive ended before its metadata arrived, so the run script clears
`.cfdp-tmp/*.tmp` before each start, while nothing on board runs. That, and `COMMIT_WAD`, is all the component
allows.

F´ file packets on the Stage 1 build were open too, but only because that build never confined them: FileUplink
writes through `Os::SandboxedFile` and has `configure(directory)` (`F´:Svc/FileUplink/FileUplink.hpp:192-201`),
which is fail-open at `/` and which Stage 1 never called [read]. (Worth adding to `feature/wad-uplink` if that
branch is kept.) F´ says CFDP is not sandboxed in `Svc/Subtopologies/FileHandlingCfdp/docs/sdd.md`. v4.4.0 adds a
sandbox to FileManager and widens PrmDb's (v4.3.0 sandboxes only parameter loads, `configureLoadSandbox`) to all
of its file access; `devel` is the same; `cfdpManager` has none in any of them [read]. The ways to close it, in
order:

1. Run the flight side as an ordinary user that can write little besides the uplink directory (no code).
2. A small guard component between `fprimeRouter.fileOut` and `cfdpManager.dataIn` that drops any metadata PDU
   whose destination is not `<uplink>/<basename>.part` (not built; about a day).
3. Upstream: a destination root in `cfdpManager`, like FileUplink's `configure(directory)` (draft F5).

### Risks and limits, most serious first

1. **No destination sandbox** (above).
2. **Class 1 is unsafe for WADs** (above). The demo defaults to Class 2 on this build.
3. **Commands have no retransmission.** A lost `COMMIT_WAD` or `LOAD_WAD` needs a resend; the demo's `--tries`
   does it, and the dashboard's `LOAD_WAD` button does not. The commit itself need not be a ground command: a
   guard component spliced into both `fprimeRouter.fileOut -> cfdpManager.dataIn` and `cfdpManager.dataOut` could
   do it with no upstream work. On the way up it records each Metadata PDU's destination against (source entity,
   sequence number), because a FIN carries no file name. On the way down, at the receiver's own FIN for a recorded
   transaction, it passes that destination to the unconnected `doom.fileAnnounce` and forgets the entry. It must
   key on condition code NO_ERROR and file status RETAINED, not the delivery code (zeroed per transaction, so
   COMPLETE even on failure, `TransactionRx.cpp:103`), and commit once per transaction, since FINs repeat [read].
   It is the destination filter of "Destination paths" item 2 plus a transaction table, so more than that
   filter's day. Not built.
4. **The v4.3.0 framer stalls** when `commsBufferManager` runs dry: it drops the packet without a `comStatus`,
   and ComQueue then waits for ever (`F´:Svc/Ccsds/SpacePacketFramer/SpacePacketFramer.cpp:40-46`) [read; in the
   harness an unpaced burst ran the pool dry, the framer dropped a downlink packet, ComQueue overflowed and no
   later FIN reached the ground]. Fixed in v4.4.0 [read]. It applies to the current stack too,
   CFDP or not [inferred]. The dedicated pool covers only what `cfdpManager` allocates: each uplinked PDU still
   holds a `commsBufferManager` buffer until `cfdpManager`'s async `dataIn` takes it [read]. Keep PDU pacing at
   5 ms or more; the demo refuses less.
5. **Big downlinks hold up game frames** (Q4) [read]. Downlink large files while not flying.
6. **The 40-character command string cap**: `COMMIT_WAD` carries `NAME.<13 digits>.part`, so an uplinked name can
   be at most 19 characters on this build (38 natively). The demo checks before anything goes up [ran].
   `SendFile` paths are capped the same way [read].
7. **A `COMMIT_WAD` sent between the last byte and the CRC pass** renames a whole file, but `r2CalcCrcChunk` then
   reopens the old `.part` name, fails, and the FIN reports a file-size error (`TransactionRx.cpp:903-918`)
   [read]. Earlier than that the size or checksum differs and the commit is refused [ran]. The CRC pass reopens
   the file only on its first chunk, so `RxCrcCalcBytesPerCycle` does not change the window: with no loss it runs
   up to two 1 Hz ticks past the EOF (the first tick sends only the EOF-ACK, `TransactionRx.cpp:273-296`), and
   one after a NAK resend [read]. The demo commits only after the FIN.
8. **Yamcs's sender inactivity timer never arms** (`eofAckReceived` is never set,
   `Y:cfdp/CfdpOutgoingTransfer.java:93`) [read]. If every FIN were lost the upload would stay RUNNING; F´ sends
   ten, so that needs ten in a row lost [inferred].
9. **Channel 1 is wired but must not carry Class 2**: its ACK, NAK and FIN would come back on `dataIn[0]`
   [inferred]. Everything here uses channel 0. Both channels sending at once share the 96-buffer pool: channel 0
   takes its 64 a cycle, channel 1 gets the other 32 and logs `BuffersExhausted` [read].
10. **Cosmetic**: one command-history entry per transfer, rewritten for every PDU; APID 3 packets archived with a
   1970 gentime; the XTCE decodes CFDP PDUs as file-packet noise [read].

### Review of this branch (4 October)

A review of the whole branch found 21 problems, and an adversarial check of each one refuted none (15 confirmed as
stated, 6 confirmed with their severity or reach cut back). All are fixed on this branch, with a test each where
one could be written, with two exceptions. The guard component that would commit on board is described under risk 3
and not built. For class 1, the suggested check of `cfdpManager`'s `faultCrcMismatch` counter is not needed: the
on-board checksum check below refuses a damaged file whether or not its `RxCrcMismatch` reaches the ground. The
fixes that changed behaviour:

- **The native branch could not come back.** This branch's sync left a topology header without FileHandling,
  and its start left `PrmDb.dat` in `bin/`, which stops fprime-gds's `find_app`. `feature/wad-uplink` now
  commits its own header and passes `--app`, and this branch's `prmDb` reads `$DOOMSAT_HOME/run/PrmDb.dat`. On
  the install this branch had used, the native branch built, flew, and uplinked `basic.wad` through FileUplink
  to `WadLoaded` [ran]. This branch then rebuilt, and booted with `PrmFileLoadComplete ... Records: 10` from
  `run/` and only the binary in `bin/` [ran].
- **`COMMIT_WAD` trusted the ground's timing.** It now carries the size and CFDP checksum of what was sent, and
  the Doom component checks both before the rename. On the live stack [ran]:
  - a commit sent with 1.4 of 4.2 MB on board was refused (`WadCommitRefused ... has 1417011 bytes`), and the
    transfer still finished;
  - after the FIN, with one byte flipped on board, the commit was refused; a wrong size was refused; the right
    numbers committed a byte-identical `doom1.wad`. The ground's Python checksum and F´'s `CFDP::Checksum`
    agree: 0x7f3981da both ways;
  - at 5 % loss each way, class 1 `doom1.wad` arrived damaged (`RxCrcMismatch`, actual 0xa6254fea), the demo did
    not commit it, and a `COMMIT_WAD` sent by hand with the true numbers was refused on board with the same
    0xa6254fea;
  - at the same loss, class 2 `cig.wad` committed and flew on MAP02 (FIN after 6.1 s).
- **The parameter file failed open.** If `PrmDb.dat` cannot be built the start now stops (opt in to the defaults
  with `DOOMSAT_PRM_DEFAULTS=1`). The path goes into the JSON escaped, so `&`, `#`, `\` and `"` survive.
  Leftover temp files are cleared at start; a planted one was gone after `flight.sh start` [ran].
- **The demo**: no budget from the size. It gives up when a transfer stops moving: 90 s in the first pass, longer
  at full size, where Yamcs shows no progress while NAK resends and the FIN are under way (`tail_limit`), and
  not at all while it is queued behind another upload. A transfer left running is cancelled on every exit,
  Ctrl-C included, and the bucket object is deleted; `--service cfdp` alone means class 2; `--tries` and `--pdu-delay` are
  checked; no `PRM_SET`; a `COMMIT_WAD` with no answer at all fails (`LOAD_WAD` could otherwise fly an older
  file of the same name), as does an unconfirmed one under `--no-load`; the telemetry stand-in for a lost
  `WadLoaded` must show this load.
- **`DOOMSAT_RELAY=0`** now means off.

The rest were comments that no longer matched the code (the dedicated pool, the buffer count, `fileAnnounce`) and
a relay test that could not pass on Windows. This document's claim that a failed receive is removed was wrong:
nothing removes one (Destination paths).

The fixes then had their own adversarial review: five reviewers by area and a skeptic for each finding. Of 22
findings none was refuted, and they came down to 15 distinct problems, one of them medium. The first stall limit
would have cancelled a healthy class 2 upload of a whole IWAD at 5 % loss, because Yamcs never counts resends:
a simulation of the NAK tail, checked against the measured `doom1.wad` tail, puts `freedoom2.wad`'s at 96 to 243 s.
The rest were low:
- a `COMMIT_WAD` with no answer could let `LOAD_WAD` fly an older file of the same name;
- a transfer was cancelled only on a stall;
- two tests also passed on the code they were meant to catch;
- the header test checked one direction only;
- `DOOMSAT_PRM_DEFAULTS` did not reach WSL from Git Bash;
- wording, including this document's risk 7 window and the guard sketch.

All are fixed, and each new test was run against the previous code and fails there. On the live stack the class 2
demo still ran to OK, and Ctrl-C during a `doom1.wad` upload cancelled the transfer (Yamcs: FAILED, "Cancel request
received", at 103,272 bytes). The cancelled receive's `.part` stayed on board, as "Destination paths" says [ran].

### The reading notes at the top, checked

- F´ v4.3.0 ships a CFDP engine with those ports, Class 1 and Class 2: **confirmed** [ran].
- The `FileHandlingCfdp` subtopology: **exists, but does not compile** at v4.3.0, v4.4.0 or `devel`: it calls
  `cfdpManager.configure(memAllocator)`, and `configure` needs a file queue depth too
  (`F´:Svc/Subtopologies/FileHandlingCfdp/FileHandlingCfdp.fpp:13`, `CfdpManager.hpp:53`) [ran (harness) at
  v4.3.0; read at the others]. This build declares the instances itself.
- The com side is not pre-wired: **confirmed**, and v4.4.0 and `devel` add no CFDP slot to `ComCcsds` and no
  reference deployment either [read]. The SDD's example ports do not exist [read].
- fprime-yamcs has no CFDP bridge: **confirmed** [read]. None was needed (Q3).
- Yamcs 5.12.8's `CfdpService` exchanges raw PDUs on streams: **confirmed** [ran].
- CFDP transfers are not sandboxed: **confirmed** [ran].

### Suggested charter note (for the pull request, not the charter)

> Uplinked WADs stay demonstrations whatever carries them, F´ file packets or CFDP. CFDP changes how a file
> reaches the spacecraft, not what the pilot may see: the pilot side reads no file-transfer state, and no bench
> or graded flight runs on an uplinked WAD.

### Upstream drafts (not filed)

Written for you to file. Each says what ran and what was only read.

**nasa/fprime**

**F1. `FileHandlingCfdp` subtopology does not compile: `cfdpManager.configure` gets one argument of two**
> `Svc/Subtopologies/FileHandlingCfdp/FileHandlingCfdp.fpp:13` emits
> `FileHandlingCfdp::cfdpManager.configure(FileHandlingCfdp::Allocation::memAllocator);`, but
> `Svc/Ccsds/CfdpManager/CfdpManager.hpp:53` declares
> `void configure(Fw::MemAllocator& allocator, FwSizeType fileQueueDepth, FwEnumStoreType memId = 0);`.
> A topology that imports the subtopology fails to build at v4.3.0 (built) and, by reading, at v4.4.0 and at
> `devel` 55f597d. Suggest a `FileHandlingCfdpConfig` constant for the queue depth, passed here.

**F2. Stock `CfdpCfg.MaxPduSize` (1024) asserts in `ComAggregator` with the stock `ComCcsds` (v4.3.0)**
> File data is capped by `OutgoingFileChunkSize` (992 by default) as well as by `MaxPduSize`, so a full stock
> file-data PDU is 7 + 4 + 992 = 1003 bytes. It travels as a space packet of 6 + 2 + 1003 = 1011 bytes, more than
> `ComCfg.AggregationSize` (1009), and `ComAggregator::Svc_AggregationMachine_guard_isFull` asserts on the first
> one: FATAL on the first full downlinked PDU (seen in a v4.3.0 build). `MaxPduSize` 1001 works (see also F10). Suggest deriving the default
> from the frame size, or a static assert tying the two. (v4.4.0 removes `AggregationSize`; the bound there was
> not checked.)

**F3. CFDP Class 1 receive keeps a file whose checksum failed and reports the transfer completed**
> Class 1 upload with lost file-data PDUs, F´ v4.3.0: `RxCrcMismatch` (WARNING_LO), then
> `RxFileTransferCompleted` (ACTIVITY_HI), and the file stays at its destination at full size with zeroed holes
> (seen on a lossy link, twice). On a checksum failure `r1SubstateRecvEof` only logs `RxCrcMismatch` and sets no
> error status (`TransactionRx.cpp:649-663`), so `Engine::finishTransaction` reports completion
> (`Engine.cpp:1006-1019`); keep is KEEP from reset (`TransactionRx.cpp:96`), so nothing removes the file. Suggest:
> treat a checksum failure as a failed transaction (condition code "file checksum failure"), report
> `RxFileTransferFailed`, and delete the file. (`fail_dir` does not apply: it is used only for a failed send from a
> polled directory. `handleNotKeepFile`'s receive branch removes the destination file, `Engine.cpp:1173-1179`, but
> no receive reaches it today, since keep is always KEEP.)

**F4. Metadata PDU: closure-requested is written at bit 0x80, not 0x40**
> `Svc/Ccsds/CfdpManager/Types/MetadataPdu.cpp:123` shifts `closureRequested` by 7 and line 180 reads it from bit
> 7. The CFDP standard's metadata layout is a reserved bit, then closure requested, so the bit is 0x40, which is
> where Yamcs (`MetadataPacket.java:47`) puts it. F´ never reads the flag on receipt (`getClosureRequested` has no
> caller outside tests) and sets it only for Class 2 (`Engine.cpp:192-194`), where Yamcs reads 0x80 as reserved.
> Harmless today; fix it before Class 1 closure is implemented. (Read; a harness saw 0x80 in Class 2 metadata.)

**F5. `cfdpManager`: no destination sandbox and no notice of a received file**
> Metadata destination paths are used as given (`Engine.cpp:405-406`) and opened to create or overwrite
> (`TransactionRx.cpp:368`). On a v4.3.0 deployment, uploads to an absolute path outside any intended directory
> and to `<dir>/../file` were written, an existing file was overwritten, and `SendFile` sent `/etc/hostname`.
> Suggest a root directory like FileUplink's `configure(directory)` (already in v4.3.0) or FileManager's in
> v4.4.0, refusing paths outside
> it, and an output port that announces a completed, verified received file, as FileUplink's `fileAnnounce`
> does, so a deployment can act on it.

**F6. Receive ignores the PDU-CRC and large-file flags instead of refusing them**
> With the CRC flag set, the two CRC bytes are taken as file data (`PduHeader.cpp:177`, `FileDataPdu.cpp:144-151`).
> Suggest refusing such PDUs until they are supported. (Read, not run.)

**F7. `SpacePacketDeframer` keeps only the first space packet in a TC frame**
> A TC frame may carry several space packets; Yamcs packs them by default. v4.3.0 forwards the first and drops the
> rest without an event. On a live stack 19 to 36 % of commands sent close together were lost until the ground was
> set to one packet per frame. Suggest deframing every packet, or at least an event for the dropped bytes.

**F8. ApidManager: one counter for both directions on an APID, and an unthrottled event**
> APID 3 carries file traffic both ways, and `validateApidSeqCountIn` (uplink) and `getApidSeqCountIn` (downlink
> stamping) share `m_apidSequences`. With both directions active, or any uplink loss, every packet can log
> `UnexpectedSequenceCount`: 486 WARNING_LO events in one 4 MB upload at 5 % loss. Suggest separate counters per
> direction and a throttle.

**F9. CFDP SDDs**
> `Svc/Subtopologies/FileHandlingCfdp/docs/sdd.md` (lines 78-89) shows `ComCcsds` CFDP ports that do not exist at
> v4.3.0. `Svc/Ccsds/CfdpManager/docs/sdd.md` says received files are written to `tmp_dir` and moved on completion
> (lines 120, 618); in fact they are written at the destination once the metadata has arrived, and go to
> `tmp_dir` only when file data comes first, moving when the metadata arrives (`TransactionRx.cpp:347-368,
> 1094-1097`). The same SDD gives default timers (`ack_timer` 3 s, `inactivity_timer` 30 s, line 625) that differ
> from `Parameters.fppi` (2 s, 120 s). (Read; the temp file was seen three times on lossy links: once at 5 % loss
> each way, twice at 50 % TC loss.)

**F10. `sSendFileData` sizes file data from an uninitialised header**
> `TransactionTx.cpp:346-350` calls `fdPdu.getMaxFileDataSize()` on a freshly declared `FileDataPdu`, whose
> `PduHeader` has no constructor (`PduBase() {}`), and only fills in the header with `initialize()` later (around
> line 388). `getMaxFileDataSize` sizes from that header (`FileDataPdu.cpp:45-56`), so the data size is decided by
> whatever was on the stack. On a v4.3.0 deployment with `MaxPduSize` 1001, one-byte transaction numbers gave 990
> bytes of file data a PDU; at transaction 263 the same code gave 981 and then 989 (seen). If the stale header is ever
> narrower than the real one (a one-byte number left over, a two-byte number now), a full PDU is a byte over
> `MaxPduSize` and, with the stock `ComCcsds`, asserts in `ComAggregator`. Suggest `initialize()` before sizing,
> or sizing from the real EIDs and sequence number. Workaround: `OutgoingFileChunkSize` ≤ `MaxPduSize` −
> (8 + 2 × entity-id bytes + transaction-number bytes), which is − 20 for the U32 types; − 14 holds only while both
> entity ids fit in a byte, and `SendFile` takes any `destId`.

**F11. Class 2 receive forgets data once 58 runs are tracked, so a lossy upload is mostly resent**
> `CFDP_CHANNEL_NUM_RX_CHUNKS_PER_TRANSACTION` defaults to `{NakMaxSegments, NakMaxSegments}` (58)
> (`default/config/CfdpCfg.hpp:37`). Past about 57 losses, `CfdpChunkList::insert` drops new received runs
> (`Chunk.cpp:275-298`), and a later NAK asks for data already written. Seen on v4.3.0: a 4.2 MB upload from Yamcs
> at 5 % loss lost 238 PDUs and had about 5,590 resent (121 s); with the RX chunk count raised to 2048 by a config
> override, at most 247 (61 s). The default ties the RX chunk count to `NakMaxSegments`, and neither `CfdpCfg.hpp`
> nor the SDD says that it caps how many received runs a Class 2 receive can track, or that past it the receiver
> forgets data it has written and asks for it again. Suggest a default sized for file size × expected loss, and
> saying so. If the two are decoupled, `rSubstateSendNak` should add the NAK's segment count, not the `computeGaps`
> count, to `sentNakSegmentRequests`, which today counts gaps found rather than segments sent.

**F12. `move_dir` and `fail_dir` are used as file names, not directories**
> `Engine::handleNotKeepFile` passes the parameter itself to `Os::FileSystem::moveFile` (`Engine.cpp:1135` and
> `1155-1156`), which is `rename(source, destination)`. With a directory there the rename fails (EISDIR) and the file
> is deleted (`1143-1148`, `1164-1169`); with a path that does not exist, the file is renamed to it and each later
> one overwrites it. The SDD calls them directories. Suggest moving to `<dir>/<basename>`. (Read.)

**nasa/fprime-gds**

**G1. `fprime-prm-write dat --defaults` fails on a struct array whose enum default is fully qualified**
> With the v4.3.0 `CfdpManager` dictionary, `--defaults` raises
> `EnumMismatchException: Invalid enum member Fw.Enabled.ENABLED` for `ChannelConfig`: the dictionary gives the
> default as `Fw.Enabled.ENABLED`, and the enum type accepts `ENABLED`. Writing the value explicitly as `ENABLED`
> works. By reading, plain enum parameters with qualified defaults (`FileInDefaultClass`,
> `Svc.Ccsds.Cfdp.Class.CLASS_2`) take the same path (`params.py:51-52,152`). Seen with fprime-gds 4.4.0, which
> fprime-yamcs 0.2.1 pulls in (F´ v4.3.0 itself pins 4.3.0).

**G2. A parameter file next to the binary stops the launcher finding the app**
> `PrmDb` opens a relative `PrmDb.dat` in its working directory, which the launcher sets to the binary's own
> directory (`run_deployment.py`, `launch_app`, `cwd=app_path.parent`). With `PrmDb.dat` there, `find_app`
> (`executables/utils.py:170-189`) sees two files and exits: "Multiple app candidates ... Specify app manually
> with --app". Suggest ignoring non-executable files when guessing. (Ran, fprime-gds 4.4.0.)

**fprime-community/fprime-yamcs**

**Y1. The instance YAML is written back with sorted keys, which breaks `streamConfig.sqlFile`**
> `__main__.py` rewrites the instance configuration with `yaml.safe_dump(...)`, which sorts keys by default
> (lines 501 and 561 in 0.2.1). Yamcs creates `streamConfig` entries in order, so an `sqlFile` whose SQL reads
> `tm_realtime` now runs before that stream exists and Yamcs fails with `RESOURCE_NOT_FOUND`. One-word fix:
> `sort_keys=False`. Any order-sensitive Yamcs config is affected; CFDP through stream SQL is how this surfaced.
> (Seen offline: the SQL run in the sorted order against Yamcs 5.12.8's stream classes gives
> `RESOURCE_NOT_FOUND 'tm_realtime'`; that this stops the instance is read from `StreamInitializer.java:39-45`.
> With `sort_keys=False` the instance starts and runs.)

**Y2. `${FPRIME_*}` placeholders in the shipped instance YAML read Java system properties, not the environment**
> Yamcs's `${NAME}` reads system properties; environment variables need `${env.NAME}`. (Found in Stage 1.)

**Y3. An upload's COMPLETED means sent, not received**
> `FprimeFilePacketService` marks an upload COMPLETED once its last packet is queued; F´ file packets have no
> acknowledgement, so the ground cannot know the file arrived. Worth saying in the UI and docs. (Stage 1.)

**Y4. Feature: CFDP to `Svc/Ccsds/CfdpManager`**
> Yamcs's own `CfdpService` reaches F´ with four lines of stream SQL (`ground/yamcs/etc/cfdp_streams.sql` in
> DoomSat: APID 3, descriptor `0x0003`, CFDP version bits) and a second TC virtual channel. Shipping that as an
> option, with Y1 fixed, would give fprime-yamcs users reliable file transfer. Its `SendFile` discovery should
> then not pick up `cfdpManager.SendFile`. (DoomSat ran it; the discovery point is read.)

**yamcs/yamcs**

**YA1. `CfdpOutgoingTransfer`: `eofAckReceived` is never set**
> The field is declared false (`CfdpOutgoingTransfer.java:93`) and only read (lines 302, 410, 422), so the
> sender's inactivity timer never arms after the EOF is acknowledged. A Class 2 upload whose FINs are all lost
> stays RUNNING. (Read, 5.12.8.)

**YA2. `CfdpService`: options read but not declared, and docs that disagree with the Spec**
> The code reads options its Spec does not declare, so setting any of them is refused at startup ("Unknown
> argument", `Spec.java:249-252`), and the docs do not list them: `maxFileSize`, `ackEofWhileSuspended`,
> `checkAckTimeout` and `checkAckLimit` (`CfdpIncomingTransfer.java:146-155`) and `maxAckSendFreq`
> (`OngoingCfdpTransfer.java:114`). The docs give `eofAckTimeout` 3000 and `finAckTimeout` 10000 where the Spec has
> 5000 for both (`cfdp.rst:161-172`, `CfdpService.java:232-234`), and describe `allowConcurrentFileOverwrites`
> backwards: they say true makes the service check, default true (`cfdp.rst:238-239`), but the code checks when it
> is false, the default (`CfdpService.java:246, 958-971`). (Read, 5.12.8.)

### What is in the branch

- **Flight**: `cfdpManager`, `fileManager`, `prmDb` and a dedicated 96 × 1024 B buffer pool, declared in
  `flight/DoomSat/Top/instances.fpp` (queue 200; async inputs assert when the queue is full) and wired in
  `topology.fpp` to the com queue's FILE slot, the router's `fileOut`, the 1 Hz group and `dpCat`.
  `DoomSatTopologyDefs.hpp` is committed because the generated one names FileHandling. `MaxPduSize` 1001
  (`CfdpCfg.fpp`) and 2048 receive chunks (`CfdpCfg.hpp`). `COMMIT_WAD(part, fileSize, checksum)` in the Doom
  component, which checks the file before it renames it. Boot parameters in `flight/config/PrmDb.json`: the data
  cap, the fast CRC pass, KEEP, the entity ids, and the temp directory under the uplink directory. `prmDb` reads
  them from `$DOOMSAT_HOME/run/PrmDb.dat`, outside `bin/`.
- **Ground**: `CfdpService` in `ground/yamcs/etc/yamcs.fprime-project.yaml` with TC virtual channel 2,
  `cfdp_streams.sql`, `ground/yamcs/launch.py`.
- **Tools**: `tools/lossy_relay.py`; `tools/wad_uplink_demo.py` uses CFDP Class 2 when Yamcs offers it, with
  `--cfdp 1|2`, `--pdu-delay` (5 ms or more), `--tries`, and `--checksum FILE` for a commit by hand. It gives up
  on a transfer only when it stops moving, and then cancels it. `tools/prmdb.py` fills the uplink directory into
  `PrmDb.json`.
- **Scripts**: `wsl_sync.sh` also copies `CfdpCfg.fpp`, `CfdpCfg.hpp` and `DoomSatTopologyDefs.hpp`, and
  `flight/config/CMakeLists.txt` registers the two config overrides; `wsl_run_flight.sh` builds
  `$RUN/PrmDb.dat` from `PrmDb.json` before each start, and stops if it cannot (`DOOMSAT_PRM_DEFAULTS=1` to fly on
  the defaults), then starts Yamcs through `launch.py` with `--app`; `flight.sh` passes `DOOMSAT_RELAY*` into WSL,
  builds the parameter file for `gds` too, and gives `fprime-gds` `--app`.
- **Tests**: `tests/test_cfdp_spike.py` (YAML, SQL, PDU sizes, boot parameters and how they are built,
  `COMMIT_WAD`, the launcher, the relay), the demo's CFDP and lossy paths in `tests/test_wad_uplink_demo.py`, and
  the topology header and launchers in `tests/test_flight_scripts.py` (shared with `feature/wad-uplink`).
- **Docs**: README "Uplink a new level", `docs/ARCHITECTURE.md`, CLAUDE.md.

### Not done

- The guard component (Destination paths, 2), and running the flight side as a confined user.
- Downlink pacing above 64 PDUs a tick, and its effect on game frames, measured.
- The dashboard's `LOAD_WAD` button does not resend on a lossy link, and the dashboard has no upload.
- COP-1 for commands.
- Data products through `dpCat.fileOut` → `cfdpManager.fileIn`: wired, but none was downlinked (DoomSat makes
  none).
- Charter: no edit. The suggested note goes in the pull request.
