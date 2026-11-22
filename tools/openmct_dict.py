"""The DoomSat parameter dictionary as Open MCT will see it (tools/build_openmct_*.py, tests/test_openmct.py).

Doom.fpp is authoritative. The running Yamcs never loads the committed ground/yamcs/mdb/fprime.xtce.xml:
scripts/wsl_run_flight.sh starts fprime-yamcs (through ground/yamcs/launch.py) with --deployment and
--yamcs-config-dir ground/yamcs, and fprime-yamcs copies that directory to a temporary one at every launch and
regenerates fprime.xtce.xml there (fprime-to-xtce) from the deployment's F' dictionary. The committed file is a
reference snapshot, and it lags.

So the merge is: every XTCE file in ground/yamcs/mdb, then any Doom.fpp channel the snapshot lacks, taken
from Doom.fpp. A display that names a parameter in neither fails the build. A name found only in Doom.fpp
is "snapshot lag": live Yamcs has it (once the deployment is built from this Doom.fpp); the committed
snapshot does not, and the build says so.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"x": "http://www.omg.org/spec/XTCE/20180204"}
DOOM = "/DoomSat_DoomSat/DoomSat/doom"


def _tag(el):
    return el.tag.split("}")[-1]


def _collect_types(root):
    types = {}
    for ts in root.iter():
        if _tag(ts) != "ParameterTypeSet":
            continue
        for t in ts:
            kind = _tag(t)
            name = t.get("name")
            info = {"kind": kind}
            if kind == "EnumeratedParameterType":
                info["enum"] = [(int(e.get("value")), e.get("label"))
                                for e in t.iter() if _tag(e) == "Enumeration"]
            elif kind == "AggregateParameterType":
                info["members"] = [(m.get("name"), m.get("typeRef")) for m in t.iter() if _tag(m) == "Member"]
            unit = [u.text for u in t.iter() if _tag(u) == "Unit"]
            if unit:
                info["unit"] = unit[0]
            aliases = [(a.get("nameSpace"), a.get("alias")) for a in t.iter() if _tag(a) == "Alias"]
            if aliases:
                info["aliases"] = aliases
            alarms = {}
            for r in t.iter():
                lvl = _tag(r).replace("Range", "").lower()
                if _tag(r).endswith("Range") and lvl in ("watch", "warning", "distress", "critical", "severe"):
                    lo, hi = r.get("minInclusive"), r.get("maxInclusive")
                    alarms[lvl] = [float(lo) if lo else None, float(hi) if hi else None]
                if _tag(r) == "EnumerationAlarm":
                    alarms.setdefault("enum", {}).setdefault(r.get("alarmLevel"), []).append(r.get("enumerationLabel"))
            if alarms:
                info["alarms"] = alarms
            types[name] = info
    return types


def _eng(info):
    k = info["kind"]
    return {"IntegerParameterType": "integer", "FloatParameterType": "float",
            "BooleanParameterType": "boolean", "EnumeratedParameterType": "enumeration",
            "StringParameterType": "string", "AggregateParameterType": "aggregate",
            "BinaryParameterType": "binary", "ArrayParameterType": "array"}.get(k, k)


def load_xtce(paths):
    """Return {qualified name: {"eng": ..., "enum": [...], "unit": ..., "aliases": [...], "source": file}}."""
    params = {}
    for path in paths:
        root = ET.parse(path).getroot()
        types = _collect_types(root)

        def walk(ss, prefix):
            here = f"{prefix}/{ss.get('name')}"
            for p in ss.findall("x:TelemetryMetaData/x:ParameterSet/x:Parameter", NS):
                ref = p.get("parameterTypeRef", "").split("/")[-1]
                info = dict(types.get(ref, {"kind": "?"}))
                aliases = [(a.get("nameSpace"), a.get("alias")) for a in p.iter() if _tag(a) == "Alias"]
                entry = {"eng": _eng(info), "source": Path(path).name}
                if "enum" in info:
                    entry["enum"] = info["enum"]
                if "unit" in info:
                    entry["unit"] = info["unit"]
                if "alarms" in info:
                    entry["alarms"] = info["alarms"]
                if aliases:
                    entry["aliases"] = aliases
                q = f"{here}/{p.get('name')}"
                params[q] = entry
                if info.get("kind") == "AggregateParameterType":
                    for mname, mref in info["members"]:
                        minfo = types.get((mref or "").split("/")[-1], {"kind": "?"})
                        m = {"eng": _eng(minfo), "source": Path(path).name, "member": True}
                        if "enum" in minfo:
                            m["enum"] = minfo["enum"]
                        params[f"{q}.{mname}"] = m
            for child in ss.findall("x:SpaceSystem", NS):
                walk(child, here)

        walk(root, "")
    return params


_FPP_PRIM = {"I8": "integer", "I16": "integer", "I32": "integer", "U8": "integer", "U16": "integer",
             "U32": "integer", "F32": "float", "F64": "float", "bool": "boolean"}


def load_fpp(path):
    """Channels, enums and structs from Doom.fpp (enough for Open MCT, not a full FPP parser)."""
    text = Path(path).read_text(encoding="utf-8")
    enums = {}
    for m in re.finditer(r"enum\s+(\w+)\s*:\s*\w+\s*\{(.*?)\}", text, re.S):
        vals = re.findall(r"(\w+)\s*=\s*(\d+)", m.group(2))
        enums[m.group(1)] = [(int(v), k) for k, v in vals]
    structs = {}
    for m in re.finditer(r"struct\s+(\w+)\s*\{(.*?)\n\s*\}", text, re.S):
        structs[m.group(1)] = re.findall(r"^\s*(\w+)\s*:\s*(\w+)", m.group(2), re.M)
    channels = {}
    for m in re.finditer(r"^\s*telemetry\s+(\w+)\s*:\s*(\w+)\s+id\s+(\d+)[^\n]*?(?:@<\s*([^\r\n]*))?$", text, re.M):
        name, typ, doc = m.group(1), m.group(2), (m.group(4) or "").strip()
        if typ in _FPP_PRIM:
            channels[name] = {"eng": _FPP_PRIM[typ]}
        elif typ in enums:
            channels[name] = {"eng": "enumeration", "enum": enums[typ]}
        elif typ in structs:
            channels[name] = {"eng": "aggregate", "members": [
                (n, {"eng": "enumeration", "enum": enums[t]} if t in enums else {"eng": _FPP_PRIM.get(t, "?")})
                for n, t in structs[typ]]}
        else:
            channels[name] = {"eng": "?"}
        if doc:
            channels[name]["doc"] = doc
    return channels, enums


ROOT = Path(__file__).resolve().parents[1]
MDB = ROOT / "ground" / "yamcs" / "mdb"
FPP = ROOT / "flight" / "Components" / "Doom" / "Doom.fpp"
RANGES = ROOT / "ground" / "yamcs" / "alarm-ranges.json"


def load(root=ROOT):
    """The merged dictionary, and the Doom.fpp channels the committed XTCE snapshot lacks. FPP doc comments ride along."""
    root = Path(root)
    xtce = [root / "ground" / "yamcs" / "mdb" / n for n in
            ("fprime.xtce.xml", "doom-ground.xtce.xml", "doom-ops.xtce.xml")]
    params = load_xtce([p for p in xtce if p.exists()])
    channels, _ = load_fpp(root / "flight" / "Components" / "Doom" / "Doom.fpp")
    drift = []
    for name, info in channels.items():
        q = f"{DOOM}/{name}"
        if q in params:
            if info.get("doc"):
                params[q]["doc"] = info["doc"]
            continue
        drift.append(name)
        if info["eng"] == "aggregate":
            params[q] = {"eng": "aggregate", "source": "Doom.fpp (snapshot lag)", "doc": info.get("doc", "")}
            for mname, minfo in info["members"]:
                params[f"{q}.{mname}"] = dict(minfo, source="Doom.fpp (snapshot lag)", member=True)
        else:
            params[q] = dict(info, source="Doom.fpp (snapshot lag)")
    ranges = root / "ground" / "yamcs" / "alarm-ranges.json"
    if ranges.exists():
        import json
        for q, r in json.loads(ranges.read_text(encoding="utf-8")).items():
            if q.startswith("/") and q in params:
                params[q]["alarms"] = r if "enum" not in r else {"enum": r["enum"]}
    return params, drift


def to_id(qualified):
    """Yamcs qualified name -> the Open MCT identifier key openmct-yamcs uses."""
    return qualified.replace("/", "~")
