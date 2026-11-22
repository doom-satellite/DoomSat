# Task: in-flight WAD uplink for DoomSat, Stage 1 (native F Prime file transfer)

You are working in a fork of `Devonance/DoomSat`: Doom runs as a spacecraft payload behind F Prime flight software, CCSDS framing, Yamcs, and a dashboard. Read `CLAUDE.md`, `README.md`, and section 2 of `docs/CHARTER.md` before anything else.

## Goal

An operator can send a new level to the running spacecraft and switch the game to it without restarting anything. The WAD file goes from a Yamcs bucket, up the real command link as F Prime file packets, onto the flight filesystem; a new `LOAD_WAD` command then tells the payload to restart the game on it. Today the WAD is fixed at launch by `--wad`.

## First actions

1. Create branch `feature/wad-uplink`. Save this whole message verbatim as `docs/plans/wad-uplink-stage1.md` and commit it, so the spec survives context compaction. Keep a short "Progress and findings" section at the bottom of that file up to date as you work.
2. Start the install in the background, because the F Prime build takes 5 to 10 minutes: `scripts/flight.sh setup` then `scripts/setup_ground.sh python`. Skip the Open MCT build. If any download or clone is refused by the network or GitHub proxy, stop and tell me exactly which URL failed; do not work around it.
3. While it builds, read the code and write your plan into the plan file. Use subagents where it helps (flight component, payload, ground and tests are separable).
4. Prove the baseline before changing anything: `python3 tools/doctor.py`, the headless Doom check (`payload/play.py --check`), `scripts/flight.sh start` then `scripts/flight.sh check` after about 40 seconds, and the unit tests with `ground/.venv/bin/python -m unittest discover -s tests`. Record the results in the plan file. If the baseline does not work here, fixing that comes first.

No TypeSafe key is available. Fly with the code rules: `scripts/play.sh --autopilot` or `scripts/start_pilot.sh --system-one code --system-two none`.

## What is already there

I read these at commit `fe2666b` (24 September 2026). I did not build or run the stack, so treat them as strong leads and confirm each one.

- `flight/DoomSat/Top/topology.fpp` imports the `FileHandling` and `DataProducts` subtopologies. The router's file port is wired to `FileHandling.fileUplink`, and `fileDownlink` is wired to the com queue.
- `ground/yamcs/etc/yamcs.fprime-project.yaml` loads `com.example.myproject.FprimeFilePacketService` from fprime-yamcs 0.2.1. It implements Yamcs's `FileTransferService`, so it shows up under `/api/filetransfer/fprime-project/services` and in the Yamcs web File Transfer page. Uplink reads an object from a bucket and sends Start, Data, End `Fw::FilePacket`s on APID 3, 512 bytes per chunk (`uplinkChunkSize`), with a 20 ms sleep per packet. There is no retransmission.
- The payload (`payload/doom_payload.py`) resolves `--wad` once in `__init__` via `_find_wad` and builds the game in `_make_game` with `set_doom_game_path`. `new_episode` already throws the world model away, as the charter requires.
- The Doom component (`flight/Components/Doom/`) forwards each command to the payload over a local TCP socket as a small record: `'D'`, kind byte, 16-bit length, body. Uplink kinds `0x10` to `0x15` are taken. Payload-to-flight kinds are `1` (status) and `2` (frame). Command opcodes `0x00` to `0x05`, event ids 0 to 11, and telemetry ids 0 to 84 are taken.
- The ViZDoom 1.3.0 wheel ships test WADs inside the payload venv, under `site-packages/vizdoom/`: `freedoom1.wad` and `freedoom2.wad` (28.8 MB each) and `scenarios/*.wad` from 2.7 KB (`basic.wad`) to 471 KB (`cig_with_unknown.wad`). The scenarios are PWADs meant to load over a Doom II compatible IWAD.

Expected uplink times at the current settings (about 25 KB/s): a scenario WAD takes seconds, `doom1.wad` (4.2 MB) about 3 minutes, `freedoom2.wad` about 19 minutes.

