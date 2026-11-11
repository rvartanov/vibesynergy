"""Web Push for alerts: RFC 8291 encryption, VAPID signature, subscriptions."""

from __future__ import annotations

import json

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vibesynergy import alerts, webpush


def _browser():
    key = ec.generate_private_key(ec.SECP256R1())
    return key, webpush._point(key.public_key()), b"0123456789abcdef"


def _decrypt(body: bytes, ua_key, ua_public: bytes, auth: bytes) -> bytes:
    """What the browser does (RFC 8291 section 3)."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, cipher = body[21 : 21 + idlen], body[21 + idlen :]
    assert rs == 4096 and idlen == 65
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = ua_key.exchange(ec.ECDH(), sender)
    prk_key = webpush._hmac(auth, shared)
    ikm = webpush._hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = webpush._hmac(salt, ikm)
    cek = webpush._hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = webpush._hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, cipher, None)
    assert plain.endswith(b"\x02")  # the last-record delimiter
    return plain[:-1]


def test_round_trip_only_the_browser_can_read_it() -> None:
    ua_key, ua_public, auth = _browser()
    body = webpush.encrypt(b'{"title":"VibeSynergy"}', ua_public, auth)
    assert _decrypt(body, ua_key, ua_public, auth) == b'{"title":"VibeSynergy"}'
    other_key, _, _ = _browser()
    with pytest.raises(InvalidTag):
        _decrypt(body, other_key, ua_public, auth)


def test_vapid_key_is_made_once_and_the_jwt_verifies(runtime) -> None:
    first = webpush.public_key(runtime.settings)
    assert webpush.public_key(runtime.settings) == first and len(webpush.unb64u(first)) == 65
    key = webpush.vapid_key(runtime.settings)
    token = webpush._jwt(key, "https://web.push.apple.com/abc", 1_000_000.0)
    head, body, sig = token.split(".")
    claims = json.loads(webpush.unb64u(body))
    assert claims["aud"] == "https://web.push.apple.com" and claims["exp"] == 1_000_000 + 12 * 3600
    assert claims["sub"].startswith("https://") and "localhost" not in claims["sub"]  # Apple
    raw = webpush.unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


class _Reply:
    def __init__(self, code):
        self.status_code = code


def test_send_all_counts_and_drops_gone_devices(runtime) -> None:
    keys = {}
    for name, code in (("ok", 201), ("gone", 410), ("busy", 500)):
        ua_key, ua_public, auth = _browser()
        endpoint = f"https://push.example/{name}"
        keys[endpoint] = (ua_key, ua_public, auth, code)
        webpush.subscribe(
            runtime.engine, endpoint, webpush.b64u(ua_public), webpush.b64u(auth), "test"
        )
    with pytest.raises(ValueError):
        webpush.subscribe(
            runtime.engine, "http://plain", webpush.b64u(ua_public), webpush.b64u(auth)
        )
    seen = {}

    def http(url, data, headers, timeout):
        ua_key, ua_public, auth, code = keys[url]
        seen[url] = json.loads(_decrypt(data, ua_key, ua_public, auth))
        assert headers["Content-Encoding"] == "aes128gcm" and headers["Authorization"].startswith(
            "vapid t="
        )
        return _Reply(code)

    assert (
        webpush.send_all(runtime, "Let op", "HRV laag.\n\nNeem rust.", url="/#/inbox", http=http)
        == 1
    )
    assert seen["https://push.example/ok"] == {
        "title": "Let op",
        "body": "HRV laag.\nNeem rust.",
        "url": "/#/inbox",
    }
    left = {s["endpoint"]: s for s in webpush.subscriptions(runtime.engine)}
    assert set(left) == {"https://push.example/ok", "https://push.example/busy"}
    assert (
        left["https://push.example/busy"]["failures"] == 1
        and left["https://push.example/ok"]["last_ok"]
    )


def test_alerts_use_push_once_a_device_is_subscribed(runtime, monkeypatch) -> None:
    assert alerts.channels(runtime.settings, runtime.engine)["push"] is False
    _, ua_public, auth = _browser()
    webpush.subscribe(
        runtime.engine, "https://push.example/x", webpush.b64u(ua_public), webpush.b64u(auth)
    )
    assert alerts.channels(runtime.settings, runtime.engine)["push"] is True
    sent = []
    monkeypatch.setattr(webpush, "send_all", lambda rt, title, body, url: sent.append(title) or 1)
    assert alerts.deliver(runtime, "VibeSynergy: test", "body", only=["push"]) == ["push"]
    assert sent == ["VibeSynergy: test"]


def test_portal_push_routes(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    write = {"x-vibesynergy": "portal", "origin": "http://localhost"}
    key = api.get("/api/push").json()["data"]["key"]
    assert len(webpush.unb64u(key)) == 65
    _, ua_public, auth = _browser()
    body = {
        "endpoint": "https://push.example/y",
        "p256dh": webpush.b64u(ua_public),
        "auth": webpush.b64u(auth),
    }
    assert api.post("/api/push/subscribe", json=body, headers=write).status_code == 200
    assert len(api.get("/api/push").json()["data"]["devices"]) == 1
    assert api.post("/api/push/unsubscribe", json=body, headers=write).status_code == 200
    assert api.get("/api/push").json()["data"]["devices"] == []
    assert api.post("/api/push/test", headers=write).status_code == 400  # no device
