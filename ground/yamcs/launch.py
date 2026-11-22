"""fprime-yamcs, launched the way DoomSat needs it. Same arguments as `fprime-yamcs`; no fork of it.

1. The instance configuration is written back in its own key order. fprime-yamcs 0.2.1 rewrites the instance YAML
   with yaml.safe_dump, which sorts keys, and Yamcs creates streamConfig entries in the order it reads them, so a
   streamConfig `sqlFile` (CFDP's streams) would run before the tm and tc streams it reads, and Yamcs would not start.
2. With DOOMSAT_RELAY=1 (the lossy-link harness, tools/lossy_relay.py), Yamcs's TM and TC frame links move to
   DOOMSAT_RELAY_TM_PORT and DOOMSAT_RELAY_TC_PORT (default 51000 / 51001). The F' comm bridge keeps the ports the
   launcher gave it, so the relay sits between the two: bridge -> 50000 -> relay -> 51000 -> Yamcs for TM, and
   Yamcs -> 51001 -> relay -> 50001 -> bridge for TC. Unset, empty, 0, false, no or off: the links are exactly as
   fprime-yamcs sets them (so DOOMSAT_RELAY=0 on the command line overrides a 1 in .env).
"""
import os
import sys

import yaml

_safe_dump = yaml.safe_dump


def _relay_on():
    return os.environ.get("DOOMSAT_RELAY", "").strip().lower() not in ("", "0", "false", "no", "off")


def _relay(data):
    if not _relay_on() or not isinstance(data, dict):
        return data
    ports = {"org.yamcs.tctm.ccsds.UdpTmFrameLink": int(os.environ.get("DOOMSAT_RELAY_TM_PORT", "51000")),
             "org.yamcs.tctm.ccsds.UdpTcFrameLink": int(os.environ.get("DOOMSAT_RELAY_TC_PORT", "51001"))}
    for link in data.get("dataLinks", []) or []:
        if link.get("class") in ports:
            link["port"] = ports[link["class"]]
            print(f"[launch] {link.get('name')} on port {link['port']} (behind the relay)", flush=True)
    return data


def safe_dump(data, stream=None, **kw):
    kw.setdefault("sort_keys", False)
    return _safe_dump(_relay(data), stream, **kw)


yaml.safe_dump = safe_dump

from fprime_yamcs.__main__ import main  # noqa: E402  after the patch: it looks yaml.safe_dump up when it writes

if __name__ == "__main__":
    sys.argv[0] = "fprime-yamcs"
    sys.exit(main())
