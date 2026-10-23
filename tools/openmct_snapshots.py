# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.45"]
# ///
"""Render every DoomSat display from a replay pack to JPG, and fail loudly on page errors.

    python tools/build_openmct_replay.py      # once
    python tools/openmct_serve.py &           # serves ground/openmct on :8071
    python tools/openmct_snapshots.py         # -> out/openmct-shots/*.jpg and console.log

Needs Playwright for Python and a Chromium (`pip install playwright && playwright install chromium`), or
`--channel chrome` to drive an installed Chrome, as tools/stack_record.mjs does. `--headed` if your
Chrome does not paint Open MCT headless (it did in testing, but tools/openmct_shot.mjs says otherwise).

Doubles as a smoke test: a display that throws, or references an object nobody provides, shows up in the
console log this writes next to the pictures.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parents[1]
DISPLAYS = HERE / "ground" / "openmct" / "displays" / "doomsat-displays.replay.json"


def paths():
    data = json.loads(DISPLAYS.read_text())
    keys = list(data["openmct"])
    idx = {k: ("root" if k == data["rootId"] else str(i)) for i, k in enumerate(keys)}
    out = {}
    for k, o in data["openmct"].items():
        chain, cur = [], k
        while cur and cur != "ROOT":
            chain.append(f"doomsat:{idx[cur]}")
            cur = data["openmct"][cur]["location"]
        out[o["name"]] = "/".join(reversed(chain))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8071/replay.html")
    ap.add_argument("--pack", default=str(HERE / "ground" / "openmct" / "replay" / "flight-32" / "pack.json"))
    ap.add_argument("--at", type=float, default=172.9, help="seconds into the recording for the end bound")
    ap.add_argument("--window", type=float, default=180.0, help="seconds of history in view")
    ap.add_argument("--out", default=str(HERE / "out" / "openmct-shots"))
    ap.add_argument("--channel", default=None, help="e.g. chrome, to use an installed browser")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    a = ap.parse_args()
    meta = json.loads(Path(a.pack).read_text())["meta"]
    p = paths()
    views = [("01-overview", "00 Mission overview", None),
             ("02-game", "10 GAME · the payload", {"at": 189.4}),
             ("03-onboard-world", "20 ONBOARD · flight side", None),
             ("04-onboard-fsw", "Flight software (F´)", None),
             ("05-ground-autonomy", "Autonomy: jev and Sonnet", None),
             ("06-ground-gds", "Ground data system", None),
             ("07-ground-history", "Command and event history", None),
             ("08-timestrip", "40 TIME STRIP · one clock for everything", {"at": 189.4, "window": 192}),
             ("09-timelist", "E1 campaign timelist", {"at": 189.4}),
             ("10-grader-wall", "Grader wall (post-flight only)", None),
             ("11-pocket", "60 POCKET · phone view", {"size": (430, 900)})]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=a.channel, headless=not a.headed)
        for fname, name, opt in views:
            if a.only and fname not in a.only:
                continue
            opt = opt or {}
            w, h = opt.get("size", (a.width, a.height))
            end = int(meta["t0"] + opt.get("at", a.at) * 1000)
            start = int(end - opt.get("window", a.window) * 1000)
            page = browser.new_page(viewport={"width": w, "height": h})
            page.on("console", lambda m, f=fname: log.append(f"{f} {m.type}: {m.text}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e, f=fname: log.append(f"{f} PAGEERROR: {e}"))
            url = (f"{a.base}?anchor=0#/browse/{p[name]}?tc.mode=fixed&tc.startBound={start}"
                   f"&tc.endBound={end}&tc.timeSystem=utc&hideTree=true&hideInspector=true")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(9000)
            page.screenshot(path=str(out / f"{fname}.jpg"), type="jpeg", quality=88)
            page.close()
            print(f"{fname}: {name}")
        browser.close()
    (out / "console.log").write_text("\n".join(log))
    errs = [l for l in log if "error" in l.lower()]
    print(f"{len(errs)} console errors, {len(log) - len(errs)} warnings -> {out / 'console.log'}")


if __name__ == "__main__":
    main()