## Design

Treat this as the intended shape. If the code tells you a better one, say why in the plan file and proceed.

**Flight component.** Add `LOAD_WAD(iwad: string, pwad: string, map: string)` to `Doom.fpp` at opcode `0x06`. `pwad` empty means none. The operator states which file is the IWAD and which is the PWAD, so nothing has to open a file to find out. The handler follows the pattern of `RESET_GAME`: pack a record of a new kind (`0x16`) and send it to the payload. Add a payload-to-flight record of kind `3` for the result, and turn it into events `WadLoaded(name, map)` and `WadLoadFailed(name, reason)`, plus telemetry for the active WAD name and a count of loads. Prefer the new record kind over extending the status record, because `STATUS_LEN` in `Doom.cpp` and `STATUS_FMT` in the payload must match byte for byte.

**Payload.** On kind `0x16`: validate the names, close the game, rebuild it through `_make_game` with the new paths (`set_doom_game_path` for the IWAD, `set_doom_scenario_path` for a PWAD), reset carried loadout and the level counter, and call `new_episode`. If ViZDoom fails to start on the new file, restore the previous WAD and report failure. Report the outcome with a kind `3` record.

**Where files land.** One uplink directory, created by the run script (for example `$DOOMSAT_HOME/wads/uplink`). `LOAD_WAD` accepts basenames only: no path separators, no `..`, must end in `.wad`, and must resolve inside the uplink directory or the installed `wads` directory. Decide how to avoid loading a file while it is still arriving (upload to a temporary name and rename with a FileManager command, or check for the FileUplink completion event) and document the choice.

**Ground.** Add `tools/wad_uplink_demo.py`: put a WAD in a Yamcs bucket, start the upload through the file transfer service, wait for completion, issue `LOAD_WAD`, and confirm the switch from telemetry and events. Use `yamcs-client` from `ground/.venv`. Add a small panel to `ground/dashboard/index.html` showing the active WAD with a control that issues `LOAD_WAD` through the existing proxy (see the `issue()` function). Add the WAD name to `scripts/wsl_check.sh`. An Open MCT display is a stretch goal only if you can build and verify it.

## Constraints

1. **The honesty suite must keep passing**, including `python research/honesty.py --canary`. Pilot-side code (the files listed as `PILOT_SIDE` in `research/honesty.py`) must never open a WAD. `LOAD_WAD` only hands paths to ViZDoom; use `os.path` for existence checks. Map names in pilot-side code are checked by a regex, and a line that passes one through as a launch parameter needs the launch context the suite expects (see `LAUNCH_CONTEXT`).
2. **Do not edit** `research/`, `docs/CHARTER.md`, `knowledge/`, or the decision logic in `ground/pilot.py`. If a change there seems necessary, stop and explain.
3. **Uplinked-WAD flights are demonstrations, never scored.** Do not run the bench or any graded flight on an uplinked WAD. The dev set is frozen to Freedoom Phase 1. Put a suggested charter note in the pull request description instead of editing the charter.
4. **Default behaviour is unchanged.** With no `LOAD_WAD` commanded, a flight launches and behaves exactly as before.
5. **Never commit a WAD.** Tests copy fixtures from the installed ViZDoom package or generate them at test time. Unit tests must still run with no network and no game.
6. **Small diffs.** Many files use CRLF line endings; keep each file's existing endings and style. `scripts/wsl_sync.sh` copies an explicit list of flight files, so add any new flight file to it. Check whether the committed `ground/yamcs/mdb/fprime.xtce.xml` needs regenerating after the dictionary changes.
7. **Tooling.** Keep the repo's own venv scripts. Give any new standalone tool PEP 723 inline metadata so it also runs with `uv run`.
8. Keys and tokens never go in git, logs, or commit messages.

## Acceptance criteria

