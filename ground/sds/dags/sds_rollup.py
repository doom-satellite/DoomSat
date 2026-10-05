"""Level 3: the rollup across every episode's current L2 summary, rebuilt after each forward or reprocessing run.

Scheduled on the L2-summary asset, so it runs once after any run that wrote a summary. When several runs finish
together Airflow folds them into one rollup run, which is exactly what an L3 wants. The rollup's id carries a
hash of its inputs, so each distinct set of summaries is its own product and the newest is current. The current
rollup is also copied to the Yamcs bucket as l3/rollup-current.json (Phase E).
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.sdk import Asset, dag, task

L2_SUMMARY = Asset(name="doomsat_sds_l2_summary", uri="file:///doomsat-sds/products/l2_summary")


@dag(
    dag_id="sds_rollup",
    schedule=[L2_SUMMARY],
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(seconds=20)},
    tags=["doomsat-sds", "phase-a"],
    doc_md=__doc__,
)
def sds_rollup():
    @task
    def build_l3_rollup(**context) -> dict:
        from doomsat_sds import pipeline
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            ref = pipeline.rollup(settings, catalog, run_id=context["run_id"])
        print("rollup %s (%s)" % (ref["product_id"], ref["status"]))
        from doomsat_sds import publish
        ref["published"] = publish.publish_file(settings, "l3/rollup-current.json", ref["path"], "application/json")
        return ref

    build_l3_rollup()


sds_rollup()
