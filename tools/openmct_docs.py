"""docs/OPENMCT.md, written from the same objects tools/build_openmct_displays.py builds.

The operator reference cannot drift from the displays: every panel is documented from NOTES below, every
indicator's rules and every derived value's formula are read out of the built objects, and every parameter
comes from the merged dictionary. tests/test_openmct.py fails if a panel has no note, a parameter has no
meaning, or the committed docs/OPENMCT.md differs from what this module renders.
"""
import re

DOOM = "/DoomSat_DoomSat/DoomSat/doom"
G = "/DoomGround"

# ------------------------------------------------------------------ what each panel is for
NOTES = {
    # displays
    "00 Mission overview": "The front-room wall. One screen that answers two questions: is this attempt going well, and does anything need a person. Status strip on top, the player's view, vitals and the sector radar in the middle, and the last few seconds of the loop (events, the candidate board, the ground brain) along the bottom.",
    "10 GAME · the payload": "Doom's player read as a spacecraft payload: what it sees, what it is carrying, what is around it, and the map it has drawn so far.",
    "20 ONBOARD · flight side": "Everything onboard: the payload's world model and executor, the F´ flight computer, and the image products going down.",
    "30 GROUND · the other half of the loop": "Everything on the ground: the pilot, jev (System One) and Sonnet (System Two), the ground data system, and the record of what went up and what came down.",
    "40 TIME STRIP · one clock for everything": "The attempt on one time axis: the campaign plan, the camera, hull and consumables, decision timing, events and coverage. Scroll the time conductor and every lane moves together.",
    "50 AFTER-ACTION": "Post-flight review. The research ledger's chart of every attempt, the campaign as a list, the Grader Wall and links out to Yamcs and the charter.",
    "60 POCKET · phone view": "The overview cut down for a phone: four indicators, the camera, seven numbers.",
    # tabs
    "World model and executor": "What the onboard world model offered the ground, what jev thought of it, where the player has walked and what the executor is doing about it.",
    "Flight software (F´)": "The F´ deployment as a flight computer: rate group timing and slips, CPU and memory, the command dispatcher, the event logger, health pings and the com buffer pool.",
    "Downlink products": "The JPEG frame product: frames and 960-byte chunks sent, the size of the last frame, and chunks per frame.",
    "Autonomy: jev and Sonnet": "The decision loop on the ground: how long a decision takes against the 900 ms budget, how many intent changes jev actually decided (the charter's 70% floor), how clear-cut each pick was, and what jev and Sonnet said last.",
    "Ground data system": "The ground half of the link: frame reassembly and loss, and the rate at which the pilot sends commands up.",
    "Command and event history": "What went up (every INTENT, with its arguments) and what came down (F´ events, and in replay the events inferred from telemetry).",
    # layouts and strips
    "Mission status strip": "Seven indicators and the numbers a flight director glances at: level, attempt, game time against the 180 s budget, kills, the current intent, cells seen, keys held.",
    "Vitals": "Hull integrity as a dial, shielding and consumables as meters, then weapon, hazards and range, the exit if one has been recognised, and what is at arm's length.",
    "Pocket status": "The four indicators that matter on a phone: hull, intent mode, decision timing, payload link.",
    "Grader wall (post-flight only)": "The grader's overlay of the attempt on the true level geometry. WAD-derived, so post-flight only: no live screen links here, and nothing here is read by the pilot, jev or Sonnet.",
    # custom views
    "Sector radar": "The payload's eight-direction sensing around the player, forward up. See Custom views.",
    "Candidate board": "The world model's candidate targets, jev's score for each, the pick and why, beside a plan view of the traverse. See Custom views.",
    # plots
    "Vitals over time": "HEALTH, ARMOR, SHELLS and BULLETS stacked, each with its alarm limits drawn.",
    "Hull and consumables": "HEALTH, ARMOR and SHELLS on one axis, for the time strip.",
    "Cells seen this attempt": "EXPLORED_CELLS: 32-unit cells the player has stood in. Flat for long means circling; it resets to 1 at every level start (honesty test 3).",
    "Decision timing (ms): jev round trip, decision age": "jev's round trip and the approximate decision age, with their alarm limits (decision age critical past the 900 ms budget).",
    "jev share and fallback rate (rolling)": "Share of the last 40 intent changes that a jev answer decided, against the charter's 0.70 floor; and share of the last 40 decisions settled by the unsure band, a hold, the cache or a rule, or with no jev answer.",
    "Pick gap and confidence": "How far the chosen candidate's score was clear of the next one, and jev's confidence in it. Small gaps are what the unsure band exists for.",
    "jev target scores by slot (nine-level rubric)": "Score0 to Score7 over time. A slot's line jumping is the world model reshuffling its offers, not jev changing its mind.",
    "Rate group max time (us)": "Longest cycle of each rate group since the last report. The 20 Hz group has 50,000 us; alarms at 25,000, 40,000 and 50,000.",
    "Rate group cycle slips": "Cycles each rate group failed to finish in time. Anything above zero is a warning.",
    "Image products sent": "Cumulative frames and chunks the Doom component has pushed into the com queue.",
    "Bytes in the last frame": "Size of the most recent JPEG. Frames over the chunk budget are dropped onboard (event FrameTooLarge).",
    "Ground reassembly: frames complete and incomplete": "How many frames the pilot put back together from FRAME_CHUNK records, and how many arrived with chunks missing.",
    "Frame loss (derived %)": "Incomplete frames as a percentage of all frames the ground saw.",
    "Uplink: commands issued by the pilot": "Cumulative INTENT (or CONTROL) commands the pilot has sent. The slope is the decision rate.",
    "Traverse (built-in scatter)": "POS_X against POS_Y through Open MCT's own Correlation Telemetry and Scatter Plot, for comparison with the candidate board's plan view.",
    # tables
    "F´ and ground events": "Every event Yamcs has, newest first. In replay these are inferred from telemetry and marked [derived].",
    "Events: warning and above": "The same, filtered to WARNING and worse.",
    "Command history (Yamcs)": "Every command that went up, with its arguments and acknowledgement state. In replay: the INTENTs the pilot actually sent (acknowledgements were not recorded).",
    "Surroundings": "Everything the payload reports about what is near the player: the nearest enemy, the worst threat in view, the exit and key if recognised, the nearest pickups, and what is at arm's length.",
    "Inventory and progress": "Weapon and ammunition, keys, kills, level and episode, and whether the level is done or the player is dead.",
    "Executor and world model": "The intent being executed, the goal the ground last set (reported only), how many candidates are on offer, whether the executor is stuck, watchdog trips, door presses and opens, Sonnet's exploration hint, and the game tic.",
    "Map rays: open way per direction (u)": "Open way in the eight directions: map rays, except CLEAR_FWD, which is the range camera straight ahead. Then the map ray straight ahead (CLEAR_MAP_FWD) and the range camera's ahead-left and ahead-right bands.",
    "New ground and doors per direction": "Percentage of never-walked ground (255 = ground the map has not seen lies that way), and distance to a door (in 8-unit steps, 0 = none), in each of the eight directions.",
    "Ground brain (latest)": "The latest of everything the ground decided: the Controls line, the engage answer, Sonnet's plan and hint, why the last pick came out as it did, gap, confidence, timing, jev share and graph version.",
    "Command, event and buffer health": "F´ command dispatcher counters, dropped events, late health pings, the com buffer pool, the payload link and the framework and project versions.",
    "Downlink products (latest)": "Frames, chunks and bytes as latest values.",
    "Chunks per frame (derived)": "Chunks sent divided by frames sent.",
    "Ground data system (latest)": "Frame sequence, reassembly counts, commands issued and Sonnet's last round trip, as latest values.",
    "Pocket numbers": "Seven numbers for the phone: hull, shielding, shells, kills, level, decision age, jev share.",
    # gauges
    "Hull integrity (HEALTH)": "HEALTH on a 0 to 200 dial; the red band starts at 25.",
    "Shielding (ARMOR)": "ARMOR on a 0 to 200 meter.",
    "Consumables: shells": "SHELLS on a 0 to 50 meter; low band under 4.",
    "Consumables: bullets": "BULLETS on a 0 to 200 meter; low band under 10.",
    "Door precision (derived)": "Door opens divided by door presses: whether the automap's ceiling-change category is telling the truth about what is a door.",
    "CPU (%)": "F´ systemResources CPU.",
    "Memory used (derived %)": "MEMORY_USED over MEMORY_TOTAL.",
    # time and plan
    "E1 campaign plan": "The charter's mission as a plan: E1M1 to E1M8 back to back at the 180 s budget each, with Sonnet's 60 s checkpoints on a second lane. At the start of a campaign flight, build with --plan-start (a git-ignored copy, which start_openmct.sh serves for a day) so planned and actual share a clock.",
    "E1 campaign timelist": "The same plan as a list, with the current and next activity.",
    "UTC": "Wall clock, UTC.",
    # links
    "E1M1: every attempt (research ledger chart)": "docs/results/e1m1-progress.html embedded as a web page.",
    "Yamcs web UI": "Opens Yamcs (http://localhost:8090) in a new tab.",
    "Charter (knowledge boundary, metrics)": "Opens docs/CHARTER.md on GitHub.",
    "E1M1 finish decision log": "Opens docs/results/e1m1-finished-decision-log.md on GitHub.",
}