1. Unit tests, the honesty suite, and the canary all pass. New unit tests cover the record encoding and decoding and the name validation.
2. End to end with the stack running under the code autopilot: `tools/wad_uplink_demo.py` uplinks `scenarios/basic.wad`, loads it over `freedoom2.wad`, and you show the `WadLoaded` event text, the WAD telemetry value, an incremented `EPISODE`, `FRAMES_SENT` still rising, and one frame saved to `out/` from after the switch.
3. An IWAD swap works: start the flight on `freedoom1.wad`, uplink `doom1.wad` under a new name, load it on `E1M1`.
4. Each negative case produces `WadLoadFailed` with a reason and the game keeps running on the previous WAD: a truncated file, a name that does not exist, a path traversal attempt, a wrong extension, and a load requested during an unfinished uplink.
5. Commands still work during a multi-minute uplink. Measure `CONTROL` round-trip latency before and during the `doom1.wad` uplink and report both numbers.
6. Docs: a short "Uplink a new level" section in `README.md`, a note in `docs/ARCHITECTURE.md`, and the new command in the `CLAUDE.md` tables.

Optional if time allows: uplink `freedoom2.wad` (about 19 minutes) in the background while flying, and check Yamcs bucket size limits for a file that large.

## Assumptions to check early

- That the Yamcs file transfer API accepts an upload to a spacecraft path of your choosing with this service, and where FileUplink writes relative paths (the flight binary's working directory).
- That ViZDoom scenario PWADs run with this payload's buttons, automap, and status code. If they do not, generate a minimal one-room test map instead and say so.
- That closing and re-creating the `DoomGame` inside the running payload loop is safe.
- Which FileManager commands exist at F Prime v4.3.0 for rename and checksum.

## Working in this environment

The VM pauses when idle and background processes die, so check `scripts/flight.sh status` before each test run and restart with `scripts/flight.sh start` when needed (Yamcs needs about 30 seconds). I cannot open `localhost` from my phone, so show evidence as text and saved images.

## Finish

Push `feature/wad-uplink` to this fork and open a draft pull request **inside the fork only**. Do not open a pull request against `Devonance/DoomSat`; I will do that. End with a report that separates what you verified by running from what you only read, lists every deviation from this spec, and notes anything worth raising upstream with fprime-yamcs or F Prime.

---

## Progress and findings

_Kept up to date by Claude during the session. Newest first within each section._

### Status

- [x] Branch `feature/wad-uplink` created from `fe2666b`, spec saved here.
- [x] Install (`scripts/flight.sh setup`, `scripts/setup_ground.sh python`), after one fix (below)
- [x] Plan (below: design as built, and where it departs from the spec)
- [x] Baseline (below)
- [x] Implementation
- [x] End to end (below: acceptance criteria 1 to 5, and the optional large-file check)
- [x] Docs (criterion 6)
- [x] Review pass (two rounds, below), push, draft PR

### Cloud environment (checked 2026-10-03)

Session runs in environment `doomsat-flight` (`env_016fDJXXdxUQxZyWfF7Gov2D`). Verified from inside the VM:
`BASH_DEFAULT_TIMEOUT_MS=600000`, `BASH_MAX_TIMEOUT_MS=1200000`; 4 vCPU, 15 GB RAM, 30 GB free disk;
Python 3.11.15 with `venv`; `build-essential` and `binutils` installed; `deb.debian.org`, PyPI, Ubuntu apt,
`raw.githubusercontent.com` and `nodejs.org` reachable; `git ls-remote` of `nasa/fprime` and
`fprime-community/fprime-yamcs` works through the proxy (the github.com web UI returns 403, which does not
affect git).

### Install (2026-10-03)

- `scripts/flight.sh`, `start_pilot.sh`, `wsl_*.sh` and others are committed without the executable bit (mode
  100644), so `scripts/flight.sh setup` fails with `Permission denied` in a fresh clone. Running them as
  `bash scripts/...` works; CLAUDE.md and the README call them directly. Left unchanged (mode change only;
  worth raising upstream).
- A transient PyPI read error (`IncompleteRead`, not a proxy refusal) killed `fprime-bootstrap`'s
  `pip install -r lib/fprime/requirements.txt` half way, and the bootstrap carried on, so CMake later failed
  in `lib/fprime/cmake/required.cmake:30` (no fpp tools). Fixed by re-running that pip install with
  `--retries 10`, then `scripts/flight.sh setup fprime`.
- **Bug fixed (`f4f7c93`)**: `scripts/wsl_sync.sh` was not re-runnable. `grep -q "/config" ... || { ...; } > .cm
  && mv .cm ...` parses as `(grep || ...) && mv`, so once the config line exists the `mv` runs on a file
  nothing wrote and `set -e` stops every later `flight.sh build` and any re-run of `flight.sh setup fprime`.
- No URL was refused. Freedoom's GitHub release asset downloaded fine; `deb.debian.org` served `doom1.wad`.
- Versions in `fprime-venv`: fprime-tools 4.3.0, fprime-fpp 3.3.0, fprime-gds 4.4.0 (pulled up by
  fprime-yamcs 0.2.1, which needs `>=4.4.0a3`), fprime-yamcs 0.2.1, fprime-xtce 0.2.0 (the PR branch),
  yamcs-client 1.13.0. First F´ build took about 3 minutes on 4 vCPUs.

### Baseline (2026-10-03, before any change)

| Check | Result |
|---|---|
| `python3 tools/doctor.py` | all ok except `Open MCT built` (skipped on purpose) and no TypeSafe key (expected) |
| `payload/play.py --check` | `vizdoom 1.3.0: doom1.wad E1M1 ran 100 tics, health 100; screenshot out/doom_check.png` |
| `scripts/flight.sh start`, `check` after 40 s | 4 processes up; `FRAMES_SENT` 203 and rising, `PAYLOAD_LINK True`, `UDP_TM_IN` 1717 frames; XTCE loaded 194 parameters and 55 commands |
| `ground/.venv/bin/python -m unittest discover -s tests` | 369 tests OK |
| `research/honesty.py --canary` | 17 checks, 0 failed |
| `start_pilot.sh --system-one code --system-two none --duration 40` | 160+ decisions, `CMDS_RECEIVED` 163, frames ok=325 lost=1 |

### Assumption probes (payload venv, no flight software)

- **Scenario PWADs run with this payload's setup.** `basic.wad` over `freedoom2.wad` on `MAP01`, with
  `set_doom_scenario_path`, ran 300 tics through `observe`, `action` and `pack_status` without error.
- **Closing and re-creating the `DoomGame` in one process is safe.** Four `close()` / `_make_game()` /
  `new_episode()` cycles alternating `freedoom1.wad` and `doom1.wad`: 0.4 to 0.6 s each, flies normally after.
- **A truncated WAD kills the process.** `doom1.wad` cut to 12 bytes, 100 KB or 2 MB: ViZDoom prints
  `Failed to allocate memory from system heap` and the Python process dies with SIGSEGV (exit 139). Nothing
  to catch. So the payload must never hand an unproven file to its own game: each candidate is first
  launched in a short-lived child process, and only a clean child result lets the swap happen.

### Plan: the design as built

Five parallel code readers (flight, payload, honesty, ground, F´ v4.3.0) checked the leads above. Every lead
held. The design follows the spec, with these additions and departures (reasons from code that was read or run):

| Decision | Why |
|---|---|
| **Race-free arrival: the Doom component renames on FileUplink's `fileAnnounce`.** The ground uploads to `NAME.<nonce>.part`. FileUplink calls `fileAnnounce` only after the End packet's checksum matches, and the Doom component (new `sync input port fileAnnounce: Svc.FileAnnounce`, wired from `FileHandling.fileUplink.fileAnnounce`, previously unconnected) renames it to `NAME` with `rename(2)` (event `WadUplinked`). | The spec offered a FileManager rename or checking for the completion event. A FileManager `MoveFile` cannot do it: F´ v4.3.0 caps every command string argument at `FW_CMD_STRING_MAX_SIZE` = 40 on board (FORMAT_ERROR above), and `/root/doom/wads/uplink/freedoom2.wad.part` is already 41. FileUplink also writes into the final path while the file arrives, keeps a file that failed its checksum, and opens without `O_TRUNC`, so re-uplinking a shorter file to the same name leaves the old tail and still passes the checksum. A fresh `.part` name per uplink plus a rename only on verified arrival covers all of these. LOAD_WAD accepts only names ending `.wad`, so a `.part` can never be loaded; a request for a name whose `.part` is present says "has not finished its uplink". |
| **The payload proves a WAD in a child process** (`doom_payload.py --probe`, the same `_make_game`, `new_episode`, `observe`, `action` and `pack_status`, flown for one second) **before touching its own game.** | Probe: a truncated WAD does not raise in ViZDoom 1.3.0. `init()` prints "Failed to allocate memory from system heap" and the process dies with SIGSEGV (exit 139), so the spec's "if ViZDoom fails, restore the previous WAD" cannot be a try/except. A damaged PWAD can instead hang while loading, and a map missing from the WAD hangs `new_episode()`. The child runs in its own session and its whole process group is killed when it ends (its engine is a separate process that otherwise outlives it); it also kills itself if the payload dies first; 15 s timeout. Only a clean exit 0 lets the swap happen. |
| **The new `DoomGame` is built before the old one is closed.** | A build that fails leaves the old game flying, untouched. |
| **Record kind 3 carries `result:u8 loads:u16` then five texts: the IWAD and PWAD now running, the requested name, the map, the reason.** It is also sent once on connect (`result` 0, REPORT). | One record has to carry both what is running (telemetry) and what was asked for (the failure event). The report on connect, plus a once-a-second repeat in the Doom component, lets a ground that joins late see the WAD. Before that repeat the channels read `None` after a restart, because the payload reports before Yamcs is listening. |
| **Telemetry: `WAD_IWAD` and `WAD_PWAD` are `[40] U8` arrays with `@ !binary`, zero-padded; `WAD_LOADS` is U16.** One channel `WAD_NAME` became two. | The repo already documents that F´ string telemetry does not decode in Yamcs: fprime-xtce gives it a fixed size while F´ sends it length-prefixed. The `!binary` byte array is the mechanism `FrameChunk` already uses. |
| **Events: `WadLoaded(name, map)`, `WadLoadFailed(name, reason)`** as specified, with `name` like "basic.wad over freedoom2.wad". Plus `WadUplinked(fileName)` and `WadUplinkFailed(fileName)`. | |
| **`LOAD_WAD(iwad: string size 40, pwad: string size 40, map: string size 10)`.** | Yamcs counts the two-byte length tag against a string argument's declared size, so 40 carries 38 characters (the payload's limit) and 10 carries the 8 of a map lump name. |
| **With a PWAD loaded, finishing a level flies the same map again.** | `next_map()` names the IWAD's next map: over freedoom2 the game drifted into freedoom2's MAP02 while telemetry still said basic.wad, and over freedoom1 `new_episode()` on a map that doesn't exist hangs the live payload. With no PWAD (every scored flight) nothing changes. |
| **The demo is a ground tool in `tools/`** (developer-side for the honesty suite) and reads the WAD bytes only to put them in the bucket. | `research/honesty.py` scans `PILOT_SIDE` only; `tools/` "may read the WAD". `payload/wad_uplink.py` is not in `PILOT_SIDE`, and it opens nothing anyway; a unit test holds it to the suite's patterns. |
| `LOAD_WAD` is refused while `--oracle` is on. | The diagnostic ladder computes exits from the launch WAD and level. |
| **Departure from constraint 4 (default behaviour unchanged): one space packet per TC frame** (`multiplePacketsPerFrame: false`, `f3d2c65`). It applies to every flight, with or without `LOAD_WAD`. | Yamcs packed queued commands into one frame, and F´ v4.3.0's `SpacePacketDeframer` keeps only the first packet of a frame. 100 `CONTROL`s from 10 threads: 81, 71 and 64 arrived on board before the change, and 100, 100 and 100 after. The lost packet behind an earlier `BadChecksum` was a file packet that shared a frame with a pilot `INTENT`. **Flight-stack numbers from before and after this commit are not directly comparable** (commands honoured, `CMDS_RECEIVED`, latency under load). It is a separate commit, so it can go in its own PR. |
| Also outside the feature: the payload closes its game on SIGTERM, and `stop` then SIGKILLs a payload stuck inside ViZDoom (`9b2c449`, `4419b22`); `maxContentLength` 32 MiB in `yamcs.yaml`. | Every restart used to orphan a ViZDoom engine. The 32 MiB limit admits a whole IWAD to a bucket (see the optional row below). |

