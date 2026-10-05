#!/bin/bash
# Serve Open MCT with the DoomSat config (openmct-yamcs dev server) on http://localhost:9000.
# Needs scripts/setup_ground.sh openmct, and Yamcs on :8090 (scripts/flight.sh start, or flight.sh yamcs).
cd "$(dirname "$0")/../external/openmct-yamcs" || { echo "run scripts/setup_ground.sh openmct first"; exit 1; }
cp ../../ground/openmct/index.html ../../ground/openmct/index.js example/
# The stock dev config proxies to 0.0.0.0 (not dialable on Windows) and shows an error overlay for the
# example display's missing telemetry; both patched in place (node, so it is the same on GNU and BSD).
node -e '
const fs = require("fs"), f = ".webpack/webpack.dev.mjs";
let s = fs.readFileSync(f, "utf8").split("0.0.0.0:8090").join("localhost:8090");
if (!s.includes("overlay: false")) s = s.replace("devServer: {", "devServer: {\n    client: { overlay: false },");
fs.writeFileSync(f, s);'
exec npx webpack serve --config ./.webpack/webpack.dev.mjs --no-open
