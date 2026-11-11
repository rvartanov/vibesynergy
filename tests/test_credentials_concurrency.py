"""Synthetic concurrent credential updates; never use real accounts or HTTP."""

import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources import oauth
from vibesynergy.sources.strava.client import Response, StravaClient


def _refresh_process(root, barrier, calls, output):
    store = SecretStore(root, "strava")

    def http(method, url, params, headers):
        with calls.get_lock():
            calls.value += 1
        assert params["refresh_token"] == "synthetic-old-refresh"
        return Response(
            200,
            {},
            {
                "access_token": "synthetic-new-access",
                "refresh_token": "synthetic-new-refresh",
                "expires_at": int(time.time()) + 3600,
            },
        )

    barrier.wait(timeout=10)
    output.put(StravaClient(store, http=http)._access_token())


@pytest.mark.skipif(os.name != "posix", reason="Current host credential locking is POSIX")
def test_processes_share_one_token_rotation(tmp_path):
    store = SecretStore(tmp_path, "strava")
    store.set(oauth.APP, {"client_id": "synthetic", "client_secret": "synthetic-secret"})
    store.set(
        oauth.TOKENS,
        {
            "access_token": "synthetic-old-access",
            "refresh_token": "synthetic-old-refresh",
            "expires_at": 0,
        },
    )
    ctx = multiprocessing.get_context("spawn")
    barrier, calls, output = ctx.Barrier(4), ctx.Value("i", 0), ctx.Queue()
    processes = [
        ctx.Process(target=_refresh_process, args=(tmp_path, barrier, calls, output))
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        answers = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert answers == ["synthetic-new-access"] * 4 and calls.value == 1
        assert store.get(oauth.TOKENS)["refresh_token"] == "synthetic-new-refresh"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()


def test_atomic_parallel_writes_and_failed_save_preserve_complete_document(tmp_path, monkeypatch):
    def write(i):
        SecretStore(tmp_path, "synthetic").set("record", {"value": str(i) * 1000})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    store = SecretStore(tmp_path, "synthetic")
    previous = store.get("record")
    assert previous["value"] in {str(i) * 1000 for i in range(40)}
    assert (tmp_path / "synthetic/record.enc").stat().st_mode & 0o777 == 0o600

    def fail(*args, **kwargs):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.set("record", {"value": "replacement"})
    assert store.get("record") == previous
    assert sorted(p.name for p in (tmp_path / "synthetic").iterdir()) == [
        ".record.write.lock",
        "record.enc",
    ]


def test_late_401_uses_rotation_already_saved_by_another_client(tmp_path):
    store = SecretStore(tmp_path, "strava")
    expiry = int(time.time()) + 3600
    store.set(oauth.TOKENS, {"access_token": "old", "refresh_token": "r1", "expires_at": expiry})
    calls = []

    def http(method, url, params, headers):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            store.set(
                oauth.TOKENS, {"access_token": "new", "refresh_token": "r2", "expires_at": expiry}
            )
            return Response(401, {}, {})
        return Response(200, {}, {"id": "synthetic"})

    assert StravaClient(store, http=http).get("/athlete")["id"] == "synthetic"
    assert calls == ["Bearer old", "Bearer new"]


@pytest.mark.parametrize(
    "response",
    [
        Response(400, {}, {"secret": "do-not-log-this-response"}),
        Response(200, {}, {"access_token": "missing-refresh"}),
        Response(200, {}, {"access_token": "a", "refresh_token": "r", "expires_at": 0}),
    ],
)
def test_failed_refresh_retains_credentials_and_redacts_provider_payload(tmp_path, response):
    store = SecretStore(tmp_path, "strava")
    store.set(oauth.APP, {"client_id": "id", "client_secret": "synthetic"})
    old = {"access_token": "old", "refresh_token": "old-refresh", "expires_at": 0}
    store.set(oauth.TOKENS, old)
    with pytest.raises(oauth.AuthError) as error:
        StravaClient(store, http=lambda *args: response)._access_token()
    assert "do-not-log-this-response" not in str(error.value)
    assert store.get(oauth.TOKENS) == old
    assert (tmp_path / "strava/tokens.enc").exists()
    assert not (tmp_path / "strava/tokens.json").exists()


@pytest.mark.parametrize("reject_again", [False, True])
def test_current_rejected_token_refreshes_once_and_read_retry_is_bounded(tmp_path, reject_again):
    from vibesynergy.sources.strava.client import StravaError

    store = SecretStore(tmp_path, "strava")
    store.set(oauth.APP, {"client_id": "id", "client_secret": "synthetic"})
    expiry = int(time.time()) + 3600
    store.set(oauth.TOKENS, {"access_token": "old", "refresh_token": "r1", "expires_at": expiry})
    reads, rotations = [], []

    def http(method, url, params, headers):
        if method == "POST":
            rotations.append(params["refresh_token"])
            return Response(
                200, {}, {"access_token": "new", "refresh_token": "r2", "expires_at": expiry}
            )
        reads.append(headers["Authorization"])
        return Response(401 if len(reads) == 1 or reject_again else 200, {}, {})

    client = StravaClient(store, http=http)
    if reject_again:
        with pytest.raises(StravaError):
            client.get("/athlete")
    else:
        assert client.get("/athlete") == {}
    assert rotations == ["r1"] and reads == ["Bearer old", "Bearer new"]
