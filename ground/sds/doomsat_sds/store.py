"""Product files on disk: one canonical encoding, atomic writes, checksums, and where each product lives.

    $DOOMSAT_SDS_HOME/products/episodes/<episode id>/<type>-<version>.<ext>   L1, L2 and the Phase D record
    $DOOMSAT_SDS_HOME/products/l3/<type>-<version>-<inputs hash>.json         L3
    $DOOMSAT_SDS_HOME/products/quicklook/<yyyymmdd>/<type>-<time>.<ext>       quicklooks

A version never overwrites another: reprocessing writes beside the old file, and the catalog says which is
current.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import Settings

# The process umask, read once at import while the process is still single-threaded. Reading it means setting it,
# and the capture service writes from two threads, so it must never be read again at run time.
_UMASK = os.umask(0)
os.umask(_UMASK)


def canonical_json(obj) -> bytes:
    """The one way products are serialised, so equal content means equal bytes and an equal checksum."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path: str | Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o666 & ~_UMASK)     # mkstemp makes 0600; products are for reading
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def episode_product_path(settings: Settings, episode_id: str, product_type: str, version: str, ext: str) -> Path:
    return settings.products / "episodes" / episode_id / f"{product_type}-{version}.{ext}"


def l3_product_path(settings: Settings, product_type: str, version: str, inputs_hash: str) -> Path:
    return settings.products / "l3" / f"{product_type}-{version}-{inputs_hash[:12]}.json"


def quicklook_path(settings: Settings, product_type: str, stamp: str, ext: str) -> Path:
    return settings.products / "quicklook" / stamp[:8] / f"{product_type}-{stamp}.{ext}"


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
