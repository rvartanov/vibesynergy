"""Credential storage for sources.

Each source gets its own documents under ``<config dir>/secrets/<source>/``, readable only
by the owner (files 0600, directories 0700). Documents are AES-256-GCM ``<name>.enc`` files.
The per-installation key lives in ``<config dir>/keys/secrets.key`` (mode 0600 in a 0700
directory), apart from the documents. Each document is bound to its source and name (as
AAD), so files can't be swapped between slots.

Older versions wrote plain ``<name>.json`` documents. Those are still read, and are rewritten
encrypted (and the plaintext removed) on first read, or in one go with
``vibesynergy secrets encrypt``. When both formats exist for one document, the newer file wins:
before every store was encrypted, some writers kept the ``.json`` copy current next to an
older ``.enc`` one.

Encryption protects the documents when they are copied or backed up without the key (or
exposed through a path-traversal or a leaked ``secrets/`` folder); it does not protect against
anyone who can read the key file as the service user. A keyring or KMS backend can replace
the files later without touching sources.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"VTL1"
KEY_FILE = "secrets.key"

log = logging.getLogger(__name__)


class SecretError(Exception):
    """A stored secret can't be decrypted (wrong or missing key, damaged file)."""


def _load_key(keys_dir: Path, create: bool) -> bytes | None:
    path = keys_dir / KEY_FILE
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise SecretError(f"the secrets key file {path} is damaged")
        return key
    if not create:
        return None
    keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(keys_dir, 0o700)
    key = os.urandom(32)
    try:
        # O_EXCL: two processes creating the key at once must end up with the same one.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_key(keys_dir, False)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())
    return key


@dataclass(frozen=True)
class Migration:
    """What ``encrypt_plaintext`` did (or would do) with one document; never its value."""

    source: str
    name: str
    action: str  # "encrypted", "stale-plaintext-removed"


