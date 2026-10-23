# cfdpGuard: uploads confined to the uplink directory, committed on board

The CFDP spike (`docs/plans/cfdp-stage2-spike.md`) left two gaps. First, `cfdpManager` wrote an upload wherever
its Metadata said (risk 1). Second, a received WAD only got its real name when the ground sent `COMMIT_WAD`, a
single unprotected command (risk 3). `cfdpGuard` (`flight/Components/CfdpGuard`) closes both with no change to F´.

## What it does

It is a passive component on both of `cfdpManager`'s paths:

```
ComCcsds.fprimeRouter.fileOut -> cfdpGuard.uplinkIn      cfdpGuard.uplinkOut -> cfdpManager.dataIn[0]
cfdpManager.dataOut[n] -> cfdpGuard.downlinkIn[n]        cfdpGuard.downlinkOut[n] -> comQueue FILE
cfdpGuard.fileAnnounceOut -> doom.fileAnnounce
```

- **Up.** Every Metadata PDU's destination must be `NAME.wad[.<nonce>].part`, directly in
  `$DOOMSAT_HOME/wads/uplink`, using only letters, digits and `_ . + -`. The demo always adds a nonce; the rule
  does not require one. This is the rule in `flight/Components/Doom/WadPath.hpp`, which the Doom component uses as
  well. A Metadata that fails the check never reaches `cfdpManager` as a PDU. Everything else passes through
  untouched.
- **Down.** When `cfdpManager`'s own FIN for an upload the guard let through says no error, delivery complete and
  file retained, the guard announces the file to the Doom component, which renames it to `NAME.wad`. It does this
  before the FIN leaves, so the file is in place by the time the ground hears of it. The receiver sends that FIN
  only after its checksum over the whole file, read back from disk at the Metadata's destination, matched the
  EOF's (`TransactionRx.cpp` `r2CalcCrcChunk`).

## Decisions, and why

- **It reads PDUs with F´'s own classes.** The guard cuts the buffer the way `CfdpManager::dataIn_handler` does,
  types it with `peekPduType` and decodes it with `MetadataPdu` / `FinPdu`, the same code `cfdpManager` runs. So
  it sees a Metadata exactly when `cfdpManager` would, and it checks the destination string `cfdpManager` would
  open. That string is NUL-terminated at the first embedded NUL; a unit test pins this. A hand-written parser
  could disagree with F´'s, and a crafted PDU would then get through.
- **A refused Metadata goes on to `cfdpManager` with its descriptor changed.** Returning the buffer to the router
  from inside the router's own call would deadlock. The router still holds its guarded-port mutex
  (`PTHREAD_MUTEX_ERRORCHECK`), so the relock returns `EDEADLK` and `Os::Mutex` asserts. `cfdpManager` instead hands
  back any buffer whose descriptor is not `FW_PACKET_FILE`, unread, on its own thread, through the return path the
  router already has. `tests/test_cfdp_guard.py` reads that branch from F´ when the install is there.
- **Drop the Metadata, not the transaction.** The file data and EOF still reach `cfdpManager`. For class 2, with
  no Metadata, it writes them to `<tmp_dir>/<eid>:<seq>.tmp` (`<uplink>/.cfdp-tmp` from the parameter file), asks
  for the Metadata nine times (with `nack_limit` 10, the tenth count sends the FIN instead of a NAK), and ends with
  a FIN carrying `NAK_LIMIT_REACHED`, so the ground shows FAILED. Class 1 file data with no Metadata is dropped
  unwritten (`Engine::recvInit`), and class 1 never hears back, so the ground shows COMPLETED. If the guard dropped
  every PDU instead, Yamcs would end PAUSED with no reason given. With the parameter file the staged bytes stay
  inside the uplink directory, and `scripts/wsl_run_flight.sh` clears `.cfdp-tmp` at each start. A start that
  flies without the parameter file (`DOOMSAT_PRM_DEFAULTS=1`) stages them in F´'s default `/tmp`, which nothing
  clears; the name is two numbers and `.tmp`, never one the ground chooses.
