"""Serve the mission dashboard (ground/dashboard) on http://localhost:8070 and proxy /api/* to Yamcs,
so the page can read telemetry, events, commands and image products from Yamcs without CORS trouble.
Nothing else is served: only regular files under ground/dashboard/ (the page, also at /), with no directory
listings and no dotfiles, so .env, .git, the venvs and out/ stay out of reach.

    python tools/serve_dashboard.py [--port 8070] [--yamcs http://localhost:8090]
"""
import argparse
import http.server
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = "ground/dashboard/index.html"
ALIASES = {"/": PAGE, "/index.html": PAGE}
STATIC_ROOTS = ("ground/dashboard",)


def resolve_static(url, root):
    """The file a GET or HEAD of `url` serves from `root`, or None for a 404. The query string is ignored
    (`/?drive` is the page). A path with a backslash, NUL, colon, empty segment or dot segment is refused before
    it touches the disk, and the resolved file must sit under one of STATIC_ROOTS, so `..` and symlinks that
    point out of them get nothing."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    rel = ALIASES.get(path, path.lstrip("/"))
    if not rel or any(c in rel for c in "\\\0:"):
        return None
    if any(not seg or seg.startswith(".") for seg in rel.split("/")):
        return None
    try:  # a name too long for the filesystem, or one it may not stat, is a 404 too, not a dropped request
        target = (root / rel).resolve()
        if not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    if not any(target.is_relative_to((root / r).resolve()) for r in STATIC_ROOTS):
        return None
    return target


class Handler(http.server.SimpleHTTPRequestHandler):
    yamcs = "http://localhost:8090"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass

    def send_head(self):
        # GET and HEAD both come through here. Anything resolve_static refuses is a 404, and it only returns
        # regular files, so there is no directory to list (short of one replacing a file mid-request).
        self._target = resolve_static(self.path, ROOT)
        if self._target is None:
            self.send_error(404)
            return None
        return super().send_head()

    def translate_path(self, path):
        return str(self._target)

    def _proxy(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(self.yamcs + self.path, data=body, method=method,
                                     headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            # Yamcs answered, with a refusal: pass its status and its reason through (a refused command
            # says why, and the page shows it) instead of hiding both behind a 502.
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            self.send_error(502, str(e))

    def do_GET(self):
        if self.path.split("?", 1)[0].startswith("/api/"):
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0].startswith("/api/"):
            return self._proxy("POST")
        self.send_error(404)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8070)
    p.add_argument("--yamcs", default="http://localhost:8090")
    a = p.parse_args()
    Handler.yamcs = a.yamcs
    print(f"dashboard on http://localhost:{a.port}/  (Yamcs at {a.yamcs})", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
