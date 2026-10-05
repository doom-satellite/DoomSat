"""Phase C: a reprocessing campaign over every cataloged episode, starting again from the Yamcs archive.

For each episode: rebuild L1 from the archive with the cataloged window and context, check that it reproduces
the cataloged checksum (a mismatch is recorded as a finding, never written over the original), then build
every L2 product that has no file at its current algorithm version. The new versions become current; the old
ones stay in the catalog and on disk. Every episode is then republished (Phase E), so its Timeline item links to
the current versions. An L2 left at its version while L1 moved on is reported as stale, never rebuilt in place.

Trigger it by hand after bumping a version in doomsat_sds.products.ALGORITHMS:
    scripts/sds.sh airflow dags trigger sds_reprocess
    scripts/sds.sh airflow dags trigger sds_reprocess -c '{"episodes": ["<id>"], "types": ["l2_path"]}'

It sends no commands; in Yamcs it writes only the republished Timeline items and bucket copies.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.sdk import Asset, Param, dag, task

L2_SUMMARY = Asset(name="doomsat_sds_l2_summary", uri="file:///doomsat-sds/products/l2_summary")
L2_TYPES = ["l2_path", "l2_summary", "l2_linkstats"]


@dag(
    dag_id="sds_reprocess",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=30), "execution_timeout": timedelta(minutes=45)},
    params={
        "episodes": Param([], type="array", description="episode ids to reprocess; empty means every cataloged episode"),
        "types": Param(L2_TYPES, type="array", description="L2 product types to bring up to their current version"),
    },
    tags=["doomsat-sds", "phase-c"],
    doc_md=__doc__,
)
def sds_reprocess():
    @task
    def list_episodes(**context) -> list[str]:
        from doomsat_sds import pipeline
        _, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            known = [e["episode_id"] for e in catalog.episodes()]
        wanted = context["params"]["episodes"] or known
        missing = sorted(set(wanted) - set(known))
        if missing:
            raise ValueError("not in the catalog: %s" % ", ".join(missing))
        print("campaign over %d episodes" % len(wanted))
        return wanted

    @task(max_active_tis_per_dag=2, outlets=[L2_SUMMARY])
    def reprocess_episode(episode_id: str, **context) -> dict:
        from doomsat_sds import pipeline
        settings, archive, catalog = pipeline.open_env()
        with catalog:
            report = pipeline.reprocess(settings, archive, catalog, episode_id, types=context["params"]["types"],
                                        run_id=context["run_id"])
            # Always: publishing is idempotent, and a retry after a failed publish built nothing this time.
            from doomsat_sds import publish
            report["published"] = publish.publish_episode(settings, catalog, episode_id)["item_id"]
        print(report)
        return report

    @task
    def campaign_report(reports: list) -> dict:
        reports = [r for r in reports if r]
        out = {"episodes": len(reports),
               "l1_reproduced": sum(1 for r in reports if r["l1"].startswith("reproduced")),
               "l1_not_reproduced": [r["episode_id"] for r in reports if r["l1"].startswith("NOT")],
               "built": sum(len(r["built"]) for r in reports),
               "already_current": sum(len(r["up_to_date"]) for r in reports),
               "stale": sorted({t for r in reports for t in r.get("stale", [])})}
        print(out)
        return out

    campaign_report(reprocess_episode.expand(episode_id=list_episodes()))


sds_reprocess()
