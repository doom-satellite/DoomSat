# CLAUDE.md

DoomSat plays Doom through a real mission stack: ViZDoom (payload) → F´ flight software → CCSDS → Yamcs →
Open MCT / dashboard, with **jev** (TypeSafe System One) piloting from the ground and **Claude** (System Two, via
the `claude` CLI) reviewing. The README covers it for humans. This file tells you how to install it on the
user's machine without them doing the work.

## If the user asks you to set this up

Do the steps in order. Every script is idempotent, so if a step fails, fix the cause and re-run the same step.
Show the user progress in short lines. The first F´ build takes 5 to 10 minutes and the Open MCT build about 5.

### 1. Work out the platform

- **Linux** (Ubuntu/Debian): everything runs natively.
- **Windows**: everything runs **inside WSL2**. Check with `wsl -l -v` (from PowerShell or Git Bash).
  - If there is no distro, tell the user to run `wsl --install -d Ubuntu-24.04` in an **Administrator**
    PowerShell, reboot, open Ubuntu once to create a user, and then start Claude Code *inside* Ubuntu. You
    cannot do the reboot or the user creation for them.
  - Inside WSL, clone into `~/` (not `/mnt/c/...`), because builds on the Windows drive are very slow.
  - If they want to stay in Git Bash with the repo on Windows (a hybrid setup): put `DOOMSAT_WSL_DISTRO=<distro>`
    and optionally `DOOMSAT_WSL_USER=` in `.env`. `scripts/flight.sh` then forwards into WSL. The ground side
    (`setup_ground.sh`, the pilot, the dashboard and Open MCT) runs on Windows and needs Python and Node there.
- **macOS**: supported by the scripts but **not yet tested end to end**. Tell the user so. If
  fprime-yamcs's bundled JRE or the ViZDoom wheel fails, report the exact error and don't paper over it.

### 2. System prerequisites (ask before using sudo)

| | Linux / WSL | macOS |
|---|---|---|
| build tools | `sudo apt update && sudo apt install -y git curl build-essential binutils python3 python3-venv python3-pip` | `xcode-select --install`; `brew install python@3.12 git` |
| Node.js 24 (Open MCT) | `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh \| bash && source ~/.nvm/nvm.sh && nvm install 24` | same |
| Claude Code (System Two, optional) | `curl -fsSL https://claude.ai/install.sh \| bash` | same |

Python must be 3.10 or newer (`python3 --version`). F´ builds its own CMake into its venv, so don't install one.
Java isn't needed: fprime-yamcs ships its own JRE and Yamcs.

### 3. Keys

```bash
cp .env.example .env
```

Ask the user for their **TypeSafe API key** and write it as `TYPESAFE_API_KEY=` in `.env`. Never print it,
never commit it, and never put it in a log you save to the repo. `.env` is git-ignored. Claude needs no key,
because the pilot shells out to `claude` and uses the user's login. `ANTHROPIC_API_KEY` is only for
`--system-two anthropic`. If they have no TypeSafe key yet, install everything anyway: only jev needs it, and
`scripts/play.sh` flies the whole stack without one (they drive from the dashboard, or `--autopilot` lets the
code rules fly).

### 4. Install

```bash
scripts/flight.sh setup        # payload venv (ViZDoom 1.3.0), WADs, F´ v4.3.0 project + DoomSat build, fprime-yamcs
scripts/setup_ground.sh        # ground/.venv (yamcs-client…), openmct-yamcs + Open MCT built at pinned commits
```

Sub-steps if one fails: `scripts/flight.sh setup payload|wads|fprime`, `scripts/setup_ground.sh python|openmct`.
Everything on the flight side goes into `$DOOMSAT_HOME` (default `~/doom`): `payload-venv/`, `wads/`,
`DoomSat/` (the F´ project), `run/` (logs).

### 5. Verify, then show the user it works

```bash
python3 tools/doctor.py --jev                       # every piece, plus one real jev call
~/doom/payload-venv/bin/python payload/play.py --check   # Doom alone → out/doom_check.png
scripts/flight.sh start && sleep 40 && scripts/flight.sh check   # FRAMES_SENT rising, PAYLOAD_LINK True
python3 -m unittest discover -s tests               # run with ground/.venv/bin/python; no network, no game
```