Flight/payload record layouts (big-endian, as every existing record):

    flight -> payload  'D' 0x16 len | iwadLen iwad | pwadLen pwad (0 = none) | mapLen map
    payload -> flight  'D' 0x03 len | result (0 REPORT, 1 LOADED, 2 FAILED) | loads:u16 |
                        iwadLen iwad | pwadLen pwad | nameLen name | mapLen map | reasonLen reason

### End-to-end results (all run on this VM, 2026-10-03)

| Criterion | Result |
|---|---|
| 1. Unit tests, honesty, canary | `unittest discover -s tests`: 412 tests OK (369 before; 38 new in `tests/test_wad_uplink.py`, 5 in `tests/test_wad_uplink_demo.py`, plus `WAD_CHANNELS` in `test_runner.py`). `honesty.py --canary`: 17 checks, 0 failed. |
| 2. `basic.wad` over `freedoom2.wad` under the code autopilot | One pilot (`--system-one code --system-two none`) flying. The 2704 bytes went up in 1.0 s, then `[FileReceived]`, then `[WadUplinked] Uplinked WAD ready to load: /root/doom/wads/uplink/basic.wad`. `LOAD_WAD` gave `[WadLoaded] Now flying basic.wad over freedoom2.wad on MAP01` about 2 s later. Telemetry: `WAD_IWAD='freedoom2.wad' WAD_PWAD='basic.wad'`, `WAD_LOADS 0 -> 1`, `EPISODE 1 -> 2`, `FRAMES_SENT 311 -> 331`. Frame `out/wad_uplink_basic_MAP01.jpg` shows basic.wad's room and its Cacodemon, the player at 100% health. |
| 3. IWAD swap | A fresh flight on `freedoom1.wad`. `doom1.wad` went up as `shareware.wad`: 4,196,020 bytes in 167.7 s (25.0 KB/s), and the md5 on board matches `doom1.wad`. `[WadLoaded] Now flying shareware.wad on E1M1`, `WAD_IWAD='shareware.wad'`, `EPISODE 1 -> 2`. The saved frame is E1M1's opening hangar. |
| 4. Negative cases (all `WadLoadFailed`; `WAD_IWAD`, `WAD_LOADS` and `EPISODE` unchanged; `FRAMES_SENT` rising) | Truncated (200 KB of doom1.wad): "the game would not start on it (killed by signal 11: Failed to allocate memory from system heap)". Missing: "nothere.wad is in neither the uplink nor the installed WAD directory". Traversal: "IWAD must be a bare file name, not a path", and the same for a PWAD `../uplink/shareware.wad`. Wrong extension: "IWAD is not a .wad file". During an unfinished uplink (11,776 of 4,196,020 bytes): "shareware.wad has not finished its uplink (shareware.wad.1791067902.part so far)". The uplink then completed and loaded. Extra case, a map not in the WAD (`MAP01` on shareware): refused after the timeout, "is it in that WAD?". |
| 5. Commands during a multi-minute uplink | `CONTROL` round trip, measured as the time from issuing the command to `CMDS_RECEIVED` arriving back one higher (command response is not downlinked), no pilot. Idle: median 103 ms (78 to 164), 30/30. During a 167 s `doom1.wad` uplink, sampled every 5 s across it: median 107 ms (39 to 150), 30/30. A first run, 20 samples in the first 12 s of the uplink: 113 ms idle and 102 ms during. |
| 6. Docs | README "Uplink a new level"; `docs/ARCHITECTURE.md` (what is proven, findings 7 to 10); CLAUDE.md (tables, a gotcha). |
| Dashboard | Level file panel, checked headless with Playwright. The form's `LOAD_WAD freedoom2.wad on MAP01` gave `[WadLoaded] Now flying freedoom2.wad on MAP01`, and a missing name gave the reason. |
| Optional: freedoom2.wad (28.8 MB), after raising `maxContentLength` | **Done while flying.** One code autopilot flew throughout. 28,787,748 bytes went up as `fd2up.wad` in 1149.9 s (19.2 min, 25.0 KB/s), and the md5 on board matches `freedoom2.wad`. `[WadLoaded] Now flying fd2up.wad on MAP01`. In those 19 minutes, about 56,000 file packets plus about 4,700 pilot commands produced 0 `UnexpectedSequenceCount`, 0 `PacketOutOfOrder` and 0 `BadChecksum` (with one space packet per TC frame). |
| Optional, first attempt (before `maxContentLength`) | **Could not be uplinked through Yamcs's HTTP API.** Yamcs 5.12.8 caps a bucket upload at 5 MiB (`max_body_size: 5242880` on `UploadObject` in `buckets.proto`, independent of the bucket's 100 MB `maxSize`); the upload fails with HTTP 413. The demo now says so. Ways round it, not built: a filesystem-backed bucket (`buckets:` in `yamcs.yaml` with a `path`, filled by copying the file in), or splitting into 5 MiB parts and joining them on board. |

