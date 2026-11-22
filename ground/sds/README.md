# DoomSat science data system (SDS)

The flight software and mission control turn Doom into telemetry. This turns the telemetry into **versioned,
cataloged products**, the way a science data system does, in its three usual modes:

- **forward processing**: one Airflow run per finished episode, from the Yamcs archive;
- **near real time**: a live frame capture and a quicklook every minute;
- **reprocessing**: a campaign over every cataloged episode when an algorithm changes.

```
 Yamcs archive ──► sds_episode_watch ──► sds_forward (one run per episode) ──► L1 + L2 ──► catalog ──► sds_rollup (L3)
   (events, TM,      every minute          locate → L1 → path/summary/linkstats     │                     │
    commands)                                                      publish ◄─────────┘          Timeline + bucket
 FRAME_CHUNK ──► capture service ──► capture/ ──► sds_quicklook (every minute) ──► contact sheet + health
 (realtime only)   (scripts/sds.sh)
 catalog ──► sds_reprocess (by hand) ──► L1 rebuilt from the archive, checked ──► L2 at the current version
 payload record ◄── SendFile ◄── sds_record (Phase D, opt-in) ──► L0 record (CFDP, cfdpDown) ──► checked against L1
```

## The rule it lives by: products never flow back to the pilot

A path image is a map of a level, and charter 2.2 says **no map survives an attempt**. So nothing on the pilot
side (`honesty.PILOT_SIDE` in `research/honesty.py`, plus the modules the pilot and payload import that the list
omits) reads anything this system writes: not the product store, not the catalog, not the capture directory, not
the `doomsat-sds` bucket. The honesty suite cannot see `ground/sds`, so `tests/test_sds_guard.py` enforces it,
together with the rest of the boundary:

- products are drawn from telemetry only, and nothing here opens a WAD;
- nothing is written under `out/frames/`, which the pilot reads;
- the only command the SDS can send is cfdpManager `SendFile`, from one task named for it, and only when
  record requests are switched on (Phase D). Of CFDP it only reads Yamcs's transfer list and the `cfdpDown`
  bucket, and deletes the one object it has ingested. The guard finds a write to either through the strings in
  the call, the module's own variables and URL helpers, and `record.BUCKET` and `record.object_url`, and allows
  the one `DELETE` only in `record.forget_downlinked`.

Nothing here is part of a graded run. Fly demonstration episodes on the dev set (`WAD=freedoom1.wad`).

## Running it next to a flight

```bash
scripts/sds.sh setup                      # once: Airflow 3.3.2 in ~/doom/sds/venv (uv + the official constraints)

WAD=freedoom1.wad scripts/flight.sh start # the flight side, as usual
scripts/sds.sh start                      # Airflow (127.0.0.1:8080) + the capture service, from a stopped state
scripts/start_pilot.sh --system-one code --system-two none     # episodes, without a TypeSafe key

scripts/sds.sh status                     # processes, Airflow health, catalog counts, last minute of capture
scripts/sds.sh catalog episodes           # what has been processed
scripts/sds.sh stop
```

