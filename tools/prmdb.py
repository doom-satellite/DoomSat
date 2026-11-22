"""flight/config/PrmDb.json with this machine's uplink directory in place of @UPLINK@.

    python tools/prmdb.py flight/config/PrmDb.json "$DOOMSAT_HOME/wads/uplink" > "$RUN/PrmDb.json"

scripts/wsl_run_flight.sh runs this before every start and turns the result into the PrmDb.dat the flight
software loads at boot (fprime-prm-write). The directory goes in as a JSON string, so any character a path
can hold (& # \\ ") arrives as itself, which a sed replacement does not manage. The standard library only:
it runs in the F´ project's venv.
"""
import json
import sys

PLACEHOLDER = "@UPLINK@"


def render(text, uplink_dir):
    """The PrmDb.json text with every @UPLINK@ replaced by uplink_dir; it must parse as JSON afterwards."""
    if not uplink_dir.startswith("/"):
        raise ValueError(f"the uplink directory must be absolute: {uplink_dir!r}")
    out = text.replace(PLACEHOLDER, json.dumps(uplink_dir)[1:-1])
    json.loads(out)
    return out


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    with open(argv[1], encoding="utf-8") as f:
        sys.stdout.write(render(f.read(), argv[2]))


if __name__ == "__main__":
    main(sys.argv)