Then give the user the URLs: Yamcs http://localhost:8090, the dashboard (`python3 tools/serve_dashboard.py`)
at http://localhost:8070, and Open MCT (`scripts/start_openmct.sh`) at http://localhost:9000. For a live
flight, run `scripts/start_pilot.sh --duration 120 --system-two none`. With no key, run `scripts/play.sh`
instead: the user clicks the dashboard's picture and drives. Finish with `scripts/flight.sh stop`.

## Running pieces on their own

| Piece | Command | Needs |
|---|---|---|
| Doom | `~/doom/payload-venv/bin/python payload/play.py [--check] [--wad freedoom1.wad]` | payload venv, WADs |
| F´ + stock GDS | `scripts/flight.sh gds` → :5000 (`scripts/flight.sh payload` adds the game) | F´ build |
| F´ + Yamcs | `scripts/flight.sh yamcs` → :8090 (no game) | F´ build |
| Full flight side | `scripts/flight.sh start` / `stop` / `status` / `check` | all of the flight side |
| Open MCT | `scripts/start_openmct.sh` → :9000 | Yamcs running |
| Dashboard | `python3 tools/serve_dashboard.py` → :8070 | Yamcs running |
| jev bench (no F´/Yamcs) | `~/doom/payload-venv/bin/python research/runner.py bench --maps E1M1 --seeds 1 --budget 60 --decider jev --wad ~/doom/wads/freedoom1.wad --out out/bench --allow-dirty` | key |
| Pilot | `scripts/start_pilot.sh [--system-two claude-cli\|anthropic\|none] [--duration S]` | flight side up, key |
| Play without jev | `scripts/play.sh` (a person drives from the dashboard) or `scripts/play.sh --autopilot` (the pilot with `--system-one code`) | flight side installed; no key |
| Uplink a WAD, switch to it (`LOAD_WAD`) | `ground/.venv/bin/python tools/wad_uplink_demo.py --wad PATH [--iwad NAME] --map MAP`, or `--iwad NAME --map MAP` alone for a WAD already on board. CFDP class 2 on this build (`--pdu-delay 5` for speed). Demonstrations only, never scored | flight side up; no key |
| Lossy link | `DOOMSAT_RELAY=1 scripts/flight.sh start`, then `python3 tools/lossy_relay.py --loss 5 [--seed N]`; the demo with `--tries 3` | flight side installed |

`WAD=`, `MAP=`, `GEOMETRY=on` and `SKILL=` before `scripts/flight.sh start` choose the level. Use `WAD=freedoom1.wad`
for the dev set. The shareware `doom1.wad` E1M1 is the test level.

## Where things are

