"""Phase D, the file seam: the payload's own record of an episode, requested, downlinked and checked against L1.

    sds_record_watch (every 2 minutes)  -> one sds_record run per recent episode, run id rec__<episode id>
    sds_record:  plan_request -> send_SendFile_command -> wait_for_downlinked_file -> ingest_record -> compare_with_l1
                                                                               \\-> report_downlink_events (always)

This is the one place the data system commands anything, and the only command is cfdpManager's SendFile: the
task that sends it is named for it. It is off unless scripts/sds.sh runs with DOOMSAT_SDS_RECORDS=on, and it
only finds a file if the flight runs with RECORDS=on (payload/episode_record.py).

Only episodes whose payload ran with --records on (the episode's cataloged context says so) and that closed in
the last two hours are asked for: any other episode has no record to send, and the payload names records by
episode and last tic, with the episode number restarting with the payload, so a much older request could find a
different file (the comparison would say so, but there is no point asking).

The file comes down as a CFDP transfer into Yamcs's bucket cfdpDown; there is no file on disk to watch, so the wait
is a sensor that reads Yamcs (command history, events, the CFDP transfer list) and never writes to it: on a
timeout the transfer is left alone. The command is never retried by itself: a failed run is re-requested by hand
(clear it in the UI). The tasks around it retry, and report_downlink_events says why no record came:
record_unavailable (SendFile refused, or the file could not be opened or was empty), record_transfer_failed (the
transfer began and failed) or record_not_received (no answer within the wait). A record that arrived although F'
never acknowledged Yamcs's Finished PDU is ingested, and record_fin_unacknowledged says so.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, task

ENABLED = os.environ.get("DOOMSAT_SDS_RECORDS", "off") == "on"
# for the idempotent tasks, not the command; the timeout turns a hung Yamcs read into a retry
RETRY = {"retries": 2, "retry_delay": timedelta(seconds=20), "execution_timeout": timedelta(minutes=15)}
MAX_AGE_S = 2 * 3600
# An interrupted episode never wrote a record. A WAD switch did: the payload's recorder closes the episode a
# LOAD_WAD cut short as "reset", and record.compare accepts that against the ground's "wad_switch".
REQUESTABLE = ("died", "level_finished", "reset", "wad_switch")


@dag(
    dag_id="sds_record_watch",
    schedule=timedelta(minutes=2),
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["doomsat-sds", "phase-d"],
    doc_md=__doc__,
)
def sds_record_watch():
    @task
    def episodes_without_record() -> list[dict]:
        if not ENABLED:
            print("record requests are off (DOOMSAT_SDS_RECORDS=on to enable)")
            return []
        from doomsat_sds import pipeline
        _, _, catalog = pipeline.open_env(archive=False)
        cutoff = pipeline.now_ms() - MAX_AGE_S * 1000
        with catalog:
            todo = [e["episode_id"] for e in catalog.episodes()
                    if e["outcome"] in REQUESTABLE and e["closing_ms"] >= cutoff
                    and e["context"].get("records") == "on"
                    and catalog.current(e["episode_id"], "l1_episode") is not None
                    and catalog.current(e["episode_id"], "l0_record") is None]
        print("records to ask for: %s" % todo)
        return [{"trigger_run_id": "rec__" + eid, "conf": {"episode_id": eid}} for eid in todo]

    TriggerDagRunOperator.partial(
        task_id="trigger_record_runs", trigger_dag_id="sds_record", logical_date=None,
        skip_when_already_exists=True, wait_for_completion=False,
    ).expand_kwargs(episodes_without_record())


@dag(
    dag_id="sds_record",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,                  # cfdpManager sends one file at a time per channel; one request at a time
    tags=["doomsat-sds", "phase-d"],
    doc_md=__doc__,
)
def sds_record():
    @task(**RETRY)
    def plan_request(**context) -> dict:
        from doomsat_sds import pipeline, record
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            p = record.plan(settings, catalog, context["dag_run"].conf["episode_id"])
        print(p)
        return p

    @task(execution_timeout=timedelta(minutes=2))     # never retried: a command is not sent twice by a machine
    def send_SendFile_command(p: dict) -> dict:
        """The only command the SDS sends: cfdpManager.SendFile(source, dest), class 2, keep the file on board."""
        if not ENABLED:
            raise RuntimeError("record requests are off (DOOMSAT_SDS_RECORDS=on)")
        from doomsat_sds import config, record
        out = record.request(config.Settings.from_env(), p)
        print(out)
        return out

    # Read only: SendFile's answer, cfdpManager's events and Yamcs's CFDP transfer list. A read that fails is logged
    # and the sensor pokes again (silent_fail). Poke mode, not reschedule: live, Airflow 3.3.2's scheduler requeued a
    # 5 s reschedule before it had handled the executor's report of the poke, then failed the task ("finished with
    # state success, but the task instance's state attribute is queued") while the transfer was completing. A record
    # takes a few seconds and one record run is active at a time, so the worker slot it holds costs nothing. Each read
    # gives up after record.POKE_READ_S without a byte, so the sensor's own 120 s timeout plus a last slow poke ends
    # inside execution_timeout, which Airflow raises past silent_fail. The verdicts that settle it are raised as
    # AirflowFailException, which is never retried.
    @task.sensor(poke_interval=5, timeout=120, mode="poke", silent_fail=True,
                 execution_timeout=timedelta(minutes=4))
    def wait_for_downlinked_file(p: dict, command: dict):
        from airflow.sdk import PokeReturnValue
        from airflow.sdk.exceptions import AirflowFailException
        from doomsat_sds import config, pipeline, record
        from doomsat_sds.archive import YamcsArchive
        settings = config.Settings.from_env()
        archive = YamcsArchive(settings.yamcs, settings.instance, read_s=record.POKE_READ_S)
        seen = record.observe(settings, archive, p, command, pipeline.now_ms())
        print(seen["state"], seen["why"])
        if seen["state"] == "failed":
            raise AirflowFailException("%s: %s" % (seen["finding"], seen["why"]))
        return PokeReturnValue(is_done=seen["state"] == "received", xcom_value=seen)

    @task(trigger_rule="all_done", **RETRY)
    def report_downlink_events(p: dict, command: dict, received: dict) -> list:
        """After the wait, whatever its outcome: what cfdpManager and Yamcs said, and a finding when the record
        did not come, or came without F' acknowledging Yamcs's Finished PDU."""
        from doomsat_sds import pipeline, record
        if not command:
            return []
        settings, archive, catalog = pipeline.open_env()
        seen = record.observe(settings, archive, p, command, pipeline.now_ms())
        for e in seen["events"]:
            print(e)
        detail = {"command": command, "events": seen["events"], "why": seen["why"], "transfer": seen["transfer"]}
        finding = record.downlink_finding(seen, received)
        with catalog:
            if finding:
                catalog.add_finding(finding, detail, episode_id=p["episode_id"])
        return seen["events"]

    @task(**RETRY)
    def ingest_record(p: dict, command: dict, received: dict, **context) -> dict:
        from doomsat_sds import pipeline, record
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            ref = record.ingest(settings, catalog, p, command, received["transfer"], run_id=context["run_id"])
        ref["bucket_copy_deleted"] = record.forget_downlinked(settings, received["transfer"])
        return ref

    @task(**RETRY)
    def compare_with_l1(p: dict, l0_ref: dict, **context) -> dict:
        from doomsat_sds import pipeline, record
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            out = record.compare_and_register(settings, catalog, p, l0_ref, run_id=context["run_id"])
        print(out)
        return out

    p = plan_request()
    command = send_SendFile_command(p)
    received = wait_for_downlinked_file(p, command)
    report_downlink_events(p, command, received)
    compare_with_l1(p, ingest_record(p, command, received))


sds_record_watch()
sds_record()