# ------------------------------------------------------------------ what each parameter means
DIRS = {"FWD": "straight ahead", "AL": "ahead-left (45°)", "LEFT": "left (90°)", "BL": "behind-left (135°)",
        "BACK": "behind", "BR": "behind-right", "RIGHT": "right", "AR": "ahead-right"}
GLOSSARY = {
    f"{DOOM}/HEALTH": "Player health, 0 to 200. Alarms: watch under 50, warning under 25, critical at 10 or less.",
    f"{DOOM}/ARMOR": "Armor points, 0 to 200.",
    f"{DOOM}/SHELLS": "Shotgun shells held.",
    f"{DOOM}/BULLETS": "Bullets held (pistol, chaingun).",
    f"{DOOM}/WEAPON": "Weapon in hand: FIST, PISTOL, SHOTGUN or OTHER.",
    f"{DOOM}/OWN_SHOTGUN": "Whether the shotgun has been picked up.",
    f"{DOOM}/KILLS": "Monsters killed this episode.",
    f"{DOOM}/POS_X": "Player x, map units.",
    f"{DOOM}/POS_Y": "Player y, map units.",
    f"{DOOM}/ANGLE": "Player heading, degrees.",
    f"{DOOM}/ENEMY_COUNT": "Live enemies in view.",
    f"{DOOM}/ENEMY_BEARING": "Bearing to the nearest enemy in view, degrees, positive left.",
    f"{DOOM}/ENEMY_DIST": "Distance to the nearest enemy in view, map units.",
    f"{DOOM}/EXIT_BEARING": "Bearing to the exit line if recognised, degrees, positive left.",
    f"{DOOM}/EXIT_DIST": "Distance to the exit line; 0 until one has been recognised within 512 units this attempt.",
    f"{DOOM}/KEY_BEARING": "Bearing to a remembered key, degrees, positive left.",
    f"{DOOM}/KEY_DIST": "Distance to a remembered key; 0 when none.",
    f"{DOOM}/HEALTH_ITEM_DIST": "Distance to the nearest health pickup seen.",
    f"{DOOM}/AMMO_ITEM_DIST": "Distance to the nearest ammunition pickup seen.",
    f"{DOOM}/ARMOR_ITEM_DIST": "Distance to the nearest armor pickup seen.",
    f"{DOOM}/HEALTH_BEARING": "Bearing to that health pickup.",
    f"{DOOM}/AMMO_BEARING": "Bearing to that ammunition pickup.",
    f"{DOOM}/ARMOR_BEARING": "Bearing to that armor pickup.",
    f"{DOOM}/AHEAD_KIND": "What is at arm's length ahead: NOTHING, WALL, DOOR, EXIT, LOCKED, BARRIER (something the map does not show) or THING.",
    f"{DOOM}/AHEAD_DIST": "Distance to that thing.",
    f"{DOOM}/STUCK": "The payload thinks the player is pushing without moving. Watch alarm when True.",
    f"{DOOM}/DOOR_AHEAD": "Something usable is at arm's length.",
    f"{DOOM}/GOAL": "Goal the ground last set with SET_GOAL (EXPLORE, KILL_ENEMY, STOCK_AMMO, RESTORE_HEALTH, ADD_ARMOR, UPGRADE_WEAPON, SCOUT, HOLD). The pilot plans with its own copy; on board it is only reported, so HOLD does not stop the player.",
    f"{DOOM}/TIC": "Game tic, 35 per second, counted from the level start. The status strip turns it into game seconds.",
    f"{DOOM}/EPISODE": "Episode counter; rises on every RESET_GAME.",
    f"{DOOM}/DEAD": "The player is dead. Critical when True.",
    f"{DOOM}/LEVEL_DONE": "The exit was reached.",
    f"{DOOM}/EXPLORED_CELLS": "32-unit cells of the self-built map the player has stood in; the charter's coverage_rate counts 128-unit cells, so the two do not compare. 1 at every level start (honesty test 3).",
    f"{DOOM}/LEVEL": "Levels started so far; 1 is the first map.",
    f"{DOOM}/KEYS": "Keys held as a bitmask: red 1, blue 2, yellow 4.",
    f"{DOOM}/HINT_ACTIVE": "Sonnet's exploration hint is in force.",
    f"{DOOM}/HINT_REL": "The hint's bearing relative to the heading, degrees.",
    f"{DOOM}/FRAMES_SENT": "JPEG frames the Doom component has sent.",
    f"{DOOM}/CHUNKS_SENT": "960-byte FrameChunk records sent.",
    f"{DOOM}/FRAME_BYTES": "Bytes in the last frame.",
    f"{DOOM}/PAYLOAD_LINK": "The F´ Doom component has a live socket to the game process. Critical when False.",
    f"{DOOM}/CMDS_RECEIVED": "Commands the Doom component has received.",
    f"{DOOM}/CLEAR_FL": "Range camera, free space in the ahead-left band.",
    f"{DOOM}/CLEAR_FR": "Range camera, free space in the ahead-right band.",
    f"{DOOM}/CLEAR_MAP_FWD": "Map ray straight ahead (the range camera's CLEAR_FWD is the other reading).",
    f"{DOOM}/CAND_COUNT": "How many of the eight candidate slots are filled.",
    f"{DOOM}/INTENT_ID": "The intent the executor is carrying out; rises with every INTENT.",
    f"{DOOM}/WATCHDOG_TRIPS": "Times an executor invariant had to pull the player out of a freeze. Watch alarm above 0.",
    f"{DOOM}/THREAT_CLASS": "Worst visible monster class, by index into knowledge/doom_rules.yaml; 255 = nothing in view.",
    f"{DOOM}/THREAT_COUNT": "How many monsters are in view.",
    f"{DOOM}/DOOR_PRESSES": "Use presses at doors.",
    f"{DOOM}/DOOR_OPENS": "Use presses that opened something.",
    # F' framework
    "/DoomSat_DoomSat/DoomSat/systemResources/CPU": "Total CPU, percent. Watch over 70, warning over 90.",
    "/DoomSat_DoomSat/DoomSat/systemResources/MEMORY_USED": "Memory in use, KB.",
    "/DoomSat_DoomSat/DoomSat/systemResources/MEMORY_TOTAL": "Memory available, KB.",
    "/DoomSat_DoomSat/CdhCore/cmdDisp/CommandsDispatched": "Commands the F´ dispatcher has sent to components.",
    "/DoomSat_DoomSat/CdhCore/cmdDisp/CommandErrors": "Commands that completed with an error. Warning above 0.",
    "/DoomSat_DoomSat/CdhCore/cmdDisp/CommandsDropped": "Commands dropped (bad packet, unknown opcode). Warning above 0.",
    "/DoomSat_DoomSat/CdhCore/events/EventsDropped": "Events the event logger could not send. Watch above 0.",
    "/DoomSat_DoomSat/CdhCore/health/PingLateWarnings": "Components that answered a health ping late. Warning above 0.",
    "/DoomSat_DoomSat/ComCcsds/commsBufferManager/CurrBuffs": "Com buffers in use now.",
    "/DoomSat_DoomSat/ComCcsds/commsBufferManager/HiBuffs": "Most com buffers ever in use at once.",
    "/DoomSat_DoomSat/ComCcsds/commsBufferManager/NoBuffs": "Allocation requests refused for lack of a buffer. Warning above 0.",
    "/DoomSat_DoomSat/ComCcsds/commsBufferManager/EmptyBuffs": "Allocation requests for zero bytes.",
    "/DoomSat_DoomSat/CdhCore/version/FrameworkVersion": "F´ framework version string.",
    "/DoomSat_DoomSat/CdhCore/version/ProjectVersion": "DoomSat project version string.",
    # ground
    f"{G}/DoomFrame": "URL of the last reassembled JPEG in the Yamcs bucket doomframes (Open MCT shows it as imagery).",
    f"{G}/DoomMap": "URL of the last map product (the payload's own map, downlinked every 5 s).",
    f"{G}/SystemTwoHint": "Sonnet's last exploration bump: bearing, how long, and why.",
    f"{G}/FrameSeq": "Sequence number of the last frame the ground reassembled.",
    f"{G}/FramesComplete": "Frames reassembled with every chunk present.",
    f"{G}/FramesIncomplete": "Frames given up on with chunks missing.",
    f"{G}/SystemOneLatencyMs": "jev's round trip for the last decision, ms. Watch over 650, warning over 800.",
    f"{G}/SystemTwoLatencyMs": "Sonnet's last round trip, ms.",
    f"{G}/ControlCommands": "Commands the pilot has issued this run.",
    f"{G}/Plan": "Sonnet's last plan line, or LEVEL n FINISHED.",
    f"{G}/Controls": "One line: the mode, what was picked, and how many candidates were on offer.",
    f"{G}/DecisionAgeMs": "Telemetry age at decision + jev round trip + command issue time, ms: an approximation of the charter's observation-to-effect decision age until the payload reports the effect tic. Watch over 750, warning over 850, critical over 900.",
    f"{G}/IntentMode": "Mode of the last INTENT: EXPLORE, APPROACH, OPERATE, FIGHT, RETREAT, RECOVER.",
    f"{G}/DecisionSource": "Why the last decision came out as it did: JEV (jev was asked and its answer used), UNSURE_BAND (the gap was too small, a named fallback decided), HELD (commitment kept the previous pick), CACHED (same state as before, same answer), RULE (a rule chose: it gave up on a target that stopped getting closer, or the System One is the code rules, as under `scripts/play.sh --autopilot`), UNAVAILABLE (no jev answer: jev could not be reached in time and a rule stood in, or there was nothing to ask).",
    f"{G}/PickSlot": "Candidate slot picked, 0 to 7; 255 = none.",
    f"{G}/PickKind": "Kind of the picked candidate: FRONTIER, DOOR, EXIT, KEY, ITEM, SWITCH, ENEMY or NONE.",
    f"{G}/PickGap": "How far the pick's score was clear of the runner-up, in rubric levels.",
    f"{G}/PickConfidence": "jev's confidence in the picked candidate's score, 0 to 1.",
    f"{G}/JevShare": "Share of the last 40 intent changes (a new mode or a new pick) that a jev answer caused, by charter 7's rule. A rule's answer never counts, so it reads a little under summary.json's jev_share when jev could not be reached. Warning under the charter's 0.70 floor; blank until the first change.",
    f"{G}/FallbackRate": "Share of the last 40 decisions whose DecisionSource is not JEV.",
    f"{G}/EngageAnswer": "The engage head's last answer (fight where I stand, fight while moving, break off, retreat), when an enemy was met.",
    f"{G}/GraphVersion": "Decision graph version in use (ground/graph/graph_v<N>.json).",
    f"{G}/Attempt": "Episode (attempt) number the pilot is on.",
    f"{G}/HonestyStatus": "PASS when research/preflight.py's honesty suite (canaries included) passed and the payload was fresh (EXPLORED_CELLS 1 or less, honesty test 3); FAIL otherwise, even when preflight cleared without --require-fresh-payload. FAIL is critical: a failed honesty test voids the attempt. Preflight sets it each time it runs and reaches Yamcs, and nothing else does, so it holds the last preflight's result until Yamcs restarts; UNKNOWN before the first.",
    "/DoomOps/FlyAttempt": "Mission Status action: may an attempt be flown (GO / NO GO).",
    "/DoomOps/JevInLoop": "Mission Status action: may jev hold the loop.",
    "/DoomOps/SystemTwoReview": "Mission Status action: may Sonnet's reviews be proposed.",
    "/DoomOps/FlightStatus": "Operator Status for the FLIGHT console.",
    "/DoomOps/AutonomyStatus": "Operator Status for the AUTONOMY console.",
    "/DoomOps/GdsStatus": "Operator Status for the GDS console.",
}
CAND_MEMBERS = {"kind": "what kind of place: FRONTIER, DOOR, EXIT, KEY, ITEM, SWITCH, ENEMY",
                "x": "x, map units", "y": "y, map units",
                "pathUnits": "distance along floor the payload has seen (not the straight line)",
                "novelty": "for a frontier or a door, the unseen 32-unit cells reachable through it, a count up to 255; "
                           "usually 0 for other kinds (a place that was once a frontier or door keeps its count)",
                "threatCount": "live things near it"}