### Findings worth raising upstream

- **F´ v4.3.0**
  - `string size N` on a command argument is ignored on board: `Fw::CmdStringArg` caps it at `FW_CMD_STRING_MAX_SIZE` (40), so FileManager's 240-character paths fail above 40 with FORMAT_ERROR.
  - FileUplink opens without truncation and keeps files that fail their checksum.
  - `next_map`-style advances to a map the WAD lacks hang ViZDoom's `new_episode()` (that one is ViZDoom).
- **fprime-xtce**
  - Telemetry strings use `Fixed` size plus `LeadingSize`, so a short F´ string does not decode in Yamcs.
  - Command `maxSizeInBits` doesn't allow for the 16-bit length tag.
- **fprime-yamcs 0.2.1**
  - The upload "COMPLETED" state means *sent*, not *received*: there is no feedback from FileUplink and no retransmission. One TC packet lost in this session (`PacketOutOfOrder: Received packet 6 after packet 4`) failed a whole file's checksum.
  - The YAML comment's `FPRIME_UPLINK_CHUNK_SIZE=1024` example can't fit a 1024-byte TC frame.
- **Yamcs**
  - The 5 MiB bucket upload cap above.
- **ViZDoom 1.3.0**
  - A truncated WAD segfaults instead of raising.
  - A killed client leaves its engine process running.