- **Remember every transaction let through, and keep it after it ends.** `cfdpManager` routes a Metadata by
  (source entity, sequence number) alone, whatever its class bit, and keeps the first destination it takes for a
  transaction (`r2RecvMd` runs once; after the FIN it drops Metadata). So the guard records every Metadata it lets
  through, class 1 and other destination entities included, and lets a second one for the same transaction through
  only if it names the same file. The destination the guard holds is then the one `cfdpManager` checksummed. A
  record is kept after its FIN (or after the sender cancels, an EOF with a condition code, which ends the
  transaction with no FIN), so a late copy of its Metadata, which a slow link can deliver after the FIN, changes
  nothing.
- **Commit only a clean Class 2 FIN.** The FIN must decode, and a `FinPdu` that failed to decode still reads
  NO_ERROR / COMPLETE / RETAINED, so that check comes first. It must point toward the sender, match a recorded class
  2 upload addressed to `cfdpManager`'s LocalEid by (source entity, sequence number, destination entity), compared
  as decoded values (Yamcs writes ids in 2 and 4 bytes, F´ in the fewest), and carry NO_ERROR, COMPLETE and
  RETAINED. The first such FIN commits and ends the record, so F´'s repeats (sent until the ground ACKs) commit
  nothing. `UPLOADS_COMMITTED` counts these announcements; Doom's `WadUplinked` or `WadUplinkFailed` says how the
  rename went. Class 1 has no FIN, so the guard lets it into the uplink directory but never commits it.
  `COMMIT_WAD`, with its size and checksum check, stays for class 1 and for commits by hand. It is also the
  ground's fallback when the guard's `WadUplinked` is lost on the way down: with the `.part` gone, it finds that
  this upload was put in place as `NAME.wad`, still with the size and checksum it names, and answers `WadUplinked`
  again (`docs/plans/idempotent-wad-commands.md`).
- **Report a refusal once per transaction.** The sender resends a Metadata each time the receiver NAKs for it,
  about ten times for a refused upload. `UploadRefused`, or `MetadataUnreadable` for a Metadata `cfdpManager`
  cannot read either, comes once per (source entity, sequence number), and `UPLOADS_REFUSED` counts uploads, not
  copies. Every copy is still refused.
- **Belt and braces.** `Doom::fileAnnounce_handler` now refuses any path outside the uplink directory as well.

## What ran (4 October 2026, this VM)

