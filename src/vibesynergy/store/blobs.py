"""Content-addressed blob store: every payload and file, stored once, zstd-compressed.

Path: ``<root>/<sha[0:2]>/<sha[2:4]>/<sha256>.zst``, where sha256 is of the uncompressed bytes.

A file a browser reads in parts (the playable copy of a workout video, served with HTTP
ranges) is kept uncompressed next to them: ``<root>/plain/<sha[0:2]>/<sha256><suffix>``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import zstandard

_LEVEL = 10
_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


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

    def put_file(self, path: Path) -> str:
        """``put`` for a file too large to hold in memory: hashed and compressed in streams."""
        sha256 = file_sha256(path)
        target = self.path_for(sha256)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            with path.open("rb") as src, tmp.open("wb") as dst:
                # The size in the frame header, so ``get`` can read it like any other blob.
                compressor = zstandard.ZstdCompressor(level=_LEVEL)
                compressor.copy_stream(src, dst, size=path.stat().st_size)
            os.replace(tmp, target)
        return sha256

    def copy_to(self, sha256: str, dest: Path) -> None:
        """Write a blob's bytes to ``dest`` without holding them in memory."""
        with self.path_for(sha256).open("rb") as src, dest.open("wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)

    def plain_path(self, sha256: str, suffix: str) -> Path:
        return self.root / "plain" / sha256[:2] / f"{sha256}{suffix}"

    def put_plain_file(self, path: Path, suffix: str) -> str:
        """Keep a file uncompressed, by its SHA-256, so it can be read in parts."""
        sha256 = file_sha256(path)
        target = self.plain_path(sha256, suffix)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            with path.open("rb") as src, tmp.open("wb") as dst:
                while chunk := src.read(_CHUNK):
                    dst.write(chunk)
            os.replace(tmp, target)
        return sha256