def describe(q, params):
    if q in GLOSSARY:
        return GLOSSARY[q]
    m = re.fullmatch(rf"{DOOM}/(CLEAR|NEW|DOOR)_(\w+)", q)
    if m and m.group(2) in DIRS:
        what = {"CLEAR": "Map ray: open way, map units,", "NEW": "Percent of not-yet-walked ground",
                "DOOR": "Distance to a door in 8-unit steps (0 = none)"}[m.group(1)]
        if m.group(1) == "CLEAR" and m.group(2) == "FWD":
            return "Range camera: free space straight ahead, map units."
        unseen = "; 255 = ground the map has not seen lies that way" if m.group(1) == "NEW" else ""
        return f"{what} {DIRS[m.group(2)]}{unseen}."
    m = re.fullmatch(rf"{G}/Score(\d)", q)
    if m:
        return f"jev's target-head score for candidate slot {m.group(1)}, on the nine-level rubric; 0 = empty slot."
    m = re.fullmatch(rf"{DOOM}/CAND(\d)\.(\w+)", q)
    if m and m.group(2) in CAND_MEMBERS:
        return f"Candidate slot {m.group(1)}: {CAND_MEMBERS[m.group(2)]}."
    m = re.fullmatch(r"/DoomSat_DoomSat/DoomSat/rateGroup_(\w+)/(RgMaxTime|RgCycleSlips)", q)
    if m:
        hz = m.group(1).replace("_", ".")
        return (f"{hz} rate group: longest cycle since the last report, microseconds." if m.group(2) == "RgMaxTime"
                else f"{hz} rate group: cycles that overran. Warning above 0.")
    doc = params.get(q, {}).get("doc")
    return doc[0].upper() + doc[1:] + ("" if doc.endswith(".") else ".") if doc else ""


