"""Web Push for alerts on the phone, without an extra package.

A notification goes from VibeSynergy to the browser's push service (Apple, Google or Mozilla),
which hands it to the device. The payload is end-to-end encrypted for that one browser
(RFC 8291, aes128gcm), so the push service only sees ciphertext. VibeSynergy signs each request
with its own VAPID key (RFC 8292), kept in the credential store and made on first use.
Subscriptions live in ``push_subscription``; one that the service says is gone (404/410)
is removed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Engine, delete, select, update

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import push_subscription

log = logging.getLogger(__name__)

RECORD_SIZE = 4096
TTL_SECONDS = 12 * 3600
# The push service's contact (RFC 8292 "sub"). Apple refuses a token whose contact is not a
# real mailto: or https: address (403 BadJwtToken, 7-11), so it is the project's site.
SUBJECT = "https://vibesynergy.nl"
MAX_BODY = 240


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _point(public: ec.EllipticCurvePublicKey) -> bytes:
    return public.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


# VAPID key ------------------------------------------------------------------------------------


def _store(settings: Any) -> SecretStore:
    return SecretStore(settings.secrets_dir, "push")


def vapid_key(settings: Any) -> ec.EllipticCurvePrivateKey:
    """VibeSynergy's own signing key, made once and kept in the credential store."""
    store = _store(settings)
    saved = store.get("vapid")
    if isinstance(saved, dict) and saved.get("private"):
        value = int.from_bytes(unb64u(saved["private"]), "big")
        return ec.derive_private_key(value, ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    store.set("vapid", {"private": b64u(raw)})
    return key


def public_key(settings: Any) -> str:
    """The application server key the browser subscribes with (base64url, 65 bytes)."""
    return b64u(_point(vapid_key(settings).public_key()))


def _jwt(key: ec.EllipticCurvePrivateKey, endpoint: str, now: float) -> str:
    parts = urlsplit(endpoint)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(now) + TTL_SECONDS,
        "sub": SUBJECT,
    }
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{header}.{body}".encode()
    r, s = decode_dss_signature(key.sign(signing, ec.ECDSA(hashes.SHA256())))
    return f"{header}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


# RFC 8291 -------------------------------------------------------------------------------------


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    sender: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """One aes128gcm record for one browser (``ua_public`` p256dh, ``auth_secret`` auth)."""
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _point(sender.public_key())
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender.exchange(ec.ECDH(), receiver)
    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise ValueError("push message too long")
    cipher = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + cipher


# Subscriptions --------------------------------------------------------------------------------


def subscribe(engine: Engine, endpoint: str, p256dh: str, auth: str, agent: str = "") -> None:
    if not endpoint.startswith("https://") or len(endpoint) > 2000:
        raise ValueError("a push endpoint is an https address")
    if len(unb64u(p256dh)) != 65 or len(unb64u(auth)) != 16:
        raise ValueError("the browser's push keys are not valid")
    values = {"p256dh": p256dh, "auth": auth, "agent": agent[:200], "failures": 0}
    with engine.begin() as conn:
        found = conn.execute(
            select(push_subscription.c.endpoint).where(push_subscription.c.endpoint == endpoint)
        ).first()
        if found:
            conn.execute(
                update(push_subscription)
                .where(push_subscription.c.endpoint == endpoint)
                .values(**values)
            )
        else:
            conn.execute(
                push_subscription.insert().values(endpoint=endpoint, created_at=utc_now(), **values)
            )


def unsubscribe(engine: Engine, endpoint: str) -> None:
    with engine.begin() as conn:
        conn.execute(delete(push_subscription).where(push_subscription.c.endpoint == endpoint))


def subscriptions(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(push_subscription)).mappings()]


def send_all(
    rt: Any, title: str, body: str, *, url: str = "/", http=requests.post, now: float | None = None
) -> int:
    """Send one notification to every subscribed device; returns how many took it."""
    subs = subscriptions(rt.engine)
    if not subs:
        return 0
    key = vapid_key(rt.settings)
    vapid = b64u(_point(key.public_key()))
    text = body.strip().replace("\n\n", "\n")
    payload = json.dumps(
        {
            "title": title[:120],
            "body": text if len(text) <= MAX_BODY else text[: MAX_BODY - 1] + "…",
            "url": url,
        },
        ensure_ascii=False,
    ).encode()
    sent = 0
    moment = now or time.time()
    for sub in subs:
        endpoint = sub["endpoint"]
        auth = f"vapid t={_jwt(key, endpoint, moment)}, k={vapid}"
        try:
            data = encrypt(payload, unb64u(sub["p256dh"]), unb64u(sub["auth"]))
            reply = http(
                endpoint,
                data=data,
                headers={
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "normal",
                    "Authorization": auth,
                },
                timeout=10,
            )
        except (requests.RequestException, ValueError) as exc:
            log.warning("push to %s failed: %s", urlsplit(endpoint).netloc, exc)
            _failed(rt.engine, endpoint)
            continue
        if reply.status_code in (404, 410):  # the browser unsubscribed or the app was removed
            unsubscribe(rt.engine, endpoint)
        elif 200 <= reply.status_code < 300:
            sent += 1
            with rt.engine.begin() as conn:
                conn.execute(
                    update(push_subscription)
                    .where(push_subscription.c.endpoint == endpoint)
                    .values(last_ok=utc_now(), failures=0)
                )
        else:
            log.warning("push to %s: HTTP %s", urlsplit(endpoint).netloc, reply.status_code)
            _failed(rt.engine, endpoint)
    return sent


def _failed(engine: Engine, endpoint: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(push_subscription)
            .where(push_subscription.c.endpoint == endpoint)
            .values(failures=push_subscription.c.failures + 1)
        )
