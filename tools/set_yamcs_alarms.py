# /// script
# requires-python = ">=3.10"
# dependencies = ["yamcs-client>=1.9"]
# ///
"""Apply the flight-parameter alarm ranges in ground/yamcs/alarm-ranges.json to a running Yamcs.

    ground/.venv/bin/python tools/set_yamcs_alarms.py            # localhost:8090, fprime-project, realtime
    ground/.venv/bin/python tools/set_yamcs_alarms.py --dry-run

yamcs-client is in ground/.venv (scripts/setup_ground.sh), not in the system python3.

fprime-xtce emits no alarms, so without this Open MCT has no limit lines, no coloured values and nothing in
Fault Management for flight telemetry. These are MDB overrides on one processor: they do not survive a Yamcs
restart. Nothing runs this for you, so run it by hand once Yamcs is up, after every scripts/flight.sh start (or
yamcs). openmct-yamcs subscribes to MDB changes, so an open Open MCT picks them up without a reload.

Each level in the file is the ALLOWED range, both ends inclusive (as the replay and doom-ground.xtce.xml read
them), so HEALTH 50 is no alarm and one enemy is no alarm. yamcs-client's set_default_alarm_ranges cannot say
that: it writes both ends as exclusive and drops a bound of 0. So the request is built here.

Enumerated alarms (DEAD, STUCK, PAYLOAD_LINK) cannot be set through this API; the Open MCT condition sets
cover them live. The durable fix is FPP limits on the channels plus fprime-xtce support for them.
"""
import argparse
import json
from pathlib import Path

from yamcs.client import YamcsClient
from yamcs.client.core.helpers import adapt_name_for_rest
from yamcs.protobuf.mdb import mdb_pb2
from yamcs.protobuf.processing import mdb_override_service_pb2

LEVELS = {"watch": mdb_pb2.WATCH, "warning": mdb_pb2.WARNING, "distress": mdb_pb2.DISTRESS,
          "critical": mdb_pb2.CRITICAL, "severe": mdb_pb2.SEVERE}


def alarm_request(r):
    """The SET_DEFAULT_ALARMS request for one entry: each level's allowed [low, high], inclusive; null is open."""
    req = mdb_override_service_pb2.UpdateParameterRequest()
    req.action = mdb_override_service_pb2.UpdateParameterRequest.SET_DEFAULT_ALARMS
    req.defaultAlarm.minViolations = 1
    for lvl, code in LEVELS.items():
        if lvl not in r:
            continue
        lo, hi = r[lvl]
        # the deprecated field: Yamcs 5.12 reads it on this request and ignores staticAlarmRanges
        ar = req.defaultAlarm.staticAlarmRange.add(level=code)
        if lo is not None:   # 0 is a bound, not "open"
            ar.minInclusive = lo
        if hi is not None:
            ar.maxInclusive = hi
    return req


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="localhost:8090")
    ap.add_argument("--instance", default="fprime-project")
    ap.add_argument("--processor", default="realtime")
    ap.add_argument("--ranges", default=str(Path(__file__).resolve().parents[1] / "ground" / "yamcs" / "alarm-ranges.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ranges = {k: v for k, v in json.loads(Path(a.ranges).read_text()).items() if k.startswith("/")}
    proc = None if a.dry_run else YamcsClient(a.url).get_processor(a.instance, a.processor)
    done, skipped = 0, []
    for q, r in ranges.items():
        if "enum" in r:
            skipped.append(q.rsplit("/", 1)[1])
            continue
        kw = {lvl: tuple(r[lvl]) for lvl in LEVELS if lvl in r}
        print(f"{'would set' if a.dry_run else 'set'} {q}: {kw} allowed, inclusive")
        if proc:
            try:
                proc.ctx.patch_proto(f"/mdb-overrides/{a.instance}/{a.processor}/parameters{adapt_name_for_rest(q)}",
                                     data=alarm_request(r).SerializeToString())
                done += 1
            except Exception as e:  # a parameter the running MDB lacks (a deployment built from an older Doom.fpp) is reported, not fatal
                print(f"  failed: {e}")
    print(f"{done} parameters have alarm ranges; enumerated (condition sets only): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