SECTOR_RADAR_INPUTS = [f"{DOOM}/{k}" for k in
                       [f"{p}_{d}" for p in ("CLEAR", "NEW", "DOOR") for d in DIRS] +
                       ["ENEMY_COUNT", "ENEMY_BEARING", "ENEMY_DIST", "EXIT_BEARING", "EXIT_DIST", "KEY_BEARING",
                        "KEY_DIST", "HEALTH_BEARING", "HEALTH_ITEM_DIST", "AMMO_BEARING", "AMMO_ITEM_DIST",
                        "ARMOR_BEARING", "ARMOR_ITEM_DIST", "HINT_ACTIVE", "HINT_REL", "ANGLE", "STUCK",
                        "AHEAD_KIND", "AHEAD_DIST"]]
CANDIDATE_BOARD_INPUTS = ([f"{DOOM}/{k}" for k in ("CAND_COUNT", "POS_X", "POS_Y", "ANGLE")] +
                          [f"{G}/{k}" for k in ("PickSlot", "IntentMode", "DecisionSource", "PickGap",
                                                "PickConfidence")] +
                          [f"{DOOM}/CAND{n}.{m}" for n in range(8) for m in CAND_MEMBERS] +
                          [f"{G}/Score{n}" for n in range(8)])

CUSTOM_VIEWS = """Both DoomSat views follow the time conductor. In real time they update live, and the plan view's traverse keeps to the conductor's window. In Fixed mode they show the last values inside the bounds and take no live data, so a review holds still while the flight goes on.

### Sector Radar

Forward is up and left is left, as the player sees it. Each of the eight wedges is one direction the payload senses (FWD, AL, LEFT, BL, BACK, BR, RIGHT, AR). A wedge's **length** is the open way in that direction, up to 400 map units (the dotted rings are 100, 200, 300 and 400): the map ray, except forward, where it is the range camera (CLEAR_FWD). Its **fill** is how much of the ground that way has never been walked: pale is walked, bright green is new, and the percentage is printed inside, or **new** where the map has not seen the ground on the way. An **orange bar** across a wedge is a door at that distance. Markers: a **red dot** is the nearest enemy in view (with the count and range), a **green triangle** the exit once recognised, a **yellow triangle** a remembered key, and small dots the nearest health (+HP), ammunition (AMMO) and armor (ARM) pickups. A **dashed magenta line** is Sonnet's exploration hint while it is in force. The top line gives the heading and whether the executor reports STUCK; the bottom line what is at arm's length.

### Candidate Board

Left: the candidates the onboard world model is offering this decision, one row per slot (t0 to t7): kind, jev's score on the nine-level rubric with a bar, path units along the seen floor, the unseen cells behind it (a count, 255 at most), and live things near it. The picked row is highlighted. The header gives the intent mode, the picked slot, the gap to the runner-up, jev's confidence, and **by**: who decided (JEV in green, anything else in orange; see DecisionSource). `n/r` means the value is not in the dictionary or not in the recording. Right: a north-up plan view. The pale line is the player's traverse within the time conductor's bounds (POS_X and POS_Y paired by time), circles are the candidates coloured by kind and sized by score, the dashed white line runs from the player to the pick, and the white tick is the heading."""

