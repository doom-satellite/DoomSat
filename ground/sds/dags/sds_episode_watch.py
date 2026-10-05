"""Phase A trigger: one `sds_forward` run per episode, as soon as the episode's end shows up in Yamcs.

Every minute this reads the episode events (EpisodeStarted, PlayerDied, LevelFinished, PayloadConnected) over the
last six hours,
works out which episodes have closed (doomsat_sds.episodes explains the gaps it allows for), drops the ones
already in the catalog, and triggers `sds_forward` once per remaining episode with
`run_id = fwd__<episode id>`. Triggering a run id that exists is skipped, so an episode is never processed
twice however often it is seen. A run that failed for good (Yamcs down through all its retries, say) is tried
again as fwd__<id>__retry1 and __retry2; after that a `forward_failed` finding asks a person to look.

Why polling and not an AssetWatcher: Airflow folds watcher events that arrive together into one run, and a run
per episode is the point here. A minute of latency is the price.

It sends nothing to Yamcs; it reads the archive, and writes to the catalog only that `forward_failed` finding.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, task

LOOKBACK_S = 6 * 3600


@dag(
    dag_id="sds_episode_watch",
    schedule=timedelta(minutes=1),
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=15), "execution_timeout": timedelta(minutes=10)},
    tags=["doomsat-sds", "phase-a"],
    doc_md=__doc__,
)
def sds_episode_watch():
    @task
    def find_closed_episodes(**context) -> list[dict]:
        from doomsat_sds import pipeline
        ti = context["ti"]

        def state(run_id):
            try:
                return ti.get_dagrun_state("sds_forward", run_id)
            except Exception:                    # no such run
                return None

        settings, archive, catalog = pipeline.open_env()
        out = []
        with catalog:
            for e in pipeline.watch(settings, archive, catalog, lookback_s=LOOKBACK_S):
                run_id, why = pipeline.forward_run_id(e["episode_id"], state)
                print("closed: %s (%s%s): %s" % (e["episode_id"], e["outcome"], ", inferred" if e["inferred"] else "",
                                                why))
                if run_id:
                    out.append({"trigger_run_id": run_id, "conf": e})
                elif why.startswith("gave up") and not any(
                        f["kind"] == "forward_failed" and f["episode_id"] == e["episode_id"] for f in catalog.findings()):
                    catalog.add_finding("forward_failed", {"why": why}, episode_id=e["episode_id"])
        return out

    TriggerDagRunOperator.partial(
        task_id="trigger_forward_runs",
        trigger_dag_id="sds_forward",
        logical_date=None,                 # runs keyed by run id alone, so two can never collide on a date
        skip_when_already_exists=True,
        wait_for_completion=False,
    ).expand_kwargs(find_closed_episodes())


sds_episode_watch()
