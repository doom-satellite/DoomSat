"""Phase A: forward processing of one finished episode, from the Yamcs archive to cataloged products.

    locate_episode -> build_l1 -> build_l2_path      -> publish_episode (Phase E: Timeline item + bucket copies)
                               -> build_l2_summary   (updates the asset that schedules sds_rollup, the L3)
                               -> build_l2_linkstats

Triggered by `sds_episode_watch` with the episode in `dag_run.conf` (an episodes.ClosedEpisode as a dict) and
`run_id = fwd__<episode id>`. Each task calls one function in doomsat_sds.pipeline and passes on a small dict
(product id, path, checksum); the products themselves are files under $DOOMSAT_SDS_HOME/products.

To run one by hand, trigger it with the conf the watcher would send, e.g.
    scripts/sds.sh airflow dags trigger sds_forward -r fwd__<id> -c '{"number": 3, "end_ms": ..., "closing":
    "PlayerDied", "outcome": "died", "start_event_ms": null, "inferred": false, "episode_id": "<id>"}'

It sends no commands. Besides $DOOMSAT_SDS_HOME it writes to Yamcs only in publish_episode: a Timeline item and
copies of the L2 products in the `doomsat-sds` bucket.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.sdk import Asset, dag, task

# Updated whenever an L2 summary is written; sds_rollup is scheduled on it.
L2_SUMMARY = Asset(name="doomsat_sds_l2_summary", uri="file:///doomsat-sds/products/l2_summary")


@dag(
    dag_id="sds_forward",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=3,
    # A Yamcs replay can hang for good (doomsat_sds.archive explains); the timeout turns that into a retry.
    default_args={"retries": 2, "retry_delay": timedelta(seconds=30), "execution_timeout": timedelta(minutes=30)},
    tags=["doomsat-sds", "phase-a"],
    doc_md=__doc__,
)
def sds_forward():
    @task
    def locate_episode(**context) -> dict:
        from doomsat_sds import pipeline
        conf = dict(context["dag_run"].conf or {})
        if "end_ms" not in conf:
            raise ValueError("sds_forward needs the episode in its conf (see the DAG docs)")
        settings, archive, catalog = pipeline.open_env()
        catalog.close()
        located = pipeline.locate_episode(archive, conf)
        print("episode %s: window %s, %.1f s" % (conf["episode_id"], located["window"],
                                                 (located["window"][1] - located["window"][0]) / 1000))
        return located

    @task
    def build_l1(located: dict, **context) -> dict:
        from doomsat_sds import pipeline
        settings, archive, catalog = pipeline.open_env()
        with catalog:
            return pipeline.l1(settings, archive, catalog, located, run_id=context["run_id"])

    def l2_task(product_type: str, **kw):
        @task(task_id="build_" + product_type, **kw)
        def build(l1_ref: dict, **context) -> dict:
            from doomsat_sds import pipeline
            settings, _, catalog = pipeline.open_env(archive=False)
            with catalog:
                return pipeline.l2(settings, catalog, l1_ref, product_type, run_id=context["run_id"])
        return build

    @task
    def publish_episode(l1_ref: dict) -> dict:
        from doomsat_sds import pipeline, publish
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            out = publish.publish_episode(settings, catalog, l1_ref["episode_id"])
        print(out)
        return out

    l1 = build_l1(locate_episode())
    l2s = [l2_task("l2_path")(l1), l2_task("l2_summary", outlets=[L2_SUMMARY])(l1), l2_task("l2_linkstats")(l1)]
    l2s >> publish_episode(l1)


sds_forward()