COLOR_NAMES = {"#38761d": "green", "#bf9000": "amber", "#b45f06": "orange", "#990000": "red", "#434343": "grey",
               "#0b5394": "blue", "#134f5c": "teal", "#351c75": "purple", "#7f6000": "olive", "#cc0000": "red",
               "#1155cc": "blue", "#f1c232": "yellow"}
OPS = {"lessThanOrEq": "<=", "lessThan": "<", "greaterThan": ">", "greaterThanOrEq": ">=", "enumValueIs": "is",
       "isOneOf": "is one of"}


def _short(q):
    return q.rsplit("/", 1)[-1] if q.startswith("/") else {"yamcs.events": "Yamcs events",
                                                            "yamcs.commands": "Yamcs command history"}.get(
        q, q.replace("yamcs.events.severity.", "Yamcs events, ") if q.startswith("yamcs.") else q)


def _enum_label(params, q, v):
    for val, label in params.get(q, {}).get("enum", []):
        if str(val) == str(v):
            return label
    return v


def render(b, root, params, drift):
    """The whole of docs/OPENMCT.md as a string."""
    objs = b.objects
    by_key = lambda ident: objs.get(ident["key"]) if ident.get("namespace") == "" else None  # noqa: E731
    out = []
    w = out.append
    w("# Open MCT for DoomSat: operator reference")
    w("")
    w("<!-- Generated by tools/build_openmct_displays.py --doc from the same objects as the displays. "
      "Do not edit by hand: change the build script or tools/openmct_docs.py and regenerate. -->")
    w("")
    w("DoomSat's Open MCT displays treat Doom's player as the spacecraft, the F´ deployment and payload as the "
      "onboard side, and Yamcs, the pilot, jev and Sonnet as the ground side. They are generated as code "
      "(`tools/build_openmct_displays.py`), served read-only as **DoomSat Displays**, and run either live "
      "against Yamcs or offline against a recorded flight with no Yamcs at all. This page says what every "
      "screen, panel, indicator and value is. The design reasoning is in the pull request that added them.")
    w("")
    w("![Mission overview, replay of flight-32](images/openmct/overview.jpg)")
    w("")
    w("## Running")
    w("")
    w("**Live**, with the flight stack up (`scripts/flight.sh start`): `ground/.venv/bin/python "
      "tools/set_yamcs_alarms.py` once Yamcs answers (again after every Yamcs restart; nothing runs it for you), "
      "then `scripts/start_openmct.sh`, then http://localhost:9000 and open **DoomSat Displays** in the tree. "
      "`?theme=snow` or `?theme=darkmatter` changes the theme.")
    w("")
    w("**Replay**, no Yamcs: `python3 tools/build_openmct_replay.py` (from `research/out/flight-32`), then "
      "`python3 tools/openmct_serve.py` and open http://localhost:8071/replay.html. Add `?anchor=0` to serve the "
      "recorded timestamps (set the time conductor to Fixed), or leave it off to play the flight as if live. "
      "`python tools/openmct_snapshots.py` renders every screen to `out/openmct-shots/` and logs page errors.")
    w("")
    w("**Changing a display**: edit `tools/build_openmct_displays.py` (and the note for any new panel in "
      "`tools/openmct_docs.py`), then `python3 tools/build_openmct_displays.py --doc`. "
      "`tests/test_openmct.py` fails if the committed displays or this page are out of date, if a display names a "
      "parameter no dictionary defines, or if a panel or parameter has no description.")
    w("")
    w("## The tree")
    w("")
    w("| Display | What it is for |")
    w("|---|---|")
    top = [by_key(c) for c in objs[root]["composition"]]
    screens = [o for o in top if o["name"][:2].isdigit()]
    for o in screens:
        w(f"| {o['name']} | {NOTES[o['name']]} |")
    w("| Conditions | Every condition set and widget, for reuse. |")
    w("| Derived telemetry | Every computed value, for reuse. |")
    w("| Parts | The custom views and a few built panels, to duplicate into My Items when building new screens. |")
    w("")
    w("The tree is read-only (Static Root plugin). To change a screen for yourself, right-click it, "
      "**Duplicate** into My Items, and edit the copy. To change it for everyone, change the build script.")
    w("")
    w("## Screen by screen")
    used_everywhere = set()

    def panel_rows(container):
        rows = []
        for ident in container.get("composition", []):
            o = by_key(ident)
            if o is None:
                q = ident["key"].replace("~", "/")
                if q.startswith("yamcs.events"):
                    rows.append((_short(q), "Yamcs events", "F´ and ground events at that severity and above, as a "
                                 "lane on the same clock.", "-"))
                    continue
                rows.append((_short(q), "telemetry (imagery)" if q in (f"{G}/DoomFrame", f"{G}/DoomMap")
                             else "telemetry", describe(q, params), _short(q)))
                continue
            inputs = []
            for c in o.get("composition", []):
                if c.get("namespace") == "taxonomy":
                    inputs.append(c["key"].replace("~", "/"))
            used_everywhere.update(inputs)
            rows.append((o["name"], o["type"], NOTES.get(o["name"], ""),
                         ", ".join(_short(q) for q in inputs) or "-"))
        return rows

    def table(rows):
        w("| Panel | Type | What it shows | Inputs |")
        w("|---|---|---|---|")
        for name, typ, note, inputs in rows:
            w(f"| {name} | {typ} | {note} | {inputs} |")
        w("")

    shots = {"20 ONBOARD · flight side": "onboard-world", "30 GROUND · the other half of the loop": "ground-autonomy",
             "40 TIME STRIP · one clock for everything": "time-strip"}
    for s in screens:
        w("")
        w(f"### {s['name']}")
        w("")
        w(NOTES[s["name"]])
        w("")
        if s["name"] in shots:
            w(f"![{s['name']}, replay of flight-32](images/openmct/{shots[s['name']]}.jpg)")
            w("")
        if s["type"] == "tabs":
            for ident in s["composition"]:
                tab = by_key(ident)
                w(f"#### Tab: {tab['name']}")
                w("")
                w(NOTES[tab["name"]])
                w("")
                table(panel_rows(tab))
        else:
            table(panel_rows(s))

    # ---- indicators
    w("## Status indicators")
    w("")
    w("Each indicator is a condition set (the rules) shown through a condition widget or conditional styling. "
      "Rules are checked top to bottom and the first match wins. Colours mean the same thing everywhere: green "
      "go, amber watch, orange warning, red critical or no go, grey unknown. A condition set outputs nothing "
      "until its first data arrives (Open MCT issue 5925), so before first data an indicator is blank or shows "
      "its fixed label.")
    w("")
    w("| Condition set | Inputs | Rules, in order | Otherwise |")
    w("|---|---|---|---|")
    for o in objs.values():
        if o["type"] != "conditionSet":
            continue
        coll = o["configuration"]["conditionCollection"]
        rules = []
        for c in coll[:-1]:
            crit = []
            for cr in c["configuration"]["criteria"]:
                q = cr["telemetry"]["key"].replace("~", "/")
                v = cr["input"][0]
                if cr["operation"] == "enumValueIs":
                    v = _enum_label(params, q, v)
                crit.append(f"{_short(q)} {OPS.get(cr['operation'], cr['operation'])} {v}")
            rules.append(f"{' and '.join(crit)} → **{c['configuration']['output']}**")
        inputs = ", ".join(_short(i["key"].replace("~", "/")) for i in o.get("composition", []))
        w(f"| {o['name'][3:]} | {inputs} | {'; '.join(rules)} | {coll[-1]['configuration']['output']} |")
    w("")
    # ---- derived
    w("## Derived telemetry")
    w("")
    w("Computed in the browser by Open MCT's Derived Telemetry plugin (Open MCT 4.3 or later) from the named "
      "inputs; each can be plotted, tabled or used in a condition like any telemetry point.")
    w("")
    w("| Name | Expression | Variables |")
    w("|---|---|---|")
    for o in objs.values():
        if o["type"] == "comps":
            cfg = o["configuration"]["comps"]
            vars_ = ", ".join(f"{p['name']} = {_short(p['keyString'].split(':', 1)[1].replace('~', '/'))}"
                              for p in cfg["parameters"])
            w(f"| {o['name']} | `{cfg['expression']}` | {vars_} |")
        if o["type"] == "telemetry.correlator":
            w(f"| {o['name']} | x, y paired by timestamp (Correlation Telemetry) | "
              f"x = {_short(o['xSource'][0]['identifier']['key'].replace('~', '/'))}, "
              f"y = {_short(o['ySource'][0]['identifier']['key'].replace('~', '/'))} |")
    w("")
    w("## Custom views")
    w("")
    w(CUSTOM_VIEWS)
    w("")
    # ---- parameters
    used = sorted(set(b.used) | used_everywhere | set(SECTOR_RADAR_INPUTS) | set(CANDIDATE_BOARD_INPUTS) |
                  {q for q in GLOSSARY if q.startswith("/DoomOps/")})
    groups = [("Flight: the Doom payload component", lambda q: q.startswith(DOOM)),
              ("Flight: the F´ framework", lambda q: q.startswith("/DoomSat_DoomSat") and not q.startswith(DOOM)),
              ("Ground: the pilot (`/DoomGround`, ground/yamcs/mdb/doom-ground.xtce.xml)", lambda q: q.startswith(G)),
              ("People: Mission Status and Operator Status (`/DoomOps`, ground/yamcs/mdb/doom-ops.xtce.xml)",
               lambda q: q.startswith("/DoomOps"))]
    w("## Parameters")
    w("")
    w("Every parameter a display or custom view reads. Alarm ranges for flight parameters are applied at runtime "
      "by `tools/set_yamcs_alarms.py` from `ground/yamcs/alarm-ranges.json` (see Running); ground ones are in the "
      "XTCE. Types come from the XTCE, and from `Doom.fpp` where the committed `fprime.xtce.xml` lacks a channel "
      "(marked **snapshot lag**). That file is a reference snapshot: Yamcs never loads it, because "
      "`scripts/wsl_run_flight.sh` starts fprime-yamcs (through `ground/yamcs/launch.py`) with the deployment, "
      "and fprime-yamcs regenerates the "
      "XTCE from the deployment's F´ dictionary at every launch. A snapshot-lag channel is live as soon as the "
      "deployment is built from the current `Doom.fpp`.")
    w("")
    for title, test in groups:
        w(f"### {title}")
        w("")
        w("| Parameter | Type | Meaning |")
        w("|---|---|---|")
        for q in used:
            if not test(q):
                continue
            e = params.get(q, {})
            typ = e.get("eng", "?") + (f" ({e['unit']})" if e.get("unit") else "")
            if "snapshot lag" in e.get("source", ""):
                typ += ", **snapshot lag**"
            w(f"| `{q[len('/DoomSat_DoomSat/DoomSat/'):] if q.startswith(DOOM) else q}` | {typ} | {describe(q, params)} |")
        w("")
    w("## Replay mode")
    w("")
    w("`ground/openmct/doomsat/replay.js` serves a recorded flight under exactly the identifiers openmct-yamcs "
      "uses, so the same displays run live or offline. `tools/build_openmct_replay.py` labels every series in a "
      "pack. **Recorded**: the raw telemetry the pilot logged with each decision, the tic, kills, goal, the "
      "INTENT sent, jev's answers and the downlinked frames. **Derived**, computed from recorded values only: "
      "decision source, rolling jev share, the approximate decision age, candidate slots, frame times (spread "
      "linearly over the decision window), and events inferred from transitions, marked `[derived]`. "
      "**Absent**: everything else, which shows no data. F´ health and frame reassembly are empty in replay "
      "because the decision log never carried them. The status bar says REPLAY while a pack is loaded.")
    w("")
    w("## The knowledge boundary")
    w("")
    w("The displays sit downstream of the stack and never upstream of a decision. Nothing Open MCT shows or "
      "stores is read by the pilot, jev or Sonnet; `ground/ops_telemetry.py` only writes `/DoomGround` "
      "parameters and nothing in the loop reads them back. No display sends a command. Open MCT's Mission Status "
      "and Operator Status indicators can write the `/DoomOps` parameters, and nothing in the loop reads those "
      "either. Nothing derived from the WAD appears on a live screen: the grader's overlay is shown only on the "
      "Grader Wall, in after-action.")
    w("")
    w("## Behaviours worth knowing")
    w("")
    w("Condition inputs for enumerations compare the numeric value, not the label; F´ booleans arrive in Yamcs "
      "as an enumeration with False = 0 and True = 255. The Static Root plugin renumbers object keys by position, "
      "so deep links are generated (see `tools/openmct_snapshots.py`), not written by hand. A read-only object "
      "cannot be changed by its own view, so the build pre-fills what plots, plans and graphs would otherwise "
      "write on first load. Open MCT's Bar Graph wants one array-valued source, which is why the eight-direction "
      "values are tables and the radar rather than bar graphs. The map product in a recorded flight is only the "
      "final one, so the automap panel stays empty until the end of a replay.")
    w("")
    if drift:
        w(f"When this page was generated, the committed `fprime.xtce.xml` snapshot lacked {len(drift)} of the Doom "
          f"channels in `Doom.fpp` ({', '.join(drift)}) and the INTENT command. Live Yamcs is unaffected (see "
          "Parameters); refreshing the snapshot is housekeeping, done by running fprime-to-xtce on the "
          "deployment's dictionary and committing the result.")
        w("")
    return "\n".join(out)