| Path | What |
|---|---|
| `scripts/` | `flight.sh` (entry point), `setup_flight.sh`, `setup_ground.sh`, `start_pilot.sh`, `start_openmct.sh`, `common.sh` (paths + `.env`) |
| `flight/` | the F´ components (`Components/Doom/`, and `Components/CfdpGuard/`, which confines CFDP uploads and commits them on board), topology (`DoomSat/Top/`), config (com buffers, `CfdpCfg.fpp` and `CfdpCfg.hpp`, and `PrmDb.json`, which `wsl_run_flight.sh` turns into the `PrmDb.dat` the binary loads at boot). `wsl_sync.sh` copies them into `$DOOMSAT_HOME/DoomSat` before each build |
| `payload/` | the game as an instrument: `doom_payload.py`, `world_model.py`, `executor.py`, `play.py`, `wad_uplink.py` (`LOAD_WAD`'s records and name checks). Many `*_probe.py` files are one-off developer probes with hard-coded paths, so ignore them |
| `ground/` | `pilot.py` (the loop), `targeting.py`, `decision_graph.py`, `providers.py` (jev / Claude / OpenAI-compatible), `yamcs/`, `openmct/`, `dashboard/`, `graph/` |
| `research/` | the measurement harness (the "ruler"). Read-only for experiments: see `research/PROGRAM.md` |
| `tools/` | `doctor.py`, `serve_dashboard.py`, `run_report.py`, `replay.py`, `wad_uplink_demo.py`, `lossy_relay.py`, charts, recording |
| `docs/` | `ARCHITECTURE.md`, `CHARTER.md` (mission and rules), `CHARTER-STATUS.md`, `results/`, report PDF |

## Gotchas that have cost hours

- **On Windows, run bash in WSL, not in the Windows filesystem.** A `.sh` with CRLF endings fails with
  `$'\r'`. `.gitattributes` forces LF, and if you write a script from Windows, check it with `file`.
- In WSL, processes started with plain `nohup` inside a one-shot `wsl bash -c` die when the call returns. The
  run script uses `setsid -f` for this reason.
- `wsl -- cmd` re-parses arguments through a shell. Use `wsl --exec` when you pass quoted arguments.
- Yamcs takes about 30 s to come up. The pilot fails if you start it earlier.
- The payload must run from a fresh start for each comparison run. `scripts/flight.sh payload` restarts only the
  game. Check that no stale `pilot.py` is still appending to `out/decisions.jsonl`.
- On Windows, clone Open MCT with `core.longpaths=true` (setup_ground.sh does this).
- openmct-yamcs asks for Node ≥ 24.14.1. The setup turns engine-strict off, so 24.14.0 works.
- F´ `fprime-xtce` comes from a PR branch (for `!binary`). pip warns that it conflicts with fprime-yamcs's pin.
  Expect that warning and ignore it.
- Uplinked WADs land in `$DOOMSAT_HOME/wads/uplink` as `NAME.wad.<nonce>.part`, and `cfdpGuard` renames a
  class 2 upload to `NAME.wad` on board at its FIN. A CFDP destination must be the absolute
  `$DOOMSAT_HOME/wads/uplink/NAME.wad[.<nonce>].part` (only `[A-Za-z0-9_.+-]`); anything else, a relative path
  included, is refused on board (`UploadRefused`). `COMMIT_WAD(part, fileSize, checksum)` is for class 1 or a
  commit by hand. It takes only the bare `.part` name, at most 38 characters as Yamcs sends it (F´ command strings
  hold 40), and renames only if the size and CFDP checksum match (`wad_uplink_demo.py --checksum FILE` prints
  both). `COMMIT_WAD` and `LOAD_WAD` are safe to send again: a repeat changes nothing and gets a definite answer. A
  repeated `COMMIT_WAD` answers `WadUplinked` again (until a flight restart; after one it fails closed with
  `WadUplinkFailed`). A `LOAD_WAD` for what already flies answers `WadAlreadyFlying`, not `WadLoaded`, and a repeat
  during the proof shares the first one's answer. `scripts/flight.sh ut` runs the guard's and the Doom component's
  GTest suites.
- Yamcs starts through `ground/yamcs/launch.py`, not `fprime-yamcs` itself: fprime-yamcs writes the instance YAML
  back with sorted keys, which breaks the CFDP stream SQL. The parameter file is `$DOOMSAT_HOME/run/PrmDb.dat`,
  built at every start; if it cannot be built the start stops (`DOOMSAT_PRM_DEFAULTS=1` flies on F´'s defaults).
  Keep `bin/` to the binary alone: fprime-gds exits when it finds two files there and no `--app` (the scripts
  pass `--app` anyway).
- fprime-yamcs (0.2.1 and 0.2.4) stamps every F´ packet 1 s ahead: it adds 38 s to F´'s Unix time where Yamcs
  adds TAI−UTC, 37 s. `scripts/flight.sh setup fprime` patches that one constant (`tools/yamcs_time_patch.py`,
  the original kept as `.jar.orig`); `tools/doctor.py` and `flight.sh start` say when it is not applied. Data
  recorded before the patch keeps the extra second (`docs/plans/fprime-yamcs-time.md`). Restart the flight side after patching: a running Yamcs keeps the old class.
- `feature/wad-uplink` (the native build) and this branch share `$DOOMSAT_HOME/DoomSat`. Each commits its own
  `DoomSatTopologyDefs.hpp` and `wsl_sync.sh` copies it, so after switching branches `scripts/flight.sh build` is
  enough.
- CFDP class 1 has no retransmission, and F´ keeps a class 1 file whose checksum failed and calls it completed.
  Send WADs as class 2.

## Rules for working in this repo

- Keys never go in git, in logs saved to `runs/`, or in commit messages. Redact the key prefix in run logs.
- The pilot must not read the level: nothing from the WAD reaches the payload's decisions or the models
  (`docs/CHARTER.md` §2; `research/honesty.py` tests it). Only `research/grader/` may open a WAD.
- Don't edit `research/levels.yaml` or `research/frozen_metrics.py` to make a result look better. Every change
  to behaviour goes through the experiment ledger (`research/PROGRAM.md`).
- Run the tests before you commit: `python -m unittest discover -s tests`, and after a change under `flight/`,
  `scripts/flight.sh ut` as well (the GTests; it needs the F´ install).
