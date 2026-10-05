"""The dashboard server serves the page and nothing else.

It runs from the repo root, which holds .env (the TypeSafe key), .git, ground/.venv, external/ and out/.
Only regular files under ground/dashboard/ may come back: no dotfiles, no `..` or encoded `..`, no symlink
out, no directory listing, for GET and for HEAD. The page's own `?drive` switch must still land on the page,
/api/* must still reach Yamcs untouched, and everything the page itself asks for must be either /api/ or a
file the server will hand out.
"""
import io
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import serve_dashboard                          # noqa: E402

FILES = [".env", "ground/.env", ".git/config", "ground/dashboard/index.html", "ground/dashboard/.hidden",
         "out/payload_map.png", "out/decisions.jsonl", "tools/x.py", "README.md"]

ALLOWED = ["/", "/index.html", "/?drive", "/index.html?drive", "/ground/dashboard/index.html",
           "/ground/dashboard/index.html?drive", "/ground/dashboard/index%2Ehtml", "/%69ndex.html"]

DENIED = ["/.env", "/.env?x", "/%2eenv", "/%2Eenv", "/ground/.env", "/ground/dashboard/../../.env",
          "/ground/dashboard/%2e%2e/%2e%2e/.env", "/ground/dashboard/..%2f..%2f.env",
          "/ground/dashboard/..%5c..%5c.env", "/.git/config", "/tools/x.py", "/README.md",
          "/out/payload_map.png", "/out/decisions.jsonl", "/out/", "/ground/", "/ground/dashboard/",
          "/ground/dashboard", "/ground/dashboard/.hidden", "/ground/dashboard/evil", "//etc/passwd",
          "/C:/Windows/win.ini", "/ground/dashboard/index.html%00.png", "/ground//dashboard/index.html",
          "http://x/.env", "/ground/dashboard/index.html/", "/" + "a" * 300, "/ground/dashboard/" + "b" * 300]


def make_root(tmp):
    root = Path(tmp).resolve()
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("<html>page</html>" if rel.endswith("index.html") else "SECRET " + rel)
    return root