`scripts/sds.sh` never starts, stops or commands the flight side, and the flight side does not depend on it:
with the SDS stopped, a flight behaves exactly as before. Start them in either order. The watcher looks back six
hours, so episodes flown while the SDS was down are processed when it comes back, but their context (map, pilot,
skill and the other launch options) is only known if the processes that flew them are still running; otherwise it
is recorded as unknown. The WAD is the exception on main: it is in telemetry, so the archive still has it (see
[Context](#context)). Frames are another matter: they are never archived, so frames from a time the capture
service was down are gone.

In a cloud VM that pauses when idle, every background process stops: run `scripts/flight.sh status` and
`scripts/sds.sh status`, and start whatever is down. `stop` (which `start` also runs first) stops the scheduler
and triggerer before the API server, so tasks in flight can finish and report, and it signals only the process
sessions it started itself (a pid file is trusted only while the process's start time still matches), never
anything matched by name. A forward run that fails for good is retried twice by the watcher
(`fwd__<id>__retry1`, `__retry2`); after that a `forward_failed` finding asks a person to look.

A Yamcs replay can die inside the server and leave its HTTP response open for ever. So parameters are read in
10-minute replays, each request has a 60 s read timeout, and every task that reads Yamcs has an
`execution_timeout`; a hung read becomes a failed try that is retried. To stop a stuck task by hand, SIGKILL its
`airflow worker -- <ti id>` process: on SIGTERM the Task SDK only calls the operator's `on_kill` and carries on.

Airflow's UI and API are on http://127.0.0.1:8080 with no login (local only). The CLI with the right
environment is `scripts/sds.sh airflow ...`, e.g. `scripts/sds.sh airflow dags list-runs sds_forward`.

## DAGs

| DAG | Schedule | What it does |
|---|---|---|
| `sds_episode_watch` | every minute | Finds closed episodes in the archive and triggers one `sds_forward` run each (`run_id fwd__<episode id>`, never twice) |
| `sds_forward` | per episode | locate → L1 → `l2_path`, `l2_summary`, `l2_linkstats` → publish (Timeline item, bucket) |
| `sds_rollup` | on the L2-summary asset | L3 rollup across the current summaries, copied to the bucket |
| `sds_quicklook` | every minute | Contact sheet of the latest captured frames plus link and payload health |
| `sds_reprocess` | by hand | Reprocessing campaign: rebuild L1 from the archive, check it reproduces, build every L2 lacking its current version, republish |
| `sds_record_watch`, `sds_record` | every 2 minutes, per episode | Phase D: request the payload's record with `SendFile`, wait for its CFDP downlink, ingest it from `cfdpDown`, compare with L1 |

The capture service is not a DAG: it has to run continuously, so `scripts/sds.sh` runs it beside Airflow.

### Why a poll and not an AssetWatcher

Airflow 3 can schedule on external events (`AssetWatcher`), but it folds events that arrive together into one run,
and the point here is one run per episode. The watcher polls the archive every minute instead and triggers runs
with the episode in their id, so a run is never duplicated however often an episode is seen.

## Episodes

There is no "episode over" message. F´ derives `EpisodeStarted`, `PlayerDied`, `LevelFinished` from edges in the
payload's status, and they have gaps: a flight's first `EpisodeStarted` fires before Yamcs is listening, an
episode ended by `RESET_GAME` has no end event, and `EPISODE` restarts at 1 with the payload. So:

- an episode is **closed** by `PlayerDied` / `LevelFinished`, or by the next `EpisodeStarted` (outcome `reset`,
  or `interrupted` when the payload restarted), or by `PayloadConnected`, which F´ logs when a restarted payload
  reconnects (outcome `interrupted`; a restart that keeps the number at 1 logs no `EpisodeStarted` at all). That
  last one is checked against the samples first, because a mere socket drop also reconnects;
- a `LOAD_WAD` that switches the WAD in flight (main) rebuilds the game and starts the next episode: F´ logs
  `WadLoaded` just before that `EpisodeStarted`, and the episode it cut short gets the outcome `wad_switch`
  instead of `reset`. It is still closed by the `EpisodeStarted`, at the same time, so its id does not change, and
  episodes cataloged before this rule keep the outcome they were cataloged with. `WadLoaded` is not added to L1's
  events: L1 embeds its list of event types, so adding one would change every L1, not just a switched episode's;
- its **window** is the contiguous run of archived `EPISODE == n` samples ending at the closing event (a silence of
  over 30 s or `TIC` going backwards breaks a run);
- its **id** is the closing time and the number: `20261004T004611Z-e0001`.

## Products

| Level | Type | File | From |
|---|---|---|---|
| L0 | `l0_record` | JSON | the payload's own record, downlinked (Phase D) |
| L1 | `l1_episode` | JSON | the archive: the ten science channels as one lossless time-ordered table, link housekeeping, the commands in the window (put on the TM time axis by the measured clock offset), the Doom events |
| L2 | `l2_path` | PNG | L1: the path walked, from telemetry only |
| L2 | `l2_summary` | JSON | L1: duration, kills, cells explored, health, outcome, distance |
| L2 | `l2_linkstats` | JSON | L1: status-stream completeness, frames and chunks sent, uplink completeness, link uptime, clock offset |
| L3 | `l3_rollup` | JSON | every current `l2_summary` |
| QL | `ql_health`, `ql_contact_sheet` | JSON, PNG | the capture directory and realtime values (kept a day) |
| QA | `qa_record_check` | JSON | `l0_record` against `l1_episode` (Phase D) |

Products are pure functions of their inputs, serialised one way, with no processing time inside, so the same
inputs and algorithm version give the same bytes and the same sha256. Algorithm versions are in
`doomsat_sds/products.py` (`ALGORITHMS`); a change to what a builder produces is a version bump there.

Files live under `$DOOMSAT_HOME/sds/products/` (`episodes/<id>/<type>-<version>.<ext>`, `l3/`, `quicklook/`).
Nothing generated goes in the repo.

### Context

Each episode's context is captured once, by its forward run, and kept in the catalog; reprocessing reuses it. Every
value carries its source in `context.sources`.

- **WAD.** On main the payload can switch WAD in flight, so its `--wad` argument can be wrong. The forward run reads
  `WAD_IWAD`, `WAD_PWAD` and `WAD_LOADS` (the base and patch WAD file names, and how many switches this payload
  process has made) in an archive read of their own, separate from the L1 read, and takes the value in effect
  when the episode began: the first sample from its first status to its last, otherwise the last sample before its
  first status (up to 5 s back), never one after its last status or its closing event (a switch writes the new WAD
  before the next episode's `EpisodeStarted`). The WAD cannot change during an episode, but F´ writes a new WAD
  once and repeats the last one every second, so if that one write is lost the sample just before the episode is
  the old WAD. The context gets `wad`, `pwad`, `wad_loads` and `wad_launch` (the payload's
  `--wad`). A flight build from before main has no such channels and cannot switch WAD; there the WAD is the
  payload's `--wad` and `--pwad` and the source says why. A Yamcs that is down fails the task (it is retried)
  rather than recording a guess. WADs are known by name only.
- **Map** from `payload.log`, which the payload also prints after a switch; `--map` stands in only for episode 1
  of a payload that has not switched WAD.
- **dev, test or other.** An episode flown on a switched WAD (`wad_loads > 0`) or a patch WAD is `other`, never dev
  or test: flights on an uplinked WAD are demonstrations, and a file name cannot tell an uplinked WAD from an
  installed one. Otherwise `research/levels.yaml` decides, by WAD file name and map, as before.
- **Processes.** A `LOAD_WAD` check runs the payload script again with `--probe` for up to 15 s, and a forked
  watchdog with the same arguments for up to 20 s; those are never taken for the flight payload.

`pwad`, `wad_loads` and `wad_launch` live in the catalog's `context_json`; its `wad` column holds the WAD flown.
`l2_summary` 1.3.0 carries `pwad` and `wad_loads`, and `l3_rollup` 1.1.0 keys a level by its patch WAD too
(`basic.wad over freedoom2.wad MAP01`).

### Uplink completeness across main's one-command-per-frame change

Main's commit `f3d2c655` sends one space packet per TC frame. Before it, Yamcs packed queued commands into one
frame and F´ kept only the first, so commands were lost under load (`docs/plans/wad-uplink-stage1.md`). The
`l2_linkstats` uplink completeness of flights before and after that commit is therefore not comparable: do not
pool or compare it across the two. The context's `repo_commit` identifies the stack an episode was flown on, and
`git merge-base --is-ancestor f3d2c655 <commit>` says which side it is. On main the uplink also carries CFDP PDUs
(TC virtual channel 2); Yamcs enters each transfer once in command history as `/doomsat/cfdp/pdu`, which, like
any command outside the Doom component, is reported under `other_commands` and kept out of completeness.

### The catalog

`$DOOMSAT_HOME/sds/catalog.sqlite`: `episodes` (window, outcome, WAD, map, skill, seed, pilot mode, repo commit,
dev/test set), `products` (id `<episode>/<type>@<version>`, level, version, path, sha256, input references,
Airflow run id, code commit, `is_current`), and `findings` (a checksum that moved, a record that disagrees).

```bash
scripts/sds.sh catalog summary
scripts/sds.sh catalog episodes
scripts/sds.sh catalog products --type l2_path
scripts/sds.sh catalog show 20261004T004611Z-e0001/l2_summary@1.0.0
scripts/sds.sh catalog findings
scripts/sds.sh catalog sql "SELECT outcome, COUNT(*) FROM episodes GROUP BY 1"
```

### Reprocessing (Phase C)

Bump a version in `ALGORITHMS`, then `scripts/sds.sh airflow dags trigger sds_reprocess` (optionally
`-c '{"episodes": [...], "types": ["l2_path"]}'`). Each episode's L1 is rebuilt from the archive with the
cataloged window and context, and must reproduce its cataloged checksum; a mismatch is a finding and the rebuilt
file is kept beside the original, never over it. The new L2 versions become current, the old ones stay. An L2
counts as up to date only if it was built from the current L1's exact bytes; one left at its version while L1
moved to a new version is reported as stale and never rebuilt in place (bump its version too: an L1 change is a
new processing baseline). Two campaigns have run so far: `l2_path` 2.0.0 (breaks the line at teleports, colours
the path by time), and the 1.1.0 baseline after L1 stopped losing samples that share a time tag.

## Phase D: the payload's own record (opt-in)

```bash
RECORDS=on WAD=freedoom1.wad scripts/flight.sh start   # the payload writes ~/doom/run/rec/<episode>-<last tic>.json
DOOMSAT_SDS_RECORDS=on scripts/sds.sh start            # the SDS asks for each one with SendFile
```

The record is written when the episode ends, before its last status goes down, so it exists by the time the
ground asks. `sds_record` sends cfdpManager's `SendFile` (class 2, keep `KEEP`: `DELETE` would remove the record on
board once sent) and waits for the CCSDS CFDP downlink, which Yamcs's CfdpService saves in its bucket `cfdpDown`.
It ingests the record from there as `l0_record` and compares it with L1: episode, outcome, last tic, kills, cells,
final health and position, positions along the path, context (the WAD flown, its patch WAD, map, skill, seed), and
how many of the statuses sent reached the archive. Every disagreement becomes a catalog finding. A WAD switch
(`LOAD_WAD`) also ends an episode with a record: the payload's recorder writes it as `reset`, which agrees with the
ground's `wad_switch`.

Only episodes whose payload ran with `--records on` are asked for (the episode's cataloged context records the
payload's arguments), so turning either switch on in either order is safe. The command is never resent
automatically: a failed `rec__` run is re-requested by hand (clear it in the UI).

There is no file on disk to wait for, so `wait_for_downlinked_file` is a sensor (every 5 s, poke mode, two
minutes at most) that reads three things and writes nothing: `SendFile`'s answer (Yamcs's acknowledgements, then the
F´ dispatcher's `OpCodeCompleted` or `OpCodeError`), cfdpManager's events, and
`GET /api/filetransfer/fprime-project/cfdp/transfers?direction=DOWNLOAD&start=<command time - 5 s>`, from which it
takes the newest transfer whose `remotePath` is the source path sent and that Yamcs created at or after the command
(it creates a downlink when the first PDU arrives, after F´ has the command). Each read gives up after 15 s without a
byte, so a stalled Yamcs costs one poke, which the sensor logs and repeats, rather than the whole wait (Airflow
fails a sensor whose poke outlasts its execution timeout, whatever `silent_fail` says). `COMPLETED` is a record
received. `FAILED`
with a reason starting "File was received OK" is one too: the checksum-verified file is in the bucket, only F´'s
acknowledgement of Yamcs's Finished PDU was lost, and the finding `record_fin_unacknowledged` says so. When no
record comes, `report_downlink_events` says why: `record_unavailable` (`SendFile` refused, or `TxFileOpenFailed`,
`TxZeroLengthFile` or `SendFileInitiateFail` on board: the file was never sent and Yamcs never lists a transfer, so
the wait ends at once), `record_transfer_failed` (a transfer that began and failed) or `record_not_received`
(nothing settled within the wait). A timeout cancels nothing: nothing in the SDS POSTs to `/filetransfer`.

The object is read by the name the transfer gives (a name already taken in the bucket gets `(1)`, `(2)`... added),
checked against the transfer's size and the record format, and then deleted, that object only: `cfdpDown` holds 1000
objects and 100 MB. The product's inputs record the bucket, object, transfer id and CFDP transaction id.

Positions are compared only where the archive can attribute them. Two statuses can share an F´ time tag, and a
channel can lose one of the pair on board, so a time tag with fewer positions than tics is reported as
unattributable, neither agreement nor disagreement.

The flight software sets traps here, all handled. F´ command strings hold at most 40 characters on board (the
dictionary says 200), so paths are kept to 39, relative to the F´ binary's directory when the absolute one is too
long. Yamcs needs all seven `SendFile` arguments. cfdpManager answers OK when it has queued the transfer and fails
later, asynchronously, if the file cannot be opened, so the answer alone proves nothing. F´ events carry the
spacecraft's time, which an unpatched fprime-yamcs puts about 0.9 s ahead of Yamcs's (`tools/yamcs_time_patch.py`),
and F´ numbers its transactions from 1 at every boot, so the transfer is matched by source path and the time
Yamcs created it.

## Publishing (Phase E)

Every episode gets one Yamcs Timeline item (source `rdb`) spanning its window, with properties linking to its
current products, and its L2 products are copied to the bucket `doomsat-sds`. Items have deterministic ids, so
republishing after a campaign updates them in place. The latest quicklook and rollup go to fixed object names
(`quicklook/latest.png`, `l3/rollup-current.json`), which the dashboard can show through its `/api` proxy, e.g.
`<img src="/api/storage/buckets/doomsat-sds/objects/quicklook/latest.png">`.

```bash
curl -s "http://localhost:8090/api/timeline/fprime-project/items?source=rdb&details=true"
curl -s "http://localhost:8090/api/storage/buckets/doomsat-sds/objects?prefix=episodes/"
```

The bucket holds current products only: publishing an episode deletes its copies of superseded versions (they
stay in the product store and the catalog). That is three objects per episode, so the default cap of 1000 objects
is reached at about 330 episodes; give the bucket a larger `maxObjects` in Yamcs's configuration before that. L1
records are not copied: at about 3 KB per second of play they would fill the default 100 MB after a few hundred
episodes.

Every product written also appends an OpenLineage `RunEvent` to `$DOOMSAT_HOME/sds/lineage/openlineage.jsonl`
(inputs, output, checksum, Airflow run, code commit), so run history and lineage can be read without opening
Airflow's database.

## Tests

```bash
ground/.venv/bin/python -m unittest discover -s tests                                   # the repo's suite, SDS included
ground/.venv/bin/python -m unittest discover -s ground/sds/tests -p "test_sds_*.py"     # the SDS alone
```

No network, no game, no Airflow: Yamcs is replaced by a recorded window of the real archive
(`tests/data/two_deaths.json.gz`, two episodes). Tests that need Pillow skip themselves in `ground/.venv`; run
them with `~/doom/sds/venv/bin/python` to include those.

## Layout

| Path | What |
|---|---|
| `dags/` | thin DAG files: each task calls one function in the package |
| `doomsat_sds/` | the product code: `archive` (Yamcs reads, recorded or live), `episodes`, `products`, `png`, `catalog`, `context`, `pipeline`, `frames` and `capture` (Phase B), `quicklook`, `publish` (Phase E), `record` (Phase D), `lineage`, `store`, `config` |
| `tests/` | unit tests and the recorded archive window |
| `../../scripts/sds.sh` | setup, start, stop, status |
