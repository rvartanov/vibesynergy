"""Credential store: every store encrypts, legacy plaintext is migrated transparently."""

from __future__ import annotations

import json
import os
import stat

import pytest
from typer.testing import CliRunner

from vibesynergy.cli.main import app
from vibesynergy.core.secrets import SecretError, SecretStore, encrypt_all

SECRET = "synthetic-refresh-token-7f3a"


def _legacy(root, source, name, value):
    """A plaintext document as versions before the encrypted default wrote it."""
    SecretStore(root, source, encrypted=False).set(name, value)
    return root / source / f"{name}.json"


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("strava", "tokens"),
        ("garmin", "tokens"),
        ("withings", "app"),
        ("mail", "password"),
        ("github", "token"),
        ("mcp", "passphrase"),
        ("alerts", "state"),
    ],
)
def test_every_store_writes_encrypted_with_owner_only_modes(tmp_path, source, name):
    root = tmp_path / "config" / "secrets"
    store = SecretStore(root, source)
    store.set(name, {"value": SECRET})
    enc = root / source / f"{name}.enc"
    assert SECRET.encode() not in enc.read_bytes()
    assert not (root / source / f"{name}.json").exists()
    assert _mode(enc) == 0o600 and _mode(enc.parent) == 0o700
    assert _mode(tmp_path / "config" / "keys" / "secrets.key") == 0o600
    assert SecretStore(root, source).get(name) == {"value": SECRET}


def test_plaintext_read_rewrites_it_encrypted(tmp_path):
    root = tmp_path / "c" / "secrets"
    plain = _legacy(root, "garmin", "tokens", {"value": SECRET})
    assert SecretStore(root, "garmin").get("tokens") == {"value": SECRET}
    enc = root / "garmin" / "tokens.enc"
    assert not plain.exists() and enc.exists()
    assert SECRET.encode() not in enc.read_bytes()
    assert _mode(enc) == 0o600
    before = enc.read_bytes()
    assert SecretStore(root, "garmin").get("tokens") == {"value": SECRET}
    assert enc.read_bytes() == before  # a second read leaves the encrypted file alone


def test_newer_plaintext_beats_an_older_encrypted_copy(tmp_path):
    """Older versions kept writing ``.json`` next to an ``.enc``: the newest one wins."""
    root = tmp_path / "c" / "secrets"
    SecretStore(root, "strava").set("tokens", {"refresh": "old"})
    enc = root / "strava" / "tokens.enc"
    os.utime(enc, ns=(1_000_000_000, 1_000_000_000))
    _legacy(root, "strava", "tokens", {"refresh": "new"})
    assert SecretStore(root, "strava").get("tokens") == {"refresh": "new"}
    assert not (root / "strava" / "tokens.json").exists()
    assert SecretStore(root, "strava").get("tokens") == {"refresh": "new"}


def test_older_plaintext_next_to_newer_encrypted_is_ignored_and_cleaned(tmp_path):
    root = tmp_path / "c" / "secrets"
    SecretStore(root, "strava").set("tokens", {"refresh": "current"})
    plain = root / "strava" / "tokens.json"
    plain.write_text(json.dumps({"refresh": "stale"}))  # a leftover copy, older than the .enc
    os.utime(plain, ns=(1_000_000_000, 1_000_000_000))
    assert SecretStore(root, "strava").get("tokens") == {"refresh": "current"}
    assert [m.action for m in encrypt_all(root)] == ["stale-plaintext-removed"]
    assert not plain.exists()
    assert SecretStore(root, "strava").get("tokens") == {"refresh": "current"}


def test_encrypt_all_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "c" / "secrets"
    _legacy(root, "mail", "password", {"password": SECRET})
    _legacy(root, "withings", "app", {"client_secret": SECRET})
    SecretStore(root, "caldav").set("account", {"password": SECRET})
    (root / "publication").mkdir(mode=0o700)
    (root / "publication" / "strava-1.lock").touch()  # lock files are not documents

    preview = encrypt_all(root, dry_run=True)
    assert [(m.source, m.name, m.action) for m in preview] == [
        ("mail", "password", "encrypted"),
        ("withings", "app", "encrypted"),
    ]
    assert (root / "mail" / "password.json").exists()
    assert not (root / "mail" / "password.enc").exists()

    enc_before = (root / "caldav" / "account.enc").read_bytes()
    assert len(encrypt_all(root)) == 2
    assert not list(root.glob("*/*.json"))
    assert (root / "caldav" / "account.enc").read_bytes() == enc_before  # untouched
    assert _mode(root / "mail" / "password.enc") == 0o600
    assert encrypt_all(root) == []
    assert SecretStore(root, "mail").get("password") == {"password": SECRET}
    assert SecretStore(root, "withings").get("app") == {"client_secret": SECRET}


def test_wrong_key_names_the_document_and_the_key_file(tmp_path):
    root = tmp_path / "c" / "secrets"
    SecretStore(root, "github").set("token", {"token": SECRET})
    key = tmp_path / "c" / "keys" / "secrets.key"
    key.write_bytes(os.urandom(32))
    with pytest.raises(SecretError) as err:
        SecretStore(root, "github").get("token")
    message = str(err.value)
    assert "github/token" in message and "another key" in message and str(key) in message
    assert SECRET not in message
    key.unlink()
    with pytest.raises(SecretError, match="no key at"):
        SecretStore(root, "github").get("token")


def test_cli_encrypt_dry_run_then_apply_never_prints_values(tmp_path, monkeypatch):
    config = tmp_path / "cfg" / "config.toml"
    config.parent.mkdir()
    config.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    root = config.parent / "secrets"
    _legacy(root, "mail", "password", {"password": SECRET})
    runner = CliRunner()

    dry = runner.invoke(app, ["secrets", "encrypt", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would encrypt mail/password" in dry.output and SECRET not in dry.output
    assert (root / "mail" / "password.json").exists()

    done = runner.invoke(app, ["secrets", "encrypt"])
    assert done.exit_code == 0, done.output
    assert "encrypted mail/password" in done.output and SECRET not in done.output
    assert not (root / "mail" / "password.json").exists()

    again = runner.invoke(app, ["secrets", "encrypt"])
    assert again.exit_code == 0 and "Nothing to do" in again.output
