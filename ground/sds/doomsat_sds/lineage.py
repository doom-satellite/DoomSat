"""OpenLineage run events for every product the SDS makes, appended to a JSON-lines file.

One COMPLETE RunEvent per product written (not for a retry that found the same bytes): the job is the product
type, the inputs are what the catalog lists as its inputs (archive reads or other products), the output is the
product itself with its checksum as the dataset version. An operations agent can read run history and lineage
from $DOOMSAT_SDS_HOME/lineage/openlineage.jsonl without opening Airflow's database or the catalog.

Spec: https://openlineage.io/spec/2-0-2/OpenLineage.json (RunEvent). Writing lineage never fails a product:
an unwritable file is skipped.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path

from .config import Settings

PRODUCER = "https://github.com/PaulMRamirez/DoomSat/tree/main/ground/sds"
SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
FACET = "https://openlineage.io/spec/facets/1-0-1/%s.json#/$defs/%s"        # as openlineage-python writes them
CUSTOM_FACET = PRODUCER + "/README.md#publishing-phase-e"                   # our own run facet's description
NAMESPACE = "doomsat-sds"


def _facet(facet_name: str, **fields) -> dict:
    return dict(fields, _producer=PRODUCER, _schemaURL=FACET % (facet_name, facet_name))


def _dataset(ref: dict) -> dict:
    if "product_id" in ref:
        return {"namespace": NAMESPACE, "name": ref["product_id"],
                "facets": {"version": _facet("DatasetVersionDatasetFacet", datasetVersion=ref.get("sha256", ""))}}
    # An archive read: the Yamcs instance is the namespace, the kind of read and its window the name.
    name = "%s[%s,%s)" % (ref.get("kind", "read"), ref.get("start", ""), ref.get("stop", ""))
    return {"namespace": ref.get("source", "yamcs"), "name": name}


def event(row: dict, status: str, at: str | None = None) -> dict:
    run_id = row.get("airflow_run_id") or "manual"
    return {
        "eventType": "COMPLETE",
        "eventTime": at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "producer": PRODUCER,
        "schemaURL": SCHEMA,
        "job": {"namespace": NAMESPACE, "name": row["product_type"]},
        "run": {"runId": str(uuid.uuid5(uuid.NAMESPACE_URL, "doomsat-sds/%s/%s" % (run_id, row["product_id"]))),
                "facets": {"doomsat_sds": dict(_producer=PRODUCER, _schemaURL=CUSTOM_FACET, airflow_run_id=run_id,
                                               status=status,
                                               algorithm_version=row["algorithm_version"],
                                               code_commit=row.get("code_commit"), level=row["level"],
                                               episode_id=row.get("episode_id"))}},
        "inputs": [_dataset(r) for r in row.get("inputs", [])],
        "outputs": [{"namespace": NAMESPACE, "name": row["product_id"],
                     "facets": {"version": _facet("DatasetVersionDatasetFacet", datasetVersion=row["sha256"]),
                                "dataSource": _facet("DatasourceDatasetFacet", name="product store",
                                                     uri=Path(row["path"]).resolve().as_uri())}}],
    }


def emit(settings: Settings, row: dict, status: str) -> None:
    if status == "unchanged":
        return
    try:
        os.makedirs(settings.lineage, exist_ok=True)
        with open(settings.lineage / "openlineage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event(row, status), sort_keys=True) + "\n")
    except OSError:
        pass
