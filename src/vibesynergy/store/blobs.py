"""Content-addressed blob store: every payload and file, stored once, zstd-compressed.

Path: ``<root>/<sha[0:2]>/<sha[2:4]>/<sha256>.zst``, where sha256 is of the uncompressed bytes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import zstandard

_LEVEL = 10


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.zst"

    def put(self, data: bytes) -> str:
        sha256 = hashlib.sha256(data).hexdigest()
        path = self.path_for(sha256)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(zstandard.ZstdCompressor(level=_LEVEL).compress(data))
            os.replace(tmp, path)
        return sha256

    def get(self, sha256: str) -> bytes:
        return zstandard.ZstdDecompressor().decompress(self.path_for(sha256).read_bytes())

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()
