"""Content-addressed blob store: every payload and file, stored once, zstd-compressed.

Path: ``<root>/<sha[0:2]>/<sha[2:4]>/<sha256>.zst``, where sha256 is of the uncompressed bytes.

A file a browser reads in parts (the playable copy of a workout video, served with HTTP
ranges) is kept uncompressed next to them: ``<root>/plain/<sha[0:2]>/<sha256><suffix>``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import IO

import zstandard

_LEVEL = 10
_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(target: Path, write: Callable[[IO[bytes]], None]) -> None:
    """Write ``target`` through a temporary file of its own in the same folder, then move it
    in place. Two writers of the same blob each have their own temporary file (threads in one
    process too); the second move finds the same content there, which is fine."""
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            write(out)
        try:
            os.replace(tmp, target)
        except OSError:
            if not target.exists():
                raise
    finally:
        tmp.unlink(missing_ok=True)


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / f"{sha256}.zst"

    def put(self, data: bytes) -> str:
        sha256 = hashlib.sha256(data).hexdigest()
        _write_once(
            self.path_for(sha256),
            lambda out: out.write(zstandard.ZstdCompressor(level=_LEVEL).compress(data)),
        )
        return sha256

    def get(self, sha256: str) -> bytes:
        return zstandard.ZstdDecompressor().decompress(self.path_for(sha256).read_bytes())

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def put_file(self, path: Path) -> str:
        """``put`` for a file too large to hold in memory: hashed and compressed in streams."""
        sha256 = file_sha256(path)

        def write(out: IO[bytes]) -> None:
            with path.open("rb") as src:
                # The size in the frame header, so ``get`` can read it like any other blob.
                compressor = zstandard.ZstdCompressor(level=_LEVEL)
                compressor.copy_stream(src, out, size=path.stat().st_size)

        _write_once(self.path_for(sha256), write)
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

        def write(out: IO[bytes]) -> None:
            with path.open("rb") as src:
                while chunk := src.read(_CHUNK):
                    out.write(chunk)

        _write_once(self.plain_path(sha256, suffix), write)
        return sha256