class SecretStore:
    def __init__(self, root: Path, source_id: str, *, encrypted: bool = True) -> None:
        # ``encrypted=False`` only exists so tests can write legacy plaintext documents.
        self._dir = root / source_id
        self._source = source_id
        self._keys = root.parent / "keys"
        self._encrypted = encrypted

    def _aad(self, name: str) -> bytes:
        return f"{self._source}/{name}".encode()

    def _decrypt(self, name: str, blob: bytes) -> Any:
        key = _load_key(self._keys, create=False)
        where = f"{self._source}/{name}"
        if key is None:
            raise SecretError(
                f"can't decrypt the stored {where} secret: no key at {self._keys / KEY_FILE} "
                "(restore keys/ together with secrets/, or connect the account again)"
            )
        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 28:
            raise SecretError(f"can't decrypt the stored {where} secret: the file is damaged")
        nonce, body = blob[4:16], blob[16:]
        try:
            return json.loads(AESGCM(key).decrypt(nonce, body, self._aad(name)))
        except InvalidTag:
            raise SecretError(
                f"can't decrypt the stored {where} secret: it was written with another key "
                f"than {self._keys / KEY_FILE}, or the file was changed"
            ) from None

    def _plaintext_wins(self, name: str) -> bool:
        """A ``.json`` document exists and is newer than its ``.enc`` (if any)."""
        plain, enc = self._dir / f"{name}.json", self._dir / f"{name}.enc"
        try:
            plain_mtime = plain.stat().st_mtime_ns
        except FileNotFoundError:
            return False
        try:
            return plain_mtime > enc.stat().st_mtime_ns
        except FileNotFoundError:
            return True

    def get(self, name: str) -> Any:
        if self._encrypted and self._plaintext_wins(name):
            try:
                found, value = self._migrate(name)
            except OSError as exc:  # e.g. a read-only config dir: keep serving the plaintext
                log.warning("could not encrypt %s/%s: %s", self._source, name, exc)
                path = self._dir / f"{name}.json"
                return json.loads(path.read_text()) if path.exists() else self.get(name)
            if found:
                return value
        enc = self._dir / f"{name}.enc"
        if enc.exists():
            return self._decrypt(name, enc.read_bytes())
        path = self._dir / f"{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def set(self, name: str, value: Any) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._write_guard(name):
            self._write(name, value, encrypted=self._encrypted)
            # One format per document: a plaintext copy must not outlive its encrypted
            # successor.
            if self._encrypted:
                (self._dir / f"{name}.json").unlink(missing_ok=True)

    def _migrate(self, name: str) -> tuple[bool, Any]:
        """Rewrite a plaintext document encrypted and remove the plaintext.

        Returns ``(False, None)`` when there was nothing to migrate any more (another
        process did it, or a newer ``.enc`` was written meanwhile).
        """
        plain = self._dir / f"{name}.json"
        with self._write_guard(name):
            if not self._plaintext_wins(name):
                return False, None
            mtime = plain.stat().st_mtime_ns
            value = json.loads(plain.read_text())
            self._write(name, value, encrypted=True)
            if plain.stat().st_mtime_ns == mtime:
                plain.unlink()
            return True, value

    @contextmanager
    def _write_guard(self, name: str):
        """Short internal lock so a migration never overwrites a concurrent set().

        Separate from lock(), which callers may hold around get()/set().
        """
        import fcntl

        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._dir / f".{name}.write.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _write(self, name: str, value: Any, *, encrypted: bool) -> None:
        """Write one document atomically (temp file, fsync, rename, fsync the directory)."""
        if encrypted:
            key = _load_key(self._keys, create=True)
            nonce = os.urandom(12)
            data = (
                MAGIC
                + nonce
                + AESGCM(key).encrypt(nonce, json.dumps(value).encode(), self._aad(name))
            )
            path = self._dir / f"{name}.enc"
        else:
            data = json.dumps(value).encode()
            path = self._dir / f"{name}.json"
        fd, name_tmp = tempfile.mkstemp(prefix=f".{name}-", dir=self._dir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(name_tmp, path)
            # The attempt journal must survive a crash before an external send.
            dir_fd = os.open(self._dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            Path(name_tmp).unlink(missing_ok=True)

    def documents(self) -> list[str]:
        """Names of the stored documents (either format), sorted."""
        if not self._dir.is_dir():
            return []
        return sorted(
            {p.stem for p in self._dir.iterdir() if p.suffix in (".json", ".enc")}
            - {""}  # dotfiles such as half-written temporaries
        )

    def encrypt_plaintext(self, *, dry_run: bool = False) -> list[Migration]:
        """Encrypt every plaintext document of this store; idempotent.

        A ``.json`` older than its ``.enc`` is a stale copy (``get`` never returns it) and is
        removed. Values are never logged or returned.
        """
        done: list[Migration] = []
        for name in self.documents():
            plain = self._dir / f"{name}.json"
            if not plain.exists():
                continue
            if self._plaintext_wins(name):
                if not dry_run:
                    self._migrate(name)
                done.append(Migration(self._source, name, "encrypted"))
            else:
                if not dry_run:
                    self._decrypt(name, (self._dir / f"{name}.enc").read_bytes())  # key check
                    plain.unlink(missing_ok=True)
                done.append(Migration(self._source, name, "stale-plaintext-removed"))
        return done

    @contextmanager
    def lock(self, name: str):
        """Serialize read/refresh/save on POSIX, across processes and store instances.

        Lock the stable sidecar, not the credential inode replaced by set(). Callers must
        not acquire the same lock recursively; get/set remain atomic but do not lock.
        """
        import fcntl

        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._dir / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def delete(self, name: str) -> None:
        (self._dir / f"{name}.json").unlink(missing_ok=True)
        (self._dir / f"{name}.enc").unlink(missing_ok=True)


def encrypt_all(root: Path, *, dry_run: bool = False) -> list[Migration]:
    """Encrypt the plaintext documents of every store under ``root`` (the secrets dir)."""
    if not root.is_dir():
        return []
    done: list[Migration] = []
    for source_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        done.extend(SecretStore(root, source_dir.name).encrypt_plaintext(dry_run=dry_run))
    return done