| Check | Result |
|---|---|
| Unit tests: `scripts/flight.sh ut` (GTest, 18) | All pass. They cover every refused shape of destination (the directory's name as a bare prefix included), a name with no nonce let through, an embedded NUL both ways, Yamcs-width ids, the file announced before its FIN is passed on, a truncated FIN, seven failed or partial FINs, class 1 (and a class 2 Metadata naming another file for its transaction), resends, an unreadable Metadata reported once per transaction, a second source with the same sequence number, a late Metadata after the FIN with the FIN repeated, a cancel, Metadata addressed to another entity, and a full table emptied in the right order. Eight one-line mutants of the table logic (forgetting at the FIN, no eviction order, ignoring a cancel, following only class 2, committing class 1, any destination entity, the source left out of either match) each fail at least one test. Three more, run on 5 October after a second review, fail too: the FIN passed on before the announce, and an unreadable Metadata reported on every copy or only once ever |
| Python suite | Passes, with `tests/test_cfdp_guard.py` (wiring, parser reuse, the shared name rule, the F´ branch the refusal relies on) and five demo tests with an on-board stand-in for the guard: the commit at the FIN, a lost `WadUplinked`, another upload of the same name (with this upload's rename at the FIN failing too), a failed rename at the FIN, and class 1 left to `COMMIT_WAD` |
| Class 2 upload, no loss | `MetadataReceived`, then `UploadCommitted`, then `WadUplinked`, then `RxFileTransferCompleted`. No `COMMIT_WAD` was sent; `LOAD_WAD` flew it |
| Uploads to `/tmp/…`, `<uplink>/../…`, `<uplink>/notawad.bin`, `<uplink>/.cfdp-tmp/x.wad.1.part` | Each was refused on board (`UploadRefused`, once per transaction although its Metadata came about ten times). The ground showed FAILED `NAK_LIMIT_REACHED` after 28.0 s. No file at any target; the bytes sat in `.cfdp-tmp` until the next start cleared them |
| Class 1 upload to `/tmp/…` | Refused on board, nothing written. The ground showed COMPLETED, because class 1 never hears back |
| Class 1 WAD upload | Not committed by the guard; `COMMIT_WAD` with its checksum put it in place, and it flew |
| `doom1.wad` (4.2 MB) at 5 % loss each way | FIN after 40.2 s, committed on board with no `COMMIT_WAD`, byte-identical; its first `LOAD_WAD` was lost and resent |
| Class 2 downlink at 5 % loss (`SendFile`) | Completed and byte-identical, every PDU through the guard |
| After the review fixes, on the build stacked on this branch (`feature/idempotent-wad-commands`, flight `2026_10_04-18_18_35`) | Class 2 uploads committed at the FIN, the demo matching this upload's own `UploadCommitted`. Uploads to `/tmp/…` and to `<uplink>.wad.1.part` (beside the directory) refused: ground FAILED `NAK_LIMIT_REACHED` after 28.0 s, no file at either target. Class 1 to `/tmp/…` refused, the ground showing COMPLETED. With the guard's events lost in a 12 s downlink blackout over the FIN, the file was committed all the same |

The rename happens while `cfdpManager` still holds the file open read-only, between the FIN and the FIN-ACK.
POSIX keeps the descriptor valid, and no error followed: `RxFileTransferCompleted` came as usual, naming the old
`.part`, which is only cosmetic.

## What it does not cover

- **Ground commands and parameters still name paths.** `fileManager.MoveFile`, `AppendFile`, `RemoveFile` and
  `CreateDirectory`, `cfdpManager.SendFile`, `PlaybackDirectory`, `PollDirectory` and the `ChannelConfig` parameter
  (`tmp_dir`, `move_dir`, `fail_dir`) take any path, and the guard never sees them. Anyone who can command the
  spacecraft can still read, write over (moving an uplinked WAD onto a file, for example) or delete any file the
  flight process can. Run the flight side as an ordinary user.
- **A refused upload costs a whole transfer.** The ground's failure reason, `NAK_LIMIT_REACHED`, reads like a link
  fault. The `UploadRefused` event and the `UPLOADS_REFUSED` channel say what happened. Injecting a FIN with
  `FILESTORE_REJECTION` would fail the transfer at once, but it means building PDUs in the guard and was not done.
- **8 transactions are remembered at once** (`MAX_UPLOADS`). `cfdpManager` does not stop at `MaxSimultaneousRx`
  (5): in F´ v4.3.0 that check in `Engine::startRxTransaction` is commented out, and any of the channel's 50
  transactions can be a receive. When the table is full, a transaction that has ended goes first, then one no FIN
  can settle (class 1, or addressed to another entity), and only then the oldest upload still waiting for its FIN
  (`UploadForgotten`), whose FIN then commits nothing; `COMMIT_WAD` can still commit it. The ground sends one upload
  at a time (Yamcs `maxNumPendingUploads: 1`), so that last case needs nine uploads at once from elsewhere.
- **A sender that breaks CFDP within a transaction** can still make the guard's record differ from `cfdpManager`'s
  destination, for example by pushing a live upload out of the table with eight others and then naming another
  file for it. Every destination it can reach still passed the confinement, and such a sender could as well send
  `COMMIT_WAD` or `SendFile`.
- **The temp files of refused class 2 uploads** stay until the next start (in `/tmp`, and never cleared, when
  flying without the parameter file).
- **Read, not run.** If a file-data PDU's body fails to decode before any Metadata, F´ ends the transaction with
  no error status and could send a NO_ERROR FIN for the temp file. The guard has no Metadata for it, so it commits
  nothing. If a Metadata did arrive and the file is short, `r2CalcCrcChunk` would loop. Yamcs never sends such a
  PDU, and TC frames are FECF-checked.
- **On `feature/wad-uplink`** (the native build) there is no guard, and FileUplink calls `fileAnnounce` itself.
