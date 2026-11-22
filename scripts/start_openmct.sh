#!/bin/bash
# Serve Open MCT with the DoomSat config and displays (docs/OPENMCT.md; openmct-yamcs dev server) on
# http://localhost:9000.
# Needs scripts/setup_ground.sh openmct, and Yamcs on :8090 (scripts/flight.sh start, or flight.sh yamcs).
cd "$(dirname "$0")/../external/openmct-yamcs" || { echo "run scripts/setup_ground.sh openmct first"; exit 1; }
cp ../../ground/openmct/index.html ../../ground/openmct/index.js example/
rm -rf example/doomsat example/displays && cp -r ../../ground/openmct/doomsat ../../ground/openmct/displays example/
# A campaign build (build_openmct_displays.py --plan-start, git-ignored) is served in place of the committed
# displays while it is the newer of the two and under a day old, so the plan runs on the flight's clock. An
# older one is a past campaign's, anchored in the past: it is left alone.
campaign=../../ground/openmct/displays/doomsat-displays.campaign.json
if [ "$campaign" -nt ../../ground/openmct/displays/doomsat-displays.json ]; then
  if [ -n "$(find "$campaign" -mmin -1440)" ]; then
    cp "$campaign" example/displays/doomsat-displays.json && echo "serving the campaign displays (--plan-start)"
  else
    echo "not serving the campaign displays: over a day old (delete ground/openmct/displays/doomsat-displays.campaign.json, or rebuild with --plan-start)"
  fi
fi
mkdir -p example/aar && cp ../../docs/results/e1m1-progress.html example/aar/
# The stock dev config proxies to 0.0.0.0 (not dialable on Windows) and shows an error overlay for the
# example display's missing telemetry; both patched in place (node, so it is the same on GNU and BSD).
node -e '
const fs = require("fs"), f = ".webpack/webpack.dev.mjs";
let s = fs.readFileSync(f, "utf8").split("0.0.0.0:8090").join("localhost:8090");
if (!s.includes("overlay: false")) s = s.replace("devServer: {", "devServer: {\n    client: { overlay: false },");
fs.writeFileSync(f, s);'
exec npx webpack serve --config ./.webpack/webpack.dev.mjs --no-open
