# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""The DoomSat Open MCT displays, as code.

    python tools/build_openmct_displays.py --doc     # live displays + docs/OPENMCT.md (both committed)
    python tools/build_openmct_displays.py --target replay --pack ground/openmct/replay/flight-32/pack.json
    python tools/build_openmct_displays.py --plan-start <epoch ms>   # before a campaign flight

Writes ground/openmct/displays/doomsat-displays.json (live, committed), doomsat-displays.campaign.json (live
with --plan-start, git-ignored; scripts/start_openmct.sh serves it while it is newer than the committed file and
under a day old) or doomsat-displays.replay.json (replay, git-ignored; tools/build_openmct_replay.py calls this
for you): an Open MCT object tree that StaticRootPlugin serves read-only as "DoomSat Displays", and that "Import
from JSON" loads into My Items as an editable copy. Identifiers are uuid5 of the object's path, so a regenerated
file diffs cleanly against the last one.

Every telemetry reference, including what the custom views read, is checked against the merged dictionary
(tools/openmct_dict.py). An unknown name fails the build. A name known only from Doom.fpp is listed as
snapshot lag: live Yamcs has it (fprime-yamcs regenerates the XTCE from the F' dictionary at every launch),
but the committed fprime.xtce.xml snapshot does not.
"""
import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import openmct_dict as doomdict  # noqa: E402
import openmct_docs  # noqa: E402

DOOM = doomdict.DOOM
G = "/DoomGround"
FSW = "/DoomSat_DoomSat"
UUID_NS = uuid.UUID("6f1c5a52-2b0e-4a34-9d1d-d00d5a7d0000")
NOW = 1790000000000

# Severity palette, the same meaning everywhere (matches openmct-yamcs limit colours)
OK, WATCH, WARN, BAD, OFF, INFO = "#38761d", "#bf9000", "#b45f06", "#990000", "#434343", "#0b5394"
MODE_BG = {"EXPLORE": "#0b5394", "APPROACH": "#134f5c", "OPERATE": "#351c75", "FIGHT": "#990000",
           "RETREAT": "#b45f06", "RECOVER": "#7f6000"}


class Builder:
    def __init__(self, params, target):
        self.params, self.target = params, target
        self.objects, self.used, self.drift_used = {}, set(), set()

    # ---------------------------------------------------------------- identifiers
    def uid(self, *path):
        return str(uuid.uuid5(UUID_NS, "/".join(path)))

    def P(self, q):
        if q not in self.params:
            raise SystemExit(f"display references {q}, which no dictionary defines")
        self.used.add(q)
        if "snapshot lag" in self.params[q].get("source", ""):
            self.drift_used.add(q)
        return {"key": doomdict.to_id(q), "namespace": "taxonomy"}

    def ref(self, key):
        return {"key": key, "namespace": ""}

    def add(self, name, type_, composition=(), path=None, **fields):
        key = self.uid(path or name, type_)
        comp = [c if isinstance(c, dict) else self.ref(c) for c in composition]
        obj = {"identifier": self.ref(key), "name": name, "type": type_, "composition": comp,
               "modified": NOW, "created": NOW, "persisted": NOW, "createdBy": "build_displays",
               "modifiedBy": "build_displays", "location": None}
        obj.update(fields)
        if not comp and type_ not in ("folder", "layout", "flexible-layout", "tabs", "time-strip", "comps"):
            obj.pop("composition")
        self.objects[key] = obj
        return key

    def events_id(self, severity=None):
        k = "yamcs.events" if severity is None else f"yamcs.events.severity.{severity}"
        return {"key": k, "namespace": "taxonomy"}

    # ---------------------------------------------------------------- telemetry views
    def lad(self, name, qs):
        return self.add(name, "LadTable", [self.P(q) for q in qs], configuration={
            "hiddenColumns": {"timestamp": True, "type": True, "WATCH": True, "WARNING": True, "DISTRESS": True, "CRITICAL": True, "SEVERE": True}, "isFixedLayout": False})

    def plot(self, name, qs, stacked=False):
        if stacked:
            return self.add(name, "telemetry.plot.stacked", [self.P(q) for q in qs],
                            configuration={"series": [], "yAxis": {}, "xAxis": {}})
        return self.add(name, "telemetry.plot.overlay", [self.P(q) for q in qs], configuration={
            "series": [{"identifier": self.P(q), "yAxisId": 1} for q in qs],
            "yAxis": {}, "xAxis": {}, "legend": {"position": "top", "expandByDefault": False,
                                                 "showValueWhenExpanded": True, "showLegendsForChildren": True}})

    def table(self, name, ids, rows=50):
        return self.add(name, "table", ids, configuration={
            "columnWidths": {}, "hiddenColumns": {"name": True}, "telemetryMode": "performance", "persistModeChange": True,
            "rowLimit": rows, "columnOrder": [], "cellFormat": {}, "autosize": True})

    def gauge(self, name, q, kind="dial-filled", lo=0, hi=100, limit_low="", limit_high="", precision=0):
        return self.add(name, "gauge", [self.P(q)], configuration={"gaugeController": {
            "gaugeType": kind, "isDisplayMinMax": True, "isDisplayCurVal": True, "isDisplayUnits": True,
            "isUseTelemetryLimits": False, "limitLow": limit_low, "limitHigh": limit_high,
            "max": hi, "min": lo, "precision": precision}})

    PALETTE = ["#3d85c6", "#6aa84f", "#e69138", "#8e7cc3", "#c27ba0", "#45818e", "#bf9000", "#cc4125"]

    def bar(self, name, qs, use_bar=True):
        # Pre-filled with exactly what the bar graph view would otherwise write on first load: a static
        # (read-only) object that a view tries to mutate throws.
        series = {f"taxonomy:{doomdict.to_id(q)}": {"name": q.rsplit("/", 1)[1], "type": "yamcs.telemetry",
                                                    "isAlias": True, "color": self.PALETTE[i % 8]}
                  for i, q in enumerate(qs)}
        return self.add(name, "telemetry.plot.bar-graph", [self.P(q) for q in qs], configuration={
            "barStyles": {"series": series}, "axes": {"xKey": "value", "yKey": "none"},
            "axisScaling": {"xAxis": "linear", "yAxis": "linear"}, "useInterpolation": "linear", "useBar": use_bar})

    def comps(self, name, expression, variables, unit=""):
        time_md = {"key": "utc", "source": "timestamp", "name": "Timestamp",
                   "format": "iso" if self.target == "live" else "utc", "hints": {"domain": 1}}
        comp = [self.P(q) for _, q in variables]
        return self.add(name, "comps", comp, configuration={"comps": {
            "expression": expression, "outputFormat": "%.2f",
            "parameters": [{"keyString": f"taxonomy:{doomdict.to_id(q)}", "name": v, "valueToUse": "value",
                            "testValue": 1, "timeMetaData": time_md, "accumulateValues": False, "sampleSize": 10}
                           for v, q in variables]}}, telemetry={}, description=f"{expression} {unit}".strip())

    def correlator(self, name, qx, qy):
        return self.add(name, "telemetry.correlator", telemetry={},
                        xSource=[{"identifier": self.P(qx)}], ySource=[{"identifier": self.P(qy)}])

    def scatter(self, name, source_key):
        return self.add(name, "telemetry.plot.scatter-plot", [source_key], configuration={
            "styles": {}, "axes": {"xKey": "x", "yKey": "y"}, "ranges": {},
            "axisScaling": {"xAxis": "linear", "yAxis": "linear"}})

    # ---------------------------------------------------------------- conditions
    def condset(self, name, inputs, rules, default_output):
        """rules: [(label, output, trigger 'all'|'any', [(q or 'any', op, [inputs])])]; first match wins."""
        coll = []
        for i, (label, output, trigger, criteria) in enumerate(rules):
            cid = self.uid(name, "cond", str(i))
            crit = [{"id": self.uid(name, "crit", str(i), str(j)),
                     "telemetry": (q if q in ("any", "all") else self.P(q)),
                     "operation": op, "input": inp, "metadata": "value"}
                    for j, (q, op, inp) in enumerate(criteria)]
            coll.append({"id": cid, "configuration": {"name": label, "output": output, "trigger": trigger,
                                                      "criteria": crit}, "summary": label})
        coll.append({"isDefault": True, "id": self.uid(name, "cond", "default"),
                     "configuration": {"name": "Default", "output": default_output, "trigger": "all",
                                       "criteria": []}, "summary": ""})
        key = self.add(name, "conditionSet", [self.P(q) for q in inputs], telemetry={}, configuration={
            "conditionTestData": [], "conditionCollection": coll})
        return key, [c["id"] for c in coll]

    def styles(self, cs, colors, border=False):
        """colors: list of (bg, fg) per condition, default last."""
        key, cids = cs
        return {"styles": [{"conditionId": c, "style": {
            "backgroundColor": "" if border else bg, "color": fg, "border": f"2px solid {bg}" if border else "",
            "isStyleInvisible": "", "output": ""}} for c, (bg, fg) in zip(cids, colors)],
            "staticStyle": {"style": {"backgroundColor": "", "border": "", "color": ""}},
            "selectedConditionId": cids[-1], "defaultConditionId": cids[-1],
            "conditionSetIdentifier": self.ref(key)}

    def widget(self, name, cs, colors, label=None):
        key = self.add(name, "conditionWidget", label=label or name, conditionalLabel="", url="",
                       configuration={"objectStyles": self.styles(cs, colors),
                                      "useConditionSetOutputAsLabel": label is None})
        return key

    # ---------------------------------------------------------------- containers
    def flex(self, name, containers, rows=True):
        """containers: [(size, [(key, size), ...])]"""
        cfg = {"containers": [], "rowsLayout": rows}
        comp = []
        as_id = lambda k: k if isinstance(k, dict) else self.ref(k)  # noqa: E731
        for ci, (csize, frames) in enumerate(containers):
            cfg["containers"].append({"id": self.uid(name, "c", str(ci)), "size": csize, "frames": [
                {"id": self.uid(name, "f", str(ci), str(fi)), "domainObjectIdentifier": as_id(k),
                 "size": fs, "noFrame": isinstance(k, str) and self.objects[k]["name"].endswith("status strip")
                 or (isinstance(k, str) and self.objects[k]["name"] == "Pocket status")}
                for fi, (k, fs) in enumerate(frames)]})
            for k, _ in frames:
                if as_id(k) not in comp:
                    comp.append(as_id(k))
        return self.add(name, "flexible-layout", comp, configuration=cfg)

    def lad_ids(self, name, ids):
        return self.add(name, "LadTable", [i if isinstance(i, dict) else self.ref(i) for i in ids],
                        configuration={"hiddenColumns": {"timestamp": True, "type": True, "WATCH": True, "WARNING": True, "DISTRESS": True, "CRITICAL": True, "SEVERE": True}, "isFixedLayout": False})

    def tabs(self, name, keys):
        return self.add(name, "tabs", keys, keep_alive=False, currentTabIndex=0)

    def folder(self, name, keys):
        return self.add(name, "folder", keys)

    def layout(self, name, items, grid=(10, 10)):
        """items: dicts from the helpers below; objectStyles carried per item under '_style'."""
        comp, conf_items, styles = [], [], {}
        for i, it in enumerate(items):
            it = dict(it)
            it["id"] = self.uid(name, "item", str(i))
            st = it.pop("_style", None)
            if st:
                styles[it["id"]] = st
            if it["type"] == "subobject-view":
                comp.append(it["identifier"])
            if it["type"] == "telemetry-view" and it["identifier"] not in comp:
                comp.append(it["identifier"])
            conf_items.append(it)
        return self.add(name, "layout", comp, configuration={"items": conf_items, "layoutGrid": list(grid),
                                                             "objectStyles": styles})


def text(x, y, w, h, s, size="14", color="", fill="", style=None):
    d = {"type": "text-view", "x": x, "y": y, "width": w, "height": h, "text": s, "fontSize": size,
         "font": "default", "fill": fill, "stroke": "", "color": color}
    if style:
        d["_style"] = style
    return d


def box(x, y, w, h, fill="", stroke="#666666"):
    return {"type": "box-view", "x": x, "y": y, "width": w, "height": h, "fill": fill, "stroke": stroke}


def sub(b, key, x, y, w, h, frame=False):
    return {"type": "subobject-view", "x": x, "y": y, "width": w, "height": h, "identifier": b.ref(key),
            "hasFrame": frame, "fontSize": "default", "font": "default"}


def tv(b, q_or_id, x, y, w, h, mode="all", size="16", fmt=None, style=None):
    ident = b.P(q_or_id) if isinstance(q_or_id, str) and q_or_id.startswith("/") else b.ref(q_or_id)
    d = {"type": "telemetry-view", "identifier": ident, "x": x, "y": y, "width": w, "height": h,
         "displayMode": mode, "value": "value", "stroke": "", "fill": "", "color": "", "fontSize": size,
         "font": "default"}
    if fmt:
        d["format"] = fmt
    if style:
        d["_style"] = style
    return d


def build(b, plan_start, aar_url, grader_url):
    D = lambda k: f"{DOOM}/{k}"  # noqa: E731
    GR = lambda k: f"{G}/{k}"  # noqa: E731

    # ============================================================ derived telemetry (Open MCT computes these)
    game_time = b.comps("Level game time (s)", "tic / 35", [("tic", D("TIC"))], "s")
    door_prec = b.comps("Door precision", "opens / max(presses, 1)",
                        [("opens", D("DOOR_OPENS")), ("presses", D("DOOR_PRESSES"))])
    frame_loss = b.comps("Frame reassembly loss (%)", "100 * bad / max(good + bad, 1)",
                         [("good", GR("FramesComplete")), ("bad", GR("FramesIncomplete"))], "%")
    chunks_pf = b.comps("Chunks per frame", "chunks / max(frames, 1)",
                        [("chunks", D("CHUNKS_SENT")), ("frames", D("FRAMES_SENT"))])
    mem_pct = b.comps("Memory used (%)", "100 * used / max(total, 1)",
                      [("used", f"{FSW}/DoomSat/systemResources/MEMORY_USED"),
                       ("total", f"{FSW}/DoomSat/systemResources/MEMORY_TOTAL")], "%")
    track_src = b.correlator("Player position (x, y)", D("POS_X"), D("POS_Y"))
    traverse = b.scatter("Traverse (built-in scatter)", track_src)

    # ============================================================ condition sets: one meaning per colour
    # On a parameter with alarm ranges (alarm-ranges.json, doom-ground.xtce.xml) a rule's colour is the alarm
    # level of the values it catches, so the strip, the limit lines and Fault Management agree.
    cs_vitals = b.condset("CS Vitals", [D("HEALTH"), D("DEAD")], [
        ("Dead", "DEAD", "any", [(D("DEAD"), "enumValueIs", ["255"])]),
        ("Critical", "HULL CRITICAL", "any", [(D("HEALTH"), "lessThan", [11])]),
        ("Warning", "HULL DAMAGED", "any", [(D("HEALTH"), "lessThan", [25])]),
        ("Watch", "HULL LOW", "any", [(D("HEALTH"), "lessThan", [50])])], "HULL OK")
    vit_colors = [(BAD, "#fff"), (BAD, "#fff"), (WARN, "#fff"), (WATCH, "#000"), (OK, "#fff")]
    cs_link = b.condset("CS Payload link", [D("PAYLOAD_LINK")], [
        ("Lost", "LINK NO GO", "any", [(D("PAYLOAD_LINK"), "enumValueIs", ["0"])]),
        ("Up", "LINK GO", "any", [(D("PAYLOAD_LINK"), "enumValueIs", ["255"])])], "LINK ?")
    link_colors = [(BAD, "#fff"), (OK, "#fff"), (OFF, "#ccc")]
    cs_auto = b.condset("CS Autonomy timing", [GR("DecisionAgeMs"), GR("SystemOneLatencyMs")], [
        ("Over budget", "DECISION LATE", "any", [(GR("DecisionAgeMs"), "greaterThan", [900])]),
        ("Warning", "DECISION SLOW", "any", [(GR("DecisionAgeMs"), "greaterThan", [850])]),
        ("Watch", "DECISION SLOWING", "any", [(GR("DecisionAgeMs"), "greaterThan", [750])]),
        ("In budget", "DECISIONS GO", "any", [(GR("DecisionAgeMs"), "lessThanOrEq", [750])])], "DECISIONS ?")
    auto_colors = [(BAD, "#fff"), (WARN, "#fff"), (WATCH, "#000"), (OK, "#fff"), (OFF, "#ccc")]
    cs_share = b.condset("CS jev share", [GR("JevShare")], [
        ("Below floor", "JEV SHARE < 0.70", "any", [(GR("JevShare"), "lessThan", [0.7])]),
        ("At floor", "JEV SHARE OK", "any", [(GR("JevShare"), "greaterThanOrEq", [0.7])])], "JEV SHARE ?")
    share_colors = [(WARN, "#fff"), (OK, "#fff"), (OFF, "#ccc")]
    cs_honest = b.condset("CS Honesty", [GR("HonestyStatus")], [
        ("Fail", "HONESTY FAIL: VOID", "any", [(GR("HonestyStatus"), "enumValueIs", ["2"])]),
        ("Pass", "HONESTY PASS", "any", [(GR("HonestyStatus"), "enumValueIs", ["1"])])], "HONESTY UNKNOWN")
    honest_colors = [(BAD, "#fff"), (OK, "#fff"), (OFF, "#ccc")]
    modes = ["EXPLORE", "APPROACH", "OPERATE", "FIGHT", "RETREAT", "RECOVER"]
    cs_mode = b.condset("CS Intent mode", [GR("IntentMode")], [
        (m, m, "any", [(GR("IntentMode"), "enumValueIs", [str(i)])]) for i, m in enumerate(modes)], "NO INTENT")
    mode_colors = [(MODE_BG[m], "#fff") for m in modes] + [(OFF, "#ccc")]
    cs_budget = b.condset("CS Level budget", [D("TIC")], [
        ("Over 180 s", "OVER BUDGET", "any", [(D("TIC"), "greaterThan", [6300])]),
        ("Last 30 s", "BUDGET WATCH", "any", [(D("TIC"), "greaterThan", [5250])])], "IN BUDGET")
    budget_colors = [(BAD, "#fff"), (WATCH, "#000"), ("", "")]
    keys_cs = {}
    for colour, bits, hexc in (("RED", "1,3,5,7", "#cc0000"), ("BLUE", "2,3,6,7", "#1155cc"),
                               ("YELLOW", "4,5,6,7", "#f1c232")):
        keys_cs[colour] = (b.condset(f"CS Key {colour.lower()}", [D("KEYS")], [
            ("Held", f"{colour} KEY", "any", [(D("KEYS"), "isOneOf", [bits])])], "-"), hexc)
    cs_threat = b.condset("CS Threat", [D("ENEMY_COUNT")], [
        ("Swarm", "HAZARDS 4+", "any", [(D("ENEMY_COUNT"), "greaterThan", [3])]),
        ("Several", "HAZARDS 2+", "any", [(D("ENEMY_COUNT"), "greaterThan", [1])]),
        ("Contact", "HAZARD CONTACT", "any", [(D("ENEMY_COUNT"), "greaterThanOrEq", [1])])], "NO HAZARDS")
    threat_colors = [(WARN, "#fff"), (WATCH, "#000"), (OK, "#fff"), (OK, "#fff")]

    w_vitals = b.widget("Vitals", cs_vitals, vit_colors)
    w_link = b.widget("Payload link", cs_link, link_colors)
    w_auto = b.widget("Decision timing", cs_auto, auto_colors)
    w_share = b.widget("jev share", cs_share, share_colors)
    # label mode: with no HonestyStatus published yet the widget must still say what it is (openmct #5925)
    w_honest = b.widget("Honesty", cs_honest, honest_colors, label="HONESTY (preflight)")
    w_mode = b.widget("Intent mode", cs_mode, mode_colors)
    w_threat = b.widget("Hazards", cs_threat, threat_colors)
    w_keys = {c: b.widget(f"{c.title()} key", cs, [(hx, "#000"), (OFF, "#888")]) for c, (cs, hx) in keys_cs.items()}

    # ============================================================ custom views
    radar = b.add("Sector radar", "doomsat.sectors", configuration={})
    board = b.add("Candidate board", "doomsat.candidates", configuration={})

    # ============================================================ images and clocks
    frame_img = b.P(f"{G}/DoomFrame")
    map_img = b.P(f"{G}/DoomMap")
    utc = b.add("UTC", "clock", configuration={"baseFormat": "YYYY/MM/DD hh:mm:ss", "use24": "clock24",
                                               "timezone": "UTC"})

    # ============================================================ 00 overview: the front-room wall
    status = b.layout("Mission status strip", [
        text(0, 0, 26, 3, "DOOMSAT", "28", "#ffffff"),
        text(0, 3, 26, 2, "payload Doom · flight F´ · CCSDS · Yamcs · ground jev + Sonnet", "10", "#9fc5e8"),
        sub(b, utc, 0, 5, 26, 3),
        sub(b, w_vitals, 27, 0, 16, 4), sub(b, w_link, 44, 0, 16, 4), sub(b, w_auto, 61, 0, 18, 4),
        sub(b, w_share, 80, 0, 18, 4), sub(b, w_honest, 99, 0, 20, 4), sub(b, w_mode, 120, 0, 16, 4),
        sub(b, w_threat, 137, 0, 18, 4),
        text(27, 5, 9, 3, "LEVEL", "11", "#aaaaaa"), tv(b, D("LEVEL"), 36, 5, 6, 3, "value", "20"),
        text(43, 5, 10, 3, "ATTEMPT", "11", "#aaaaaa"), tv(b, GR("Attempt"), 53, 5, 6, 3, "value", "20"),
        text(60, 5, 12, 3, "GAME TIME s", "11", "#aaaaaa",
             style=b.styles(cs_budget, budget_colors)),
        tv(b, game_time, 72, 5, 9, 3, "value", "20", "%.1f", style=b.styles(cs_budget, budget_colors)),
        text(82, 5, 7, 3, "/ 180", "11", "#aaaaaa"),
        text(90, 5, 8, 3, "KILLS", "11", "#aaaaaa"), tv(b, D("KILLS"), 98, 5, 6, 3, "value", "20"),
        text(105, 5, 9, 3, "INTENT", "11", "#aaaaaa"), tv(b, D("INTENT_ID"), 114, 5, 8, 3, "value", "20"),
        text(123, 5, 11, 3, "CELLS SEEN", "11", "#aaaaaa"), tv(b, D("EXPLORED_CELLS"), 134, 5, 8, 3, "value", "20"),
        sub(b, w_keys["RED"], 143, 5, 4, 3), sub(b, w_keys["BLUE"], 147, 5, 4, 3),
        sub(b, w_keys["YELLOW"], 151, 5, 4, 3),
    ])
    vit_health = b.gauge("Hull integrity (HEALTH)", D("HEALTH"), "dial-filled", 0, 200, 25, "")
    vit_armor = b.gauge("Shielding (ARMOR)", D("ARMOR"), "meter-vertical", 0, 200)
    vit_shells = b.gauge("Consumables: shells", D("SHELLS"), "meter-vertical", 0, 50, 4, "")
    vit_bullets = b.gauge("Consumables: bullets", D("BULLETS"), "meter-vertical", 0, 200, 10, "")
    vitals = b.layout("Vitals", [
        text(0, 0, 38, 2, "PAYLOAD VITALS: the player, read as a spacecraft", "10", "#9fc5e8"),
        sub(b, vit_health, 0, 2, 19, 15),
        sub(b, vit_armor, 20, 2, 6, 13), sub(b, vit_shells, 26, 2, 6, 13), sub(b, vit_bullets, 32, 2, 6, 13),
        text(0, 16, 19, 2, "HULL (HEALTH)", "10", "#aaaaaa"),
        text(20, 15, 6, 2, "ARMOR", "9", "#aaaaaa"), text(26, 15, 6, 2, "SHELLS", "9", "#aaaaaa"),
        text(32, 15, 6, 2, "BULLETS", "9", "#aaaaaa"),
        text(0, 19, 10, 3, "WEAPON", "11", "#aaaaaa"), tv(b, D("WEAPON"), 10, 19, 12, 3, "value", "15"),
        text(23, 19, 9, 3, "HAZARDS", "11", "#aaaaaa"), tv(b, D("ENEMY_COUNT"), 32, 19, 6, 3, "value", "15"),
        text(0, 23, 10, 3, "RANGE u", "11", "#aaaaaa"), tv(b, D("ENEMY_DIST"), 10, 23, 12, 3, "value", "15"),
        text(23, 23, 9, 3, "EXIT u", "11", "#aaaaaa"), tv(b, D("EXIT_DIST"), 32, 23, 6, 3, "value", "15"),
        text(0, 27, 10, 3, "AHEAD", "11", "#aaaaaa"), tv(b, D("AHEAD_KIND"), 10, 27, 16, 3, "value", "15"),
    ])
    autonomy_lad = b.lad("Ground brain (latest)", [GR("Controls"), GR("EngageAnswer"), GR("Plan"), GR("SystemTwoHint"),
                                                   GR("DecisionSource"), GR("PickKind"), GR("PickGap"),
                                                   GR("PickConfidence"), GR("SystemOneLatencyMs"), GR("DecisionAgeMs"),
                                                   GR("JevShare"), GR("GraphVersion")])
    events_all = b.table("F´ and ground events", [b.events_id()], 200)
    overview = b.flex("00 Mission overview", [
        (12, [(status, 100)]),
        (55, [(frame_img, 42), (vitals, 30), (radar, 28)]),
        (33, [(events_all, 34), (board, 42), (autonomy_lad, 24)]),
    ])

    # ============================================================ 10 GAME
    vit_plot = b.plot("Vitals over time", [D("HEALTH"), D("ARMOR"), D("SHELLS"), D("BULLETS")], stacked=True)
    surroundings = b.lad("Surroundings", [D("ENEMY_COUNT"), D("ENEMY_DIST"), D("ENEMY_BEARING"), D("THREAT_CLASS"),
                                          D("THREAT_COUNT"), D("EXIT_DIST"), D("EXIT_BEARING"), D("KEY_DIST"),
                                          D("KEY_BEARING"), D("HEALTH_ITEM_DIST"), D("AMMO_ITEM_DIST"),
                                          D("ARMOR_ITEM_DIST"), D("AHEAD_KIND"), D("AHEAD_DIST"), D("DOOR_AHEAD")])
    inventory = b.lad("Inventory and progress", [D("WEAPON"), D("OWN_SHOTGUN"), D("SHELLS"), D("BULLETS"), D("ARMOR"),
                                                 D("KEYS"), D("KILLS"), D("LEVEL"), D("EPISODE"), D("LEVEL_DONE"),
                                                 D("DEAD"), D("EXPLORED_CELLS")])
    game = b.flex("10 GAME · the payload", [
        (50, [(frame_img, 64), (vit_plot, 36)]),
        (25, [(radar, 55), (surroundings, 45)]),
        (25, [(map_img, 45), (inventory, 55)]),
    ], rows=False)

    # ============================================================ 20 ONBOARD
    dirs = ("FWD", "AL", "LEFT", "BL", "BACK", "BR", "RIGHT", "AR")
    # Open MCT's bar graph wants ONE array-valued source; eight scalars make it ask to replace its source.
    # The radar draws these; the tables give the numbers. (Publish an array parameter to use the bar graph.)
    rays = b.lad("Map rays: open way per direction (u)", [D(f"CLEAR_{k}") for k in dirs] + [D("CLEAR_MAP_FWD"),
                                                                                             D("CLEAR_FL"), D("CLEAR_FR")])
    novelty = b.lad("New ground and doors per direction", [D(f"NEW_{k}") for k in dirs] + [D(f"DOOR_{k}") for k in dirs])
    coverage = b.plot("Cells seen this attempt", [D("EXPLORED_CELLS")])
    executor = b.lad("Executor and world model", [D("INTENT_ID"), D("GOAL"), D("CAND_COUNT"), D("STUCK"),
                                                  D("WATCHDOG_TRIPS"), D("DOOR_PRESSES"), D("DOOR_OPENS"),
                                                  D("HINT_ACTIVE"), D("HINT_REL"), D("TIC")])
    dp_gauge = b.add("Door precision (derived)", "gauge", [b.ref(door_prec)], configuration={"gaugeController": {
        "gaugeType": "dial-needle", "isDisplayMinMax": True, "isDisplayCurVal": True, "isDisplayUnits": False,
        "isUseTelemetryLimits": False, "limitLow": 0.1, "limitHigh": "", "max": 1, "min": 0, "precision": 2}})
    world = b.flex("World model and executor", [
        (50, [(board, 58), (traverse, 42)]),
        (25, [(radar, 50), (executor, 50)]),
        (25, [(rays, 30), (novelty, 40), (coverage, 30)]),
    ], rows=False)
    rg = [f"{FSW}/DoomSat/rateGroup_{r}" for r in ("20Hz", "1Hz", "0_25Hz")]
    rg_time = b.plot("Rate group max time (us)", [f"{r}/RgMaxTime" for r in rg])
    rg_slip = b.plot("Rate group cycle slips", [f"{r}/RgCycleSlips" for r in rg])
    cpu = b.gauge("CPU (%)", f"{FSW}/DoomSat/systemResources/CPU", "dial-filled", 0, 100, "", 90)
    mem = b.add("Memory used (derived %)", "gauge", [b.ref(mem_pct)], configuration={"gaugeController": {
        "gaugeType": "dial-filled", "isDisplayMinMax": True, "isDisplayCurVal": True, "isDisplayUnits": True,
        "isUseTelemetryLimits": False, "limitLow": "", "limitHigh": 90, "max": 100, "min": 0, "precision": 1}})
    fsw_lad = b.lad("Command, event and buffer health", [
        f"{FSW}/CdhCore/cmdDisp/CommandsDispatched", f"{FSW}/CdhCore/cmdDisp/CommandErrors",
        f"{FSW}/CdhCore/cmdDisp/CommandsDropped", f"{FSW}/CdhCore/events/EventsDropped",
        f"{FSW}/CdhCore/health/PingLateWarnings", f"{FSW}/ComCcsds/commsBufferManager/CurrBuffs",
        f"{FSW}/ComCcsds/commsBufferManager/HiBuffs", f"{FSW}/ComCcsds/commsBufferManager/NoBuffs",
        f"{FSW}/ComCcsds/commsBufferManager/EmptyBuffs", D("PAYLOAD_LINK"), D("CMDS_RECEIVED"),
        f"{FSW}/CdhCore/version/FrameworkVersion", f"{FSW}/CdhCore/version/ProjectVersion"])
    fsw = b.flex("Flight software (F´)", [
        (50, [(rg_time, 50), (rg_slip, 50)]),
        (20, [(cpu, 50), (mem, 50)]),
        (30, [(fsw_lad, 100)]),
    ], rows=False)
    dl_counts = b.plot("Image products sent", [D("FRAMES_SENT"), D("CHUNKS_SENT")])
    dl_bytes = b.plot("Bytes in the last frame", [D("FRAME_BYTES")])
    dl_lad = b.lad("Downlink products (latest)", [D("FRAMES_SENT"), D("CHUNKS_SENT"), D("FRAME_BYTES"), D("PAYLOAD_LINK")])
    downlink = b.flex("Downlink products", [
        (60, [(dl_counts, 50), (dl_bytes, 50)]),
        (40, [(dl_lad, 50), (b.lad_ids("Chunks per frame (derived)", [chunks_pf]), 50)]),
    ], rows=False)
    onboard = b.tabs("20 ONBOARD · flight side", [world, fsw, downlink])

    # ============================================================ 30 GROUND
    timing = b.plot("Decision timing (ms): jev round trip, decision age", [GR("SystemOneLatencyMs"),
                                                                          GR("DecisionAgeMs")])
    scores = b.plot("jev target scores by slot (nine-level rubric)", [GR(f"Score{n}") for n in range(8)])
    share = b.plot("jev share and fallback rate (rolling)", [GR("JevShare"), GR("FallbackRate")])
    pick = b.plot("Pick gap and confidence", [GR("PickGap"), GR("PickConfidence")])
    brain = b.flex("Autonomy: jev and Sonnet", [
        (45, [(timing, 50), (share, 50)]),
        (35, [(board, 55), (pick, 45)]),
        (20, [(autonomy_lad, 65), (dp_gauge, 35)]),
    ], rows=False)
    gds_frames = b.plot("Ground reassembly: frames complete and incomplete", [GR("FramesComplete"),
                                                                             GR("FramesIncomplete")])
    loss_plot = b.add("Frame loss (derived %)", "telemetry.plot.overlay", [b.ref(frame_loss)],
                      configuration={"series": [{"identifier": b.ref(frame_loss), "yAxisId": 1}],
                                     "yAxis": {}, "xAxis": {}})
    cmd_plot = b.plot("Uplink: commands issued by the pilot", [GR("ControlCommands")])
    gds_lad = b.lad("Ground data system (latest)", [GR("FrameSeq"), GR("FramesComplete"), GR("FramesIncomplete"),
                                           GR("ControlCommands"), GR("SystemTwoLatencyMs")])
    gds = b.flex("Ground data system", [
        (50, [(gds_frames, 50), (loss_plot, 50)]),
        (50, [(cmd_plot, 50), (gds_lad, 50)]),
    ], rows=False)
    cmds = b.table("Command history (Yamcs)", [{"key": "yamcs.commands", "namespace": "taxonomy"}], 100)
    warn_events = b.table("Events: warning and above", [b.events_id("warning")], 100)
    history = b.flex("Command and event history", [
        (50, [(cmds, 100)]), (50, [(events_all, 55), (warn_events, 45)])], rows=False)
    ground = b.tabs("30 GROUND · the other half of the loop", [brain, gds, history])

    # ============================================================ 40 TIME STRIP and the campaign plan
    budget_ms = 180_000
    plan_body = {"E1 campaign: 180 s budget per level": [
        {"name": f"E1M{i + 1}", "id": f"e1m{i + 1}", "start": plan_start + i * budget_ms,
         "end": plan_start + (i + 1) * budget_ms, "type": "level", "color": "#0b5394", "textColor": "#ffffff",
         "properties": {"budget_s": 180}} for i in range(8)],
        "Sonnet checkpoints (every 60 s)": [
        {"name": "bump", "id": f"bump{j}", "start": plan_start + j * 60_000, "end": plan_start + j * 60_000 + 3000,
         "type": "review", "color": "#741b47", "textColor": "#ffffff"} for j in range(1, 24)]}
    plan = b.add("E1 campaign plan", "plan", selectFile={"name": "e1-campaign.json",
                                                         "body": json.dumps(plan_body)},
                 configuration={"clipActivityNames": True, "aheadBehind": {"duration": 0, "isBehind": False},
                                "swimlaneVisibility": {g: True for g in plan_body}},
                 sourceMap={"orderedGroups": None})
    b.objects[plan].pop("sourceMap")
    timelist = b.add("E1 campaign timelist", "timelist", [b.ref(plan)], configuration={
        "sortOrderIndex": 0, "currentEventsIndex": 1, "filter": "", "filterMetadata": "", "isCompact": False})
    hv = b.plot("Hull and consumables", [D("HEALTH"), D("ARMOR"), D("SHELLS")])
    strip = b.add("40 TIME STRIP · one clock for everything", "time-strip",
                  [b.ref(plan), frame_img, b.ref(hv), b.ref(timing), b.events_id("info"), b.ref(coverage)],
                  configuration={"useIndependentTime": False, "containers": [], "swimLaneLabelWidth": 200})

    # ============================================================ 50 AFTER-ACTION (and the grader wall)
    aar_page = b.add("E1M1: every attempt (research ledger chart)", "webPage", url=aar_url)
    links = [b.add(n, "hyperlink", url=u, displayText=n, displayFormat="button", linkTarget="_blank")
             for n, u in (("Yamcs web UI", "http://localhost:8090/"),
                          ("Charter (knowledge boundary, metrics)", "https://github.com/Devonance/DoomSat/blob/main/docs/CHARTER.md"),
                          ("E1M1 finish decision log", "https://github.com/Devonance/DoomSat/blob/main/docs/results/e1m1-finished-decision-log.md"))]
    grader_items = [
        box(0, 0, 60, 6, BAD, ""),
        text(1, 0, 58, 6, "GRADER WALL: post-flight only. WAD-derived. Never in a live display, never read "
                          "by the pilot, jev or Sonnet (charter 2.4, honesty test 6).", "12", "#ffffff"),
    ]
    if grader_url:
        grader_items.append({"type": "image-view", "x": 0, "y": 7, "width": 60, "height": 40, "url": grader_url,
                             "stroke": "#666666", "fill": ""})
    grader = b.layout("Grader wall (post-flight only)", grader_items)
    aar = b.folder("50 AFTER-ACTION", [aar_page, timelist, grader, *links])

    # ============================================================ 60 POCKET (phone)
    pocket_strip = b.layout("Pocket status", [sub(b, w_vitals, 0, 0, 16, 4), sub(b, w_mode, 17, 0, 16, 4),
                                              sub(b, w_auto, 0, 5, 16, 4), sub(b, w_link, 17, 5, 16, 4)])
    pocket_lad = b.lad("Pocket numbers", [D("HEALTH"), D("ARMOR"), D("SHELLS"), D("KILLS"), D("LEVEL"),
                                          GR("DecisionAgeMs"), GR("JevShare")])
    pocket = b.flex("60 POCKET · phone view", [(18, [(pocket_strip, 100)]), (50, [(frame_img, 100)]),
                                                (32, [(pocket_lad, 100)])])

    # ============================================================ parts bins
    conds = b.folder("Conditions", [k for k, o in b.objects.items() if o["type"] in ("conditionSet",
                                                                                      "conditionWidget")])
    derived = b.folder("Derived telemetry", [game_time, door_prec, frame_loss, chunks_pf, mem_pct, track_src])
    parts = b.folder("Parts (duplicate into My Items to build new screens)",
                     [radar, board, traverse, rays, novelty, scores, vit_health, utc])
    b.bar  # kept for array-valued sources
    root = b.folder("DoomSat Displays", [overview, game, onboard, ground, strip, aar, pocket, conds, derived, parts])
    return root


def locate(b, root):
    """Every object's location is its first parent; the root sits under ROOT."""
    b.objects[root]["location"] = "ROOT"
    stack = [root]
    while stack:
        k = stack.pop()
        for c in b.objects[k].get("composition", []):
            if c.get("namespace") == "" and b.objects[c["key"]]["location"] is None:
                b.objects[c["key"]]["location"] = k
                stack.append(c["key"])
    orphans = [o["name"] for o in b.objects.values() if o["location"] is None]
    for o in b.objects.values():
        if o["location"] is None:
            o["location"] = root
    return orphans


ROOT = doomdict.ROOT
WEB = ROOT / "ground" / "openmct"
DOC = ROOT / "docs" / "OPENMCT.md"
DEFAULT_PLAN_START = 1790000000000


def build_all(target="live", pack=None, plan_start=None):
    """Build the tree in memory. Returns (builder, root key, dictionary, drift)."""
    params, drift = doomdict.load()
    grader_url = None
    if pack:
        meta = json.loads(Path(pack).read_text(encoding="utf-8"))["meta"]
        plan_start = plan_start or meta["t0"]
        g = sorted((Path(pack).resolve().parent / "grader").glob("*.png"))
        if g:
            grader_url = g[0].resolve().relative_to(WEB.resolve()).as_posix()
    b = Builder(params, target)
    root = build(b, plan_start or DEFAULT_PLAN_START,
                 "aar/e1m1-progress.html" if target == "replay" else "/aar/e1m1-progress.html", grader_url)
    orphans = locate(b, root)
    if orphans:
        raise SystemExit(f"objects with no parent: {orphans}")
    return b, root, params, drift


def display_json(b, root):
    return json.dumps({"openmct": b.objects, "rootId": root}, indent=1, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["live", "replay"], default="live")
    ap.add_argument("--pack", help="replay pack; anchors the campaign plan at the recording's start")
    ap.add_argument("--plan-start", type=int, help="epoch ms the campaign plan starts (live: the flight start)")
    ap.add_argument("--doc", action="store_true", help="also regenerate docs/OPENMCT.md")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    b, root, params, drift = build_all(a.target, a.pack, a.plan_start)
    # --plan-start never writes the committed file: that one keeps DEFAULT_PLAN_START, which the tests check
    name = ("doomsat-displays.replay.json" if a.target == "replay" else
            "doomsat-displays.campaign.json" if a.plan_start else "doomsat-displays.json")
    out = Path(a.out) if a.out else WEB / "displays" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(display_json(b, root), encoding="utf-8", newline="\n")
    types = {}
    for o in b.objects.values():
        types[o["type"]] = types.get(o["type"], 0) + 1
    print(f"{out.relative_to(ROOT)}: {len(b.objects)} objects, {len(b.used)} telemetry references, "
          "all known to the dictionary")
    print("  object types: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())))
    views = set(openmct_docs.SECTOR_RADAR_INPUTS + openmct_docs.CANDIDATE_BOARD_INPUTS)
    lagging = {q for q in (b.drift_used | views) if "snapshot lag" in params.get(q, {}).get("source", "")}
    if lagging:
        names = sorted({q.rsplit("/", 1)[1].split(".")[0] for q in lagging})
        print(f"  {len(names)} channels on screen are in Doom.fpp but not in the committed fprime.xtce.xml snapshot: "
              + ", ".join(names) + ". Live Yamcs regenerates its XTCE from the F' dictionary at launch, so this is "
              "the snapshot lagging, not a live gap.")
    if a.doc:
        if a.target != "live" or a.plan_start:
            raise SystemExit("--doc documents the committed live build: run it without --target/--plan-start")
        DOC.write_text(openmct_docs.render(b, root, params, drift), encoding="utf-8", newline="\n")
        print(f"{DOC.relative_to(ROOT)} regenerated")


if __name__ == "__main__":
    main()