- **DoomSat itself (fixed here, worth a look upstream)**
  - `scripts/wsl_sync.sh` was not re-runnable (`f4f7c93`).
  - Every `flight.sh stop` or restart orphaned the ViZDoom engine (twelve built up in one session); fixed by closing the game on SIGTERM.
  - `scripts/*.sh` are committed without the executable bit.
  - The committed reference XTCE was stale, missing `INTENT` and 15 channels.

### Review (two rounds) and the final regression

An adversarial review read the diff through four lenses (flight, payload, ground, spec compliance), and a
second reader then tried to refute each finding. Every confirmed finding is fixed:
- **Rename rule.** The rename cut at the first `.wad.`, so `e1m1.wad.fixed.wad.<nonce>.part` would have become
  `e1m1.wad`. Fixed, and checked standalone against 11 paths.
- **Probe and load raced.** The probe and the load could see different files if a re-uplink of the same name
  landed in between. The proven file is now hard-linked into `uplink/.pinned/<n>/`.
- **Watchdog.** It was a thread, and ViZDoom holds the GIL while it hangs (measured: a ticking thread printed
  nothing for 6 s). It is now a forked process.
- **Hung payload on stop.** A payload hung inside ViZDoom never runs its SIGTERM handler; stop now follows up
  with SIGKILL.
