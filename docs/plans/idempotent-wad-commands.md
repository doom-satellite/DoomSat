# COMMIT_WAD and LOAD_WAD, safe to send again

_PR numbers here (#9 and #10) are those of PaulMRamirez/DoomSat, where this work was reviewed._

A command goes up as one unprotected TC frame (no COP-1: `docs/plans/cop1-investigation.md`, #10), so on a lossy
link the ground resends a command when no answer comes. The demo does this with `--tries`, and the dashboard's
`LOAD_WAD` form does it (#9). Often only the answer was lost, and the command had already run. Before this change, a
resend did one of two things:

- **`COMMIT_WAD`** found no `.part` and answered `WadUplinkFailed`. That is the same answer as for a file that never
  arrived. The demo then had to let `LOAD_WAD` decide, and `LOAD_WAD` would fly an older file of the same name if
  there was one.
- **`LOAD_WAD`** proved and switched the game a second time. Or, if it arrived during the first one's proof, it was
  refused (`WadLoadFailed`, "another LOAD_WAD is still being checked").

Now a repeat changes nothing and gets a definite answer: a repeated `COMMIT_WAD` answers `WadUplinked` again (until
a flight restart, after which it fails closed; see Limits), and a `LOAD_WAD` for what already flies answers
`WadAlreadyFlying`. A repeat during the proof shares the first one's answer.

## What changed

- **`COMMIT_WAD` (`Doom.cpp`).**
  - If the `.part` is there, nothing changed: it is checked against the size and CFDP checksum and renamed, or
    refused.
  - If the `.part` is gone, the command now asks whether this upload was put in place. The Doom component keeps,
    under a lock (the guard's commit runs on `cfdpManager`'s thread), which `.part` each `NAME.wad` was last renamed
    from since start, for eight names (see Limits). If it was this `.part`, and `NAME.wad` still has the size and
    checksum named, the answer is `WadUplinked` and OK, the same answer the first commit gave, or the guard's
    commit at the FIN.
  - Anything else is still `WadUplinkFailed` with EXECUTION_ERROR: not arrived, or a `NAME.wad` from another upload
    or changed since. A size and checksum alone would not do: the CFDP checksum is a sum of 4-byte words, and an
    older `NAME.wad` with its words in another order (lumps moved around inside a WAD) matches both.
  - A `.part` that is present but wrong is still refused, even when a matching `NAME.wad` exists, because the
    command names the `.part`.
  - If the `.part` checks out but its rename fails, the same question decides the answer. That covers a
    `COMMIT_WAD` racing the guard's commit at the FIN.
- **`LOAD_WAD` (`payload/doom_payload.py`, `payload/wad_uplink.py`).** The payload resolves the request first, then
  compares it with `wad_uplink.load_key`: each file's name (through any link) and identity (device, inode, size,
  modification time), and the map. What flies is remembered as it was when the game was built on it (at start, and
  at each switch), never looked up again from its name: without a pin (another file system), a new upload of the
  same name renamed over the path must still load.
  - A request for what is flying now is answered with the new result `ALREADY`, which the Doom component logs as
    a new event, `WadAlreadyFlying` ("Already flying … on …: LOAD_WAD changed nothing"). `WAD_LOADS` does not move,
    the level is not restarted, and no new level is announced (`m_lastLevel` is kept). Not `WadLoaded`: that means
    a switch to everything that reads it, and the science data system on `main` takes a `WadLoaded` just before
    an `EpisodeStarted` for a switch that ended the episode (`ground/sds/doomsat_sds/episodes.py`); a no-op load
    followed within 10 s by a `RESET_GAME` would otherwise be cataloged as a `wad_switch`.
  - A request equal to the one being proven gets no answer of its own. The proof's answer serves both.
  - Any other request during a proof is refused, as before.
  - A new upload of the same name is a new file, renamed over the old one, so it gets a new key and is loaded.
  - "What is flying now" includes the current map. If the level has moved on, a repeat flies the map it asks for
    again. `RESET_GAME` restarts the current level.
- **`tools/wad_uplink_demo.py`.**
  - A `WadUplinkFailed` now fails the run. A repeat that found the file in place would have said `WadUplinked`,
    so there is no longer an "unconfirmed, let `LOAD_WAD` decide" case.
  - It takes `WadAlreadyFlying` as an answer. When `WAD_LOADS` has moved since before the first `LOAD_WAD`, an
    earlier copy switched the game and had its answer lost, and the run goes on to the frame from after the
    switch; otherwise it reports "already flying".
- **The dashboard** (#9) takes `WadAlreadyFlying` as an answer too, and shows it as such.

## Tests

| Suite | What |
|---|---|
| `scripts/flight.sh ut`: Doom GTest suite, 11 tests, new (`flight/Components/Doom/test/ut`) | `COMMIT_WAD` against real files in a scratch `$DOOMSAT_HOME`: a commit; a repeat, twice; a repeat after the guard's `fileAnnounce`; an older `NAME.wad` with other bytes, another size with the same checksum, or its words reordered (same size and checksum); a repeat naming another upload of the same bytes; a repeat with the same size and another checksum; a repeat after `NAME.wad` changed, or after another upload replaced it; nothing on board; a wrong `.part` (other bytes, or another size with the same checksum) with and without a matching `NAME.wad`; a rename that fails; names that are not bare. Also: `fileAnnounce` outside the uplink directory, and every WAD report result, including `ALREADY` keeping the level. Eight mutants (any upload taken for this one, no record, a rename not recorded, no size check on either path, a doubled failure event, no repeat answer, `ALREADY` restarting the level) each failed a test. A ninth, no checksum check on the repeat, fails the repeat with the same size and another checksum, a step added in review |
| `scripts/flight.sh ut`: the guard's 18 | Still pass (`docs/plans/cfdp-guard.md`). Both suites run under the address, undefined-behaviour and leak sanitizers |
| `tests/test_payload_load_wad.py`, 11 tests, new | The payload's own `request_wad` and `poll_wad`, with the game and the probe child stood in for: a repeat after the switch, a repeat during the proof, another request during the proof, a new upload of the same name (with and without a pin), the game as launched (and under a linked name), another map, and a refusal, a failed proof and a failed switch, none of them remembered as flying. They run in both venvs: in the ground venv the payload is imported with empty stand-ins for ViZDoom and Pillow |
| `tests/test_wad_uplink.py` | `load_key` (5 tests), the result codes against `Doom.cpp`, and the payload and flight branches pinned as text: their order, that a repeat during the proof sends nothing of its own, that `poll_wad` remembers what flies (the two failure tests above pin that it does so only at a switch) |
| `tests/test_wad_uplink_demo.py` | The stand-in stack now does what the flight software and payload do. New tests: a repeat commit answers as the first did; an older file of the same name is never taken; a load of what is flying; a load resent after its answer was lost takes the repeat's answer; an answer naming other files fails even with the count unmoved; a `WadAlreadyFlying` whose count comes down late is still a switch, and a `WadLoaded` is never called "already flying" |

## What ran live

The build before the no-op got its own event, on top of the reviewed guard (4 October 2026, flight
`2026_10_04-18_18_35`, behind `tools/lossy_relay.py`):

| Check | Result |
|---|---|
| A `.part` in place as class 1 leaves it (`cigrc.wad`), `COMMIT_WAD`, then the same command again | `WadUplinked …/cigrc.wad` and `OpCodeCompleted`, both times |
| `COMMIT_WAD` naming another upload of the same bytes (`cigrc.wad.1.part`, never uplinked) | `WadUplinkFailed`, `OpCodeError … EXECUTION_ERROR` |
| This upload's repeat with the checksum off by one | `WadUplinkFailed` |
| An older `cigro.wad` put on board by hand with two 8-byte runs swapped (the same size and checksum), and a `COMMIT_WAD` for an upload that never came | `WadUplinkFailed`: not taken for the upload |
| A `.part` as class 1 leaves it, `COMMIT_WAD` with the downlink blacked out (TM 100 %), then sent again | The first renamed it on board, and its answer was lost. The resend got `WadUplinked …/cigrep.wad` and `OpCodeCompleted` |
| The same `LOAD_WAD` three times, 0.1 s apart (`cig.wad` over `freedoom2.wad`, MAP02) | The payload absorbed both repeats while proving. One `WadLoaded`, `WAD_LOADS` +1, `EPISODE` +1 |
| The same `LOAD_WAD` once more | Answered (then as `WadLoaded`; `WadAlreadyFlying` since, see below). `WAD_LOADS` and `EPISODE` did not move, and frames kept coming. Payload: "already flying it; nothing to do" |
| `doom1.wad` on E1M2, then E1M3 | Two switches, `WAD_LOADS` +1 each |
| `LOAD_WAD doom1.wad E1M1` with the downlink blacked out, then the downlink restored and the same command resent | Nothing heard for 25 s. `WAD_LOADS` had already moved by 1. The resend was answered (then as `WadLoaded`; `WadAlreadyFlying` since), and `WAD_LOADS` was +1 in all |
| Demo with the downlink blacked out from full size for 12 s (`cigfe.wad`) | The guard committed it on board during the blackout, and its `UploadCommitted` and `WadUplinked` were lost. The demo sent `COMMIT_WAD` 5 s after the FIN and got `WadUplinked` from the repeat, then `LOAD_WAD` flew it. Before this change, that repeat answered `WadUplinkFailed` |
| Demo, 5 % loss each way (seed 31), `cig.wad` as `cigs1`–`cigs3` | 3 of 3 OK, each committed on board at the FIN. In one, the `WadLoaded` was lost and the WAD channels stood in for it |
| Demo for what was already flying (`--pwad cigs3.wad --map map02`), same link | Answered, `WAD_LOADS` still 9: "OK: already flying" |

Then, with `main` merged in and the no-op answer made its own event (flight `2026_10_04-21_44_46`): the five
`COMMIT_WAD` checks above again OK; three `LOAD_WAD` 0.1 s apart gave one `WadLoaded` and one switch (`EPISODE`
1 → 2), the repeats absorbed while proving; once more gave `WadAlreadyFlying` with nothing moved; with the downlink
blacked out, `LOAD_WAD doom1.wad E1M1` switched on board (its `WadLoaded` lost) and the resend was answered
`WadAlreadyFlying`; and the demo for what was flying printed "OK: already flying".

An earlier build of this branch (flight `2026_10_04-16_46_37`, before its review) ran the same checks and four demo
runs at TC 5 % / TM 30 %, all OK; one of those lost its first `LOAD_WAD` answer and resent it.

Seen along the way, not caused by this change: `cig.wad` over `freedoom2.wad` on MAP01 killed the probe child with
signal 11. The payload refused the load and kept flying what it had, which is what the probe is for.

## Limits

- **Older flight builds.** One built before this change answers a repeat `COMMIT_WAD` with `WadUplinkFailed`, and
  the demo now fails on that. That errs on the safe side. Compare `NAME.wad` on board with the file sent byte
  for byte (`cmp` on the flight machine; a matching `--checksum FILE` alone is not proof, see below) and send
  `LOAD_WAD`; commit by hand only if the
  `.part` is still there. The other way round, this payload with an older flight build, a load of what is already
  flying gets no answer at all: the old Doom component has no `ALREADY` and logs nothing for it. The flight
  software and the payload come from the same checkout; rebuild with `scripts/flight.sh build`.
- **A restart between a commit and its repeat.** The record of which upload each `NAME.wad` came from is kept in
  memory, so after a restart a repeat answers `WadUplinkFailed`, and the demo fails, although the file is in place.
  That errs on the safe side: `LOAD_WAD` shows what is there.
- **Eight names are remembered** (`PLACED_MAX`). A new name takes the next slot in turn, in the order names were
  first put in place, so a name put in place again keeps its old slot, and as few as two new names can displace
  its record. A repeat whose record was displaced answers `WadUplinkFailed`, although the file is in place: it
  fails closed, as after a restart.
- **What the checksum can tell apart.** The first commit's check of the `.part`, like CFDP's own check of the whole
  transfer, is the CFDP modular checksum, a sum of 4-byte words: two files of the same size whose aligned words are
  the same in another order pass it alike. The repeat answer does not rely on it alone; it names the upload.
- **What makes two files the same for `LOAD_WAD`.** Identity is device, inode, size and modification time, taken
  when the game loads the file and kept, never looked up again from the name. A file rewritten in place with the
  same size and time would count as the same file. Nothing on board does that: an upload is renamed over the old
  file.
- **A no-op whose answer is lost.** Telemetry cannot stand in for it, because the count does not move. The ground
  asks again, and each repeat is answered. The dashboard (#9) gives up after three unanswered tries, as it would
  for any load.
- **Other commands.** `RESET_GAME`, `EXPLORE_HINT` and the relative turn in `CONTROL` are still not safe to
  repeat. Nothing resends them. Changing them would change the pilot's behaviour, which goes through the experiment
  ledger (`research/PROGRAM.md`).
