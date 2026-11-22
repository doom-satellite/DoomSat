"""The replay server serves the replay page, Open MCT and the after-action page, and nothing else.

It runs from the repo, which holds .env (the TypeSafe key), .git and ground/.venv. A request may only get a
regular file under the folder its prefix maps to: /node_modules/openmct/dist/ to Open MCT's build, /aar/ to
docs/results, everything else to ground/openmct. No `..` or encoded `..`, no dotfiles, no symlink out, no
directory listing, for GET and for HEAD. The page, its scripts, its pack and Open MCT itself must still come
back, and the server must listen on 127.0.0.1 unless told otherwise.
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import openmct_serve                            # noqa: E402

DIST = "external/openmct-yamcs/node_modules/openmct/dist"
FILES = [".env", ".git/config", "ground/.venv/pyvenv.cfg", "tools/x.py", "external/openmct-yamcs/package.json",
         "ground/openmct/replay.html", "ground/openmct/replay.js", "ground/openmct/doomsat/plugin.js",
         "ground/openmct/replay/flight-32/pack.json", "ground/openmct/replay/flight-32/frames/frame-000001.jpg",
         "ground/openmct/.hidden", DIST + "/openmct.js", DIST + "/images/logo.svg", DIST + "/.hidden",
         "docs/results/e1m1-progress.html", "docs/results/.hidden"]

ALLOWED = {"/": "ground/openmct/replay.html",
           "/?pack=replay/flight-32/pack.json&anchor=0": "ground/openmct/replay.html",
           "/replay.html": "ground/openmct/replay.html",
           "/replay.html?anchor=0": "ground/openmct/replay.html",
           "/%72eplay.html": "ground/openmct/replay.html",
           "/replay.js": "ground/openmct/replay.js",
           "/doomsat/plugin.js": "ground/openmct/doomsat/plugin.js",
           "/replay/flight-32/pack.json": "ground/openmct/replay/flight-32/pack.json",
           "/replay/flight-32/frames/frame-000001.jpg": "ground/openmct/replay/flight-32/frames/frame-000001.jpg",
           "/node_modules/openmct/dist/openmct.js": DIST + "/openmct.js",
           "/node_modules/openmct/dist/images/logo.svg": DIST + "/images/logo.svg",
           "/aar/e1m1-progress.html": "docs/results/e1m1-progress.html",
           "/aar/e1m1%2Dprogress.html": "docs/results/e1m1-progress.html"}

# `..` after /aar/ or after the dist prefix once reached the repo root and its .env. Those, their encoded forms,
# and the stock path's own edge cases.
TRAVERSAL = ["/aar/../../.env", "/aar/%2e%2e/%2e%2e/.env", "/aar/%2E%2E/%2E%2E/.env", "/aar/..%2f..%2f.env",
             "/aar/..%5c..%5c.env", "/aar/../../.git/config", "/aar/../../tools/x.py", "/aar/../../",
             "/node_modules/openmct/dist/../../../../../.env",
             "/node_modules/openmct/dist/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/.env",
             "/node_modules/openmct/dist/..%2f..%2f..%2f..%2f..%2f.env",
             "/node_modules/openmct/dist/../../../package.json", "/node_modules/openmct/dist/../../../../../",
             "/../.env", "/%2e%2e/.env", "/../../.env", "/../ground/.venv/pyvenv.cfg"]

DENIED = TRAVERSAL + [
    "/.env", "/%2eenv", "/.hidden", "/aar/.hidden", "/node_modules/openmct/dist/.hidden", "/aar/", "/aar",
    "/replay/", "/replay", "/doomsat/", "/node_modules/openmct/dist/", "/replay.html/", "//etc/passwd",
    "/C:/Windows/win.ini", "/aar/C:/Windows/win.ini", "/aar/e1m1-progress.html%00.png", "/aar//e1m1-progress.html",
    "/node_modules/openmct/dist//openmct.js", "http://x/.env", "/" + "a" * 300, "/aar/" + "b" * 300]


def make_root(tmp):
    root = Path(tmp).resolve()
    for rel in FILES:
        public = rel.startswith(("ground/openmct/", DIST + "/", "docs/results/")) and not Path(rel).name.startswith(".")
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(("PUBLIC " if public else "SECRET ") + rel)
    return root


class Tree:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(tmp.name)
        self.web, self.aar, self.dist = self.root / "ground/openmct", self.root / "docs/results", self.root / DIST

    def resolve(self, url):
        return openmct_serve.resolve_static(url, self.web, self.aar, self.dist)


class ResolveStatic(Tree, unittest.TestCase):
    def test_the_page_its_pack_open_mct_and_the_after_action_page(self):
        for url, rel in ALLOWED.items():
            with self.subTest(url=url):
                self.assertEqual(self.resolve(url), self.root / rel)

    def test_everything_else_is_refused(self):
        for url in DENIED:
            with self.subTest(url=url):
                self.assertIsNone(self.resolve(url))

    def test_nothing_resolves_outside_its_base(self):
        # whatever the rules above say, nothing any of these asks for may land outside the three folders
        bases = (self.web, self.aar, self.dist)
        for url in DENIED + list(ALLOWED):
            with self.subTest(url=url):
                got = self.resolve(url)
                self.assertTrue(got is None or any(got.is_relative_to(b) for b in bases), got)

    def test_symlinks_out_of_a_base_are_refused(self):
        try:
            (self.aar / "evil.html").symlink_to(Path("..") / ".." / ".env")
            (self.dist / "evil.js").symlink_to(Path("..") / ".." / ".." / ".." / ".." / ".env")
            (self.web / "evil.js").symlink_to(Path("..") / ".." / ".env")
            (self.web / "up").symlink_to(Path("..") / "..", target_is_directory=True)
            (self.web / "alias.html").symlink_to("replay.html")
        except (OSError, NotImplementedError) as e:   # Windows without the symlink privilege
            self.skipTest(f"no symlinks here: {e}")
        for url in ["/aar/evil.html", "/node_modules/openmct/dist/evil.js", "/evil.js", "/up/tools/x.py",
                    "/up/docs/results/e1m1-progress.html"]:
            with self.subTest(url=url):
                self.assertIsNone(self.resolve(url))
        # a link that stays inside its folder is still a file under it
        self.assertEqual(self.resolve("/alias.html"), self.root / "ground/openmct/replay.html")

    def test_backslash_and_colon_are_refused_even_when_the_file_exists(self):
        # They only bite on Windows (the hybrid setup runs the ground side there), so prove the refusal itself
        # with real files, which a Linux filesystem allows.
        try:
            (self.aar / "a:b.html").write_text("SECRET colon")
            (self.web / "a\\b.html").write_text("SECRET backslash")
        except OSError as e:
            self.skipTest(f"this filesystem will not hold those names: {e}")
        for url in ["/aar/a:b.html", "/aar/a%3Ab.html", "/a%5cb.html"]:
            with self.subTest(url=url):
                self.assertIsNone(self.resolve(url))


class Handler(Tree, unittest.TestCase):
    """The real request handling, driven from a byte string with no socket."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(openmct_serve, "AAR", self.aar),
                        mock.patch.object(openmct_serve.Handler, "dist", self.dist)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path):
        test = self

        class Fake(openmct_serve.Handler):
            def __init__(self, raw):
                self.rfile = io.BytesIO(raw)
                self.wfile = io.BytesIO()
                self.client_address = ("127.0.0.1", 0)
                self.server = None
                self.directory = str(test.web)
                self.request_version = "HTTP/1.1"

        h = Fake(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:8071\r\nConnection: close\r\n\r\n".encode())
        h.handle_one_request()
        raw = h.wfile.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:])}
        self.assertNotIn(b"Directory listing", raw, f"{method} {path}")
        self.assertNotIn(b"SECRET", raw, f"{method} {path}")
        return int(lines[0].split()[1]), headers, body

    def test_traversal_is_404(self):
        for path in TRAVERSAL + ["/.env", "/aar/", "/replay/", "/node_modules/openmct/dist/"]:
            for method in ("GET", "HEAD"):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.request(method, path)[0], 404)

    def test_the_page_open_mct_and_the_after_action_page_are_served(self):
        for path, rel, ctype in [("/", "ground/openmct/replay.html", "text/html"),
                                 ("/replay.html?anchor=0", "ground/openmct/replay.html", "text/html"),
                                 ("/replay.js", "ground/openmct/replay.js", "javascript"),
                                 ("/replay/flight-32/frames/frame-000001.jpg",
                                  "ground/openmct/replay/flight-32/frames/frame-000001.jpg", "image/jpeg"),
                                 ("/node_modules/openmct/dist/openmct.js", DIST + "/openmct.js", "javascript"),
                                 ("/aar/e1m1-progress.html", "docs/results/e1m1-progress.html", "text/html")]:
            for method in ("GET", "HEAD"):
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(method, path)
                    self.assertEqual(status, 200)
                    self.assertIn(ctype, headers["content-type"])
                    self.assertEqual(body, b"" if method == "HEAD" else ("PUBLIC " + rel).encode())


class Listen(unittest.TestCase):
    def run_main(self, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with mock.patch.object(openmct_serve, "ROOT", root), \
                    mock.patch.object(openmct_serve, "WEB", root / "ground/openmct"), \
                    mock.patch.object(openmct_serve, "DIST", [root / DIST]), \
                    mock.patch.object(openmct_serve.Handler, "dist", None), \
                    mock.patch.object(openmct_serve.http.server, "ThreadingHTTPServer") as server, \
                    mock.patch.object(sys, "argv", ["openmct_serve.py", *argv]), \
                    contextlib.redirect_stdout(io.StringIO()):
                openmct_serve.main()
        return server.call_args.args[0]

    def test_loopback_by_default(self):
        self.assertEqual(self.run_main(), ("127.0.0.1", 8071))

    def test_host_flag(self):
        self.assertEqual(self.run_main("--host", "0.0.0.0", "--port", "8072"), ("0.0.0.0", 8072))


if __name__ == "__main__":
    unittest.main()
