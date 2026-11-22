"""Phase B: the quicklook, every minute: contact sheet of the latest captured frames plus link and payload health.

Reads the capture service's directory ($DOOMSAT_SDS_HOME/capture: frames, per-minute counts, status) and a
handful of realtime values and link states from Yamcs. Writes `ql_health` (JSON) and `ql_contact_sheet` (PNG),
catalogs both, keeps a copy of the newest as products/quicklook/latest.{json,png}, and drops quicklooks older than
a day, and copies the newest to the Yamcs bucket `doomsat-sds` as quicklook/latest.{png,json} (Phase E) for the
displays. The capture service itself is started by scripts/sds.sh, not by Airflow: it has to run continuously,
because frames are never archived and a frame not caught live is gone.

It sends no commands; in Yamcs it writes only the two bucket objects.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.sdk import dag, task


@dag(
    dag_id="sds_quicklook",
    schedule=timedelta(minutes=1),
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=5),
    default_args={"retries": 0, "execution_timeout": timedelta(minutes=4)},
    tags=["doomsat-sds", "phase-b"],
    doc_md=__doc__,
)
def sds_quicklook():
    @task
    def build_quicklook(**context) -> dict:
        from doomsat_sds import pipeline
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            out = pipeline.quicklook(settings, catalog, run_id=context["run_id"])
        from doomsat_sds import publish
        for ext, media in (("png", "image/png"), ("json", "application/json")):
            publish.publish_file(settings, "quicklook/latest." + ext, settings.products / "quicklook" / ("latest." + ext),
                                 media)
        print(out)
        return out

    build_quicklook()


sds_quicklook()
