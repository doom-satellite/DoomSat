# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Serve ground/openmct for the replay page, with no Yamcs and no build step.

    python tools/openmct_serve.py [--port 8071] [--host 127.0.0.1]   then open http://localhost:8071/replay.html

Open MCT itself comes from the first of: ground/openmct/node_modules/openmct/dist (npm install openmct@^4.3
in ground/openmct) or external/openmct-yamcs/node_modules/openmct/dist (the build start_openmct.sh uses).
/aar/ serves docs/results, for the after-action web page. Threaded, because the imagery strip asks for dozens
of frames at once.

Nothing else is served: only regular files under those three folders (replay.html is also at /), with no
directory listings and no dotfiles, so .env, .git and the venvs stay out of reach. It listens on 127.0.0.1
only. `--host 0.0.0.0` lets another machine (a phone, for the pocket view) watch the replay, and it gets the
same three folders and nothing more.
"""
import argparse
import functools
import http.server
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "ground" / "openmct"
DIST = [WEB / "node_modules" / "openmct" / "dist", ROOT / "external" / "openmct-yamcs" / "node_modules" / "openmct" / "dist"]
AAR = ROOT / "docs" / "results"
ALIASES = {"/": "/replay.html"}


def resolve_static(url, web, aar, dist=None):
    """The file a GET or HEAD of `url` serves, or None for a 404. /node_modules/openmct/dist/ maps to `dist`,
    /aar/ to `aar` and everything else to `web`. The query string is ignored (`?pack=` and `?anchor=` are the
    page's). A path with a backslash, NUL, colon, empty segment or dot segment is refused before it touches the
    disk, and the resolved file must be a regular file under the folder its prefix maps to, so `..`, encoded
    `..` and symlinks that point out of it get nothing."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    path = ALIASES.get(path, path)
    for prefix, base in (("/node_modules/openmct/dist/", dist), ("/aar/", aar), ("/", web)):
        if base is not None and path.startswith(prefix):
            rel = path[len(prefix):]
            break
    else:
        return None
    if not rel or any(c in rel for c in "\\\0:"):
        return None
    if any(not seg or seg.startswith(".") for seg in rel.split("/")):
        return None
    try:  # a name too long for the filesystem, or one it may not stat, is a 404 too, not a dropped request
        base = Path(base).resolve()
        target = (base / rel).resolve()
        if not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    return target if target.is_relative_to(base) else None


class Handler(http.server.SimpleHTTPRequestHandler):
    dist = None

    def send_head(self):
        # GET and HEAD both come through here. Anything resolve_static refuses is a 404, and it only returns
        # regular files, so there is no directory to list (short of one replacing a file mid-request).
        self._target = resolve_static(self.path, Path(self.directory), AAR, self.dist)
        if self._target is None:
            self.send_error(404)
            return None
        return super().send_head()

    def translate_path(self, path):
        return str(self._target)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8071)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1, this machine only; 0.0.0.0 for the network)")
    a = ap.parse_args()
    Handler.dist = next((d for d in DIST if (d / "openmct.js").exists()), None)
    if Handler.dist is None:
        raise SystemExit("no Open MCT build found: cd ground/openmct && npm install openmct@^4.3")
    if not (WEB / "replay").is_dir():
        print("no replay pack yet: python tools/build_openmct_replay.py")
    shown = "localhost" if a.host in ("127.0.0.1", "0.0.0.0", "") else a.host
    print(f"serving {WEB.relative_to(ROOT)} with Open MCT from {Handler.dist.relative_to(ROOT)} "
          f"on http://{shown}:{a.port}/replay.html")
    http.server.ThreadingHTTPServer((a.host, a.port), functools.partial(Handler, directory=str(WEB))).serve_forever()


if __name__ == "__main__":
    main()
