"""The product catalog: one SQLite file saying what exists, where, from what, and which version is current.

    episodes   one row per closed episode, with the context it was flown in (WAD, map, skill, pilot, commit)
    products   one row per product file: id, level, type, algorithm version, path, sha256, inputs, run, current
    findings   anything a pipeline noticed that a person should look at (a checksum that moved, a record that
               disagrees with the archive)

A product id is "<episode id>/<type>@<version>" (L3 and quicklooks use "l3/..." and "ql/..." in place of the
episode). Versions never replace each other; `is_current` marks the newest per episode and type.

    python -m doomsat_sds.catalog summary | episodes | products [--episode ID] [--type T] [--current] |
                                  show PRODUCT_ID | findings | sql "SELECT ..."
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id          TEXT PRIMARY KEY,
    number              INTEGER NOT NULL,
    outcome             TEXT NOT NULL,
    closing_event       TEXT NOT NULL,
    inferred_close      INTEGER NOT NULL DEFAULT 0,
    closing_ms          INTEGER NOT NULL,
    start_event_ms      INTEGER,
    start_utc           TEXT, end_utc TEXT, start_ms INTEGER, end_ms INTEGER, duration_s REAL,
    wad TEXT, map TEXT, skill INTEGER, seed INTEGER, pilot_mode TEXT,
    repo_commit         TEXT,
    level_set           TEXT,
    context_json        TEXT NOT NULL,
    first_cataloged_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id          TEXT PRIMARY KEY,
    episode_id          TEXT REFERENCES episodes(episode_id),
    level               TEXT NOT NULL,
    product_type        TEXT NOT NULL,
    algorithm_version   TEXT NOT NULL,
    path                TEXT NOT NULL,
    media_type          TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    inputs_json         TEXT NOT NULL,
    airflow_run_id      TEXT,
    code_commit         TEXT,
    created_utc         TEXT NOT NULL,
    is_current          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS products_by_episode ON products (episode_id, product_type);
CREATE INDEX IF NOT EXISTS products_by_type ON products (product_type, is_current);
CREATE TABLE IF NOT EXISTS findings (
    finding_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    episode_id   TEXT,
    product_id   TEXT,
    detail_json  TEXT NOT NULL,
    created_utc  TEXT NOT NULL
);
"""

EPISODE_COLUMNS = ("episode_id", "number", "outcome", "closing_event", "inferred_close", "closing_ms",
                   "start_event_ms", "start_utc", "end_utc",
                   "start_ms", "end_ms", "duration_s", "wad", "map", "skill", "seed", "pilot_mode", "repo_commit",
                   "level_set", "context_json", "first_cataloged_utc")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