- **Dashboard.** Focus after using the form, and the page layout.
- **Demo hygiene.** A `.env`-aware `--remote-dir`, names checked before the uplink, bucket objects deleted,
  `--truncate` no longer shadowing a good WAD, latency after a timeout.

The second round read HEAD and caught two regressions in the first round's fixes:
- The demo reset `name` to `None`, so every file would have gone up as `None.<ms>.part`.
- The latency fix over-corrected.

Both are fixed. `tests/test_wad_uplink_demo.py` now runs the demo against a stand-in Yamcs, and the C++
LOAD_WAD encoder and the WAD report's field use are pinned by tests. Reintroducing either defect fails them.

The ground reading also found that Yamcs packs several commands into one TC frame while F´ reads only the first
packet of a frame (see the departures table). Its upstream root causes are listed under "Findings worth
raising upstream".

**Final regression** on the deployed build, with one code autopilot flying:
- basic.wad uplinked and loaded over freedoom2.wad (the frame is basic.wad's room).
- A name with `.wad.` inside (`basic.wad.v2.wad`) renamed correctly and loaded over freedoom1.wad.
- An IWAD swap to doom1.wad through a pinned link.
- A truncated uplink (`trunc-doom1.wad`), a missing name, traversal and a wrong extension, all refused with
  `WAD_IWAD`, `WAD_LOADS` and `EPISODE` unchanged and frames rising.

Afterwards only the flying WAD's pin was left, and no ViZDoom engine was orphaned. 412 unit tests pass (369 before this branch), and
honesty plus the canary report 17 checks, 0 failed.

### Sharing one install with feature/cfdp-spike (2026-10-04)

The CFDP spike builds into the same F´ project in `$DOOMSAT_HOME`. Its sync replaced
`DoomSat/Top/DoomSatTopologyDefs.hpp` with a header that has no FileHandling entries, and its start left a
`PrmDb.dat` next to the binary. After that this branch could not compile (`PingEntries::FileHandling_*`
undeclared), and it could not start either: with two files in `bin/`, fprime-gds's `find_app` exits with
"Multiple app candidates". This branch now commits its own `DoomSatTopologyDefs.hpp` (the `fprime-util new`
template for this topology), `wsl_sync.sh` copies it, and both launchers pass `--app`.
`tests/test_flight_scripts.py` checks the header against the topology and checks that the launchers pass
`--app`. With its 4 tests the suite is 416, all passing (the 412 above is the 3 October count).

Proof on this VM, after a CFDP flight that left both behind: `scripts/flight.sh build` built this branch,
with `bin/PrmDb.dat` still present. `scripts/flight.sh start` came up with `PAYLOAD_LINK` True and frames
rising. `tools/wad_uplink_demo.py --wad basic.wad --as natback.wad --iwad freedoom2.wad --map MAP01` then
reported FileReceived, WadUplinked, WadLoaded and OK, with `WAD_LOADS` going 0 to 1.

### Suggested charter note (for the PR; the charter is not edited)

> A flight in which `WAD_LOADS > 0` is a demonstration and is never scored. Its `out/decisions.jsonl` must
> not be fed to `research/runner.py flight --from-log` or to the ledger: attempts are labelled with the launch
> WAD, and a switch from level 2 or higher back to level 1 makes the pilot log a level row that did not happen.
> The dev set stays Freedoom Phase 1 and the test set the shareware episode (2.5); an uplinked WAD is neither.