class ResolveStatic(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(tmp.name)
        self.page = (self.root / serve_dashboard.PAGE).resolve()

    def test_the_page_and_its_drive_switch(self):
        for url in ALLOWED:
            with self.subTest(url=url):
                self.assertEqual(serve_dashboard.resolve_static(url, self.root), self.page)

    def test_everything_else_is_refused(self):
        for url in DENIED:
            with self.subTest(url=url):
                self.assertIsNone(serve_dashboard.resolve_static(url, self.root))

    def test_symlinks_out_of_the_dashboard_are_refused(self):
        dash = self.root / "ground" / "dashboard"
        try:
            (dash / "evil").symlink_to(Path("..") / ".." / ".env")
            (dash / "up").symlink_to(Path("..") / "..", target_is_directory=True)
            (dash / "alias.html").symlink_to("index.html")
        except (OSError, NotImplementedError) as e:   # Windows without the symlink privilege
            self.skipTest(f"no symlinks here: {e}")
        for url in ["/ground/dashboard/evil", "/ground/dashboard/up/README.md", "/ground/dashboard/up/tools/x.py",
                    "/ground/dashboard/up/out/payload_map.png"]:
            with self.subTest(url=url):
                self.assertIsNone(serve_dashboard.resolve_static(url, self.root))
        # a link that stays inside the dashboard is still a file under it
        self.assertEqual(serve_dashboard.resolve_static("/ground/dashboard/alias.html", self.root), self.page)

    def test_backslash_and_colon_are_refused_even_when_the_file_exists(self):
        # They only bite on Windows (the hybrid setup runs the dashboard there), so prove the refusal itself
        # with real files under the dashboard, which a Linux filesystem allows.
        dash = self.root / "ground" / "dashboard"
        try:
            (dash / "a:b.html").write_text("SECRET colon")
            (dash / "a\\b.html").write_text("SECRET backslash")
        except OSError as e:
            self.skipTest(f"this filesystem will not hold those names: {e}")
        for url in ["/ground/dashboard/a:b.html", "/ground/dashboard/a%3Ab.html", "/ground/dashboard/a%5cb.html"]:
            with self.subTest(url=url):
                self.assertIsNone(serve_dashboard.resolve_static(url, self.root))


class Handler(unittest.TestCase):
    """The real request handling, driven from a byte string with no socket and no Yamcs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(tmp.name)
        patcher = mock.patch.object(serve_dashboard, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proxied = []

    def request(self, method, path):
        test = self

        class Fake(serve_dashboard.Handler):
            def __init__(self, raw):
                self.rfile = io.BytesIO(raw)
                self.wfile = io.BytesIO()
                self.client_address = ("127.0.0.1", 0)
                self.server = None
                self.directory = str(test.root)
                self.request_version = "HTTP/1.1"

            def _proxy(self, method):
                test.proxied.append((method, self.path))
                self.send_response(204)
                self.end_headers()

        h = Fake(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:8070\r\nConnection: close\r\n\r\n".encode())
        h.handle_one_request()
        raw = h.wfile.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:])}
        self.assertNotIn(b"Directory listing", raw, f"{method} {path}")
        return int(lines[0].split()[1]), headers, body

    def test_secrets_and_directories_are_404(self):
        for method, path in [("GET", "/.env"), ("HEAD", "/.env"), ("GET", "/ground/.env"),
                             ("GET", "/ground/dashboard/"), ("GET", "/out/"), ("HEAD", "/out/"),
                             ("GET", "/.git/config"), ("GET", "/ground/dashboard/%2e%2e/%2e%2e/.env"),
                             ("GET", "/" + "a" * 300)]:
            with self.subTest(method=method, path=path):
                status, _, body = self.request(method, path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"SECRET", body)

    def test_the_page_is_served(self):
        for method, path in [("GET", "/"), ("GET", "/?drive"), ("HEAD", "/"), ("GET", "/index.html"),
                             ("GET", "/ground/dashboard/index.html?drive")]:
            with self.subTest(method=method, path=path):
                status, headers, body = self.request(method, path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith("text/html"), headers)
                self.assertEqual(body, b"" if method == "HEAD" else b"<html>page</html>")

    def test_api_goes_to_yamcs_unchanged(self):
        status, _, _ = self.request("GET", "/api/x?limit=1")
        self.assertEqual(status, 204)
        self.assertEqual(self.proxied, [("GET", "/api/x?limit=1")])

    def test_post_outside_api_is_404(self):
        status, _, _ = self.request("POST", "/x")
        self.assertEqual(status, 404)
        self.assertEqual(self.proxied, [])


class PageConsistency(unittest.TestCase):
    """Whatever ground/dashboard/index.html asks this server for must be /api/ or a file it will serve.
    Only literal fetch("...") targets and src=/href= values count: SS, G and CMD are path pieces, not URLs."""

    def test_every_literal_url_on_the_page_is_served(self):
        html = (ROOT / serve_dashboard.PAGE).read_text(encoding="utf-8")
        fetched = [m[1] for m in re.findall(r"""\bfetch\(\s*(["'])(.*?)\1""", html)]
        linked = [m[1] for m in re.findall(r"""\b(?:src|href)\s*=\s*(["'])(.*?)\1""", html)]
        self.assertTrue(fetched, "found no fetch(\"...\") on the page: has the pattern gone stale?")
        for url in fetched + linked:
            with self.subTest(url=url):
                if url.startswith("/api/"):
                    continue
                # the page lives at /, so that is where a relative URL resolves from
                self.assertIsNotNone(serve_dashboard.resolve_static(urllib.parse.urljoin("/", url), ROOT),
                                     f"{url} is on the page but the server would 404 it")

    def test_the_real_page_resolves(self):
        self.assertEqual(serve_dashboard.resolve_static("/?drive", ROOT), (ROOT / serve_dashboard.PAGE).resolve())
        self.assertIsNone(serve_dashboard.resolve_static("/.env", ROOT))
        self.assertIsNone(serve_dashboard.resolve_static("/tools/serve_dashboard.py", ROOT))


if __name__ == "__main__":
    unittest.main()