class Catalog:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=60)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ episodes
    def add_episode(self, row: dict) -> dict:
        """Catalog an episode once. A second call keeps the first context: the context belongs to the flight,
        and reprocessing must not rewrite it from whatever happens to be running later."""
        row = dict(row)
        row.setdefault("first_cataloged_utc", now_utc())
        row["context_json"] = json.dumps(row.pop("context", {}), sort_keys=True)
        cols = [c for c in EPISODE_COLUMNS if c in row]
        self.db.execute(f"INSERT OR IGNORE INTO episodes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [row[c] for c in cols])
        return self.episode(row["episode_id"])

    def episode(self, episode_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        return self._episode(r) if r else None

    def episodes(self) -> list[dict]:
        return [self._episode(r) for r in self.db.execute("SELECT * FROM episodes ORDER BY episode_id")]

    @staticmethod
    def _episode(r) -> dict:
        d = dict(r)
        d["context"] = json.loads(d.pop("context_json"))
        return d

    # ------------------------------------------------------------------ products
    def register(self, *, product_id: str, episode_id: str | None, level: str, product_type: str, version: str,
                 path: str | Path, media_type: str, sha256: str, size_bytes: int, inputs: list,
                 run_id: str | None = None, code_commit: str | None = None) -> tuple[dict, str]:
        """Record a product file. Returns (row, status) where status is "new", "unchanged" or "changed".

        Same id and same checksum is a no-op (a retried task). Same id, different checksum means the same
        algorithm version produced different bytes from what should be the same input: the row is updated to
        match the file on disk and a finding says so.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
            if old is not None and old["sha256"] == sha256:
                status = "unchanged"
            else:
                status = "new" if old is None else "changed"
                if old is not None:
                    self._finding("checksum_changed", episode_id, product_id,
                                  {"old_sha256": old["sha256"], "new_sha256": sha256, "old_run": old["airflow_run_id"],
                                   "new_run": run_id})
                self.db.execute(
                    "INSERT OR REPLACE INTO products (product_id, episode_id, level, product_type, algorithm_version,"
                    " path, media_type, sha256, size_bytes, inputs_json, airflow_run_id, code_commit, created_utc,"
                    " is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (product_id, episode_id, level, product_type, version, str(path), media_type, sha256,
                     size_bytes, json.dumps(inputs, sort_keys=True), run_id, code_commit, now_utc()))
                self._mark_current(episode_id, product_type)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.product(product_id), status

    def _mark_current(self, episode_id: str | None, product_type: str) -> None:
        if episode_id is None:
            rows = self.db.execute("SELECT product_id, algorithm_version, created_utc FROM products"
                                   " WHERE episode_id IS NULL AND product_type = ?", (product_type,)).fetchall()
        else:
            rows = self.db.execute("SELECT product_id, algorithm_version, created_utc FROM products"
                                   " WHERE episode_id = ? AND product_type = ?", (episode_id, product_type)).fetchall()
        if not rows:
            return
        best = max(rows, key=lambda r: (_vkey(r["algorithm_version"]), r["created_utc"]))["product_id"]
        for r in rows:
            self.db.execute("UPDATE products SET is_current = ? WHERE product_id = ?",
                            (1 if r["product_id"] == best else 0, r["product_id"]))

    def product(self, product_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        return self._product(r) if r else None

    def products(self, episode_id: str | None = None, product_type: str | None = None,
                 current_only: bool = False) -> list[dict]:
        q, args = "SELECT * FROM products WHERE 1=1", []
        if episode_id is not None:
            q += " AND episode_id = ?"
            args.append(episode_id)
        if product_type is not None:
            q += " AND product_type = ?"
            args.append(product_type)
        if current_only:
            q += " AND is_current = 1"
        return [self._product(r) for r in self.db.execute(q + " ORDER BY product_id", args)]

    def current(self, episode_id: str | None, product_type: str) -> dict | None:
        rows = self.products(episode_id, product_type, current_only=True)
        if episode_id is None:
            rows = [r for r in self.products(None, product_type, current_only=True) if r["episode_id"] is None]
        return rows[0] if rows else None

    @staticmethod
    def _product(r) -> dict:
        d = dict(r)
        d["inputs"] = json.loads(d.pop("inputs_json"))
        d["is_current"] = bool(d["is_current"])
        return d

    def forget(self, product_ids: list[str]) -> int:
        """Drop rows (quicklooks past their retention). Episode products are never forgotten."""
        n = 0
        for pid in product_ids:
            n += self.db.execute("DELETE FROM products WHERE product_id = ? AND episode_id IS NULL"
                                 " AND substr(product_type, 1, 3) = 'ql_'", (pid,)).rowcount
        return n

    # ------------------------------------------------------------------ findings
    def _finding(self, kind: str, episode_id: str | None, product_id: str | None, detail: dict) -> None:
        self.db.execute("INSERT INTO findings (kind, episode_id, product_id, detail_json, created_utc)"
                        " VALUES (?,?,?,?,?)", (kind, episode_id, product_id, json.dumps(detail, sort_keys=True),
                                                now_utc()))

    def add_finding(self, kind: str, detail: dict, episode_id: str | None = None,
                    product_id: str | None = None) -> None:
        self._finding(kind, episode_id, product_id, detail)

    def findings(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM findings ORDER BY finding_id"):
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    def summary(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchall()
        return {"episodes": q("SELECT COUNT(*) FROM episodes")[0][0],
                "products": {f"{r[0]}@{r[1]}": r[2] for r in q(
                    "SELECT product_type, algorithm_version, COUNT(*) FROM products GROUP BY 1, 2 ORDER BY 1, 2")},
                "current": {r[0]: r[1] for r in q(
                    "SELECT product_type, COUNT(*) FROM products WHERE is_current = 1 GROUP BY 1 ORDER BY 1")},
                "findings": q("SELECT COUNT(*) FROM findings")[0][0]}


# ---------------------------------------------------------------------------------------------- CLI

def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "(none)"
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    line = lambda vals: "  ".join(str(v).ljust(w[c]) for c, v in zip(cols, vals))
    return "\n".join([line(cols), line("-" * w[c] for c in cols)] + [line(r.get(c, "") for c in cols) for r in rows])


def main(argv=None) -> int:
    from .config import Settings
    p = argparse.ArgumentParser(prog="python -m doomsat_sds.catalog", description=__doc__.split("\n")[0])
    p.add_argument("--catalog", default=None, help="catalog file (default $DOOMSAT_SDS_HOME/catalog.sqlite)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    sub.add_parser("episodes")
    pp = sub.add_parser("products")
    pp.add_argument("--episode")
    pp.add_argument("--type")
    pp.add_argument("--current", action="store_true")
    ps = sub.add_parser("show")
    ps.add_argument("product_id")
    sub.add_parser("findings")
    pq = sub.add_parser("sql")
    pq.add_argument("query")
    a = p.parse_args(argv)
    path = Path(a.catalog) if a.catalog else Settings.from_env().catalog
    if not path.exists():
        print(f"no catalog yet at {path}", file=sys.stderr)
        return 1
    with Catalog(path, readonly=True) as c:
        if a.cmd == "summary":
            s = c.summary()
            print("catalog: %d episodes, %d findings; products %s; current %s" % (
                s["episodes"], s["findings"], json.dumps(s["products"]), json.dumps(s["current"])))
        elif a.cmd == "episodes":
            print(_table(c.episodes(), ["episode_id", "number", "outcome", "duration_s", "wad", "map", "skill",
                                         "pilot_mode", "level_set", "repo_commit"]))
        elif a.cmd == "products":
            rows = c.products(a.episode, a.type, a.current)
            for r in rows:
                r["sha256"] = r["sha256"][:12]
                r["current"] = "*" if r["is_current"] else ""
            print(_table(rows, ["product_id", "level", "algorithm_version", "current", "size_bytes", "sha256",
                                "airflow_run_id"]))
        elif a.cmd == "show":
            r = c.product(a.product_id)
            if r is None:
                print("no such product", file=sys.stderr)
                return 1
            print(json.dumps(r, indent=2, sort_keys=True))
            if r["media_type"] == "application/json" and Path(r["path"]).exists():
                print(json.dumps(json.loads(Path(r["path"]).read_text()), indent=2, sort_keys=True)[:20000])
        elif a.cmd == "findings":
            for f in c.findings():
                print(json.dumps(f, sort_keys=True))
        elif a.cmd == "sql":
            cur = c.db.execute(a.query)
            cols = [d[0] for d in cur.description]
            print(_table([dict(zip(cols, r)) for r in cur.fetchall()], cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
