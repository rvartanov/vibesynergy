"""Videos of a workout (#114): kept as they came, played from a copy without the file's
metadata (so not where it was filmed), with a poster frame, one video once, from the portal,
Strava and the Strava archive.

Synthetic videos made with ffmpeg in the test; the tests skip without it."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_strava_export import HEADER, ROWS
from test_workout_media import WRITE, FakeReader, _act, _stored, picture

from vibesynergy import imports
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import media_pull, video, workout_media
from vibesynergy.store.blobs import BlobStore
from vibesynergy.store.schema import media_check, media_item, media_origin

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg is not installed"
)
PLACE = "+53.2012+005.7999/"


def make_video(path: Path, seconds: float = 2, colour: str = "testsrc") -> bytes:
    """A tiny MP4 with sound and the place it was "filmed" in its metadata."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"{colour}=size=160x120:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=duration={seconds}",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
            "-metadata", f"location={PLACE}", "-metadata", "comment=private",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path.read_bytes()


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> bytes:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg is not installed")
    return make_video(tmp_path_factory.mktemp("video") / "clip.mp4")


@pytest.fixture(scope="module")
def other_clip(tmp_path_factory) -> bytes:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg is not installed")
    return make_video(tmp_path_factory.mktemp("video") / "other.mp4", 1, "smptebars")


def _tags(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    info = json.loads(out)
    tags = dict(info["format"].get("tags") or {})
    for stream in info["streams"]:
        tags.update(stream.get("tags") or {})
    return tags


def _file(tmp_path, data: bytes, name="clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _row(rt, media_id):
    with rt.engine.connect() as conn:
        return conn.execute(select(media_item).where(media_item.c.id == media_id)).mappings().one()


def test_the_bytes_tell_a_video_from_a_photo(clip) -> None:
    assert video.looks_like_video(clip[:16])
    assert not video.looks_like_video(picture(1)[:16])
    assert not video.looks_like_video(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00")  # a HEIC photo


@needs_ffmpeg
def test_a_video_is_kept_and_played_without_where_it_was_filmed(runtime, tmp_path, clip):
    run = _act(runtime, "1")
    assert "location" in _tags(_file(tmp_path, clip))
    done = workout_media.add_video(runtime.engine, runtime.raw.blobs, run, _file(tmp_path, clip))
    assert done["state"] == "added" and done["kind"] == "video"
    row = _row(runtime, done["media"])
    assert row["kind"] == "video" and row["content_type"] == "video/mp4"
    assert (row["width"], row["height"]) == (160, 120) and 1.5 < row["duration_s"] < 2.5
    assert runtime.raw.blobs.get(row["sha256"]) == clip  # the original, as it came
    playable = workout_media.video_file(runtime.engine, runtime.raw.blobs, done["media"])
    tags = _tags(playable)
    assert not any("location" in key for key in tags) and "comment" not in tags
    assert PLACE.encode() not in playable.read_bytes()
    # The poster: a JPEG frame in both sizes.
    for size in workout_media.SIZES:
        poster = workout_media.file(runtime.engine, runtime.raw.blobs, done["media"], size)
        assert poster[:3] == b"\xff\xd8\xff"
    shown = workout_media.photos(runtime.engine, run)["photos"][0]
    assert shown["kind"] == "video" and shown["duration_s"] == row["duration_s"]


@needs_ffmpeg
def test_add_takes_a_video_too_and_keeps_it_once(runtime, tmp_path, clip, other_clip):
    run = _act(runtime, "1")
    first = workout_media.add(runtime.engine, runtime.raw.blobs, run, clip)
    assert first["kind"] == "video"
    again = workout_media.add_video(runtime.engine, runtime.raw.blobs, run, _file(tmp_path, clip))
    assert again["state"] == "same" and again["media"] == first["media"]
    other = workout_media.add(runtime.engine, runtime.raw.blobs, run, other_clip)
    assert other["state"] == "added"
    assert workout_media.count(runtime.engine, run) == 2


@needs_ffmpeg
def test_videos_count_toward_the_workout_limit(runtime, clip, monkeypatch):
    run = _act(runtime, "1")
    monkeypatch.setattr(workout_media, "MAX_PER_WORKOUT", 1)
    workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(1))
    with pytest.raises(workout_media.TooMany):
        workout_media.add(runtime.engine, runtime.raw.blobs, run, clip)


@needs_ffmpeg
def test_a_video_too_large_or_not_a_video_is_refused(runtime, tmp_path, clip, monkeypatch):
    run = _act(runtime, "1")
    broken = _file(tmp_path, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "broken.mp4")
    with pytest.raises(workout_media.NotAPhoto):
        workout_media.add_video(runtime.engine, runtime.raw.blobs, run, broken)
    monkeypatch.setattr(workout_media, "MAX_VIDEO_BYTES", 100)
    with pytest.raises(workout_media.TooLarge):
        workout_media.add_video(runtime.engine, runtime.raw.blobs, run, _file(tmp_path, clip))


def test_without_ffmpeg_a_video_is_refused_and_photos_still_come_in(runtime, monkeypatch):
    run = _act(runtime, "1")
    monkeypatch.setattr(video.shutil, "which", lambda name: None)
    fake = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    with pytest.raises(workout_media.NoVideoTool):
        workout_media.add(runtime.engine, runtime.raw.blobs, run, fake)
    assert workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(1))["state"] == (
        "added"
    )


@needs_ffmpeg
def test_a_missing_playable_copy_is_made_again(runtime, tmp_path, clip):
    run = _act(runtime, "1")
    done = workout_media.add_video(runtime.engine, runtime.raw.blobs, run, _file(tmp_path, clip))
    path = workout_media.video_file(runtime.engine, runtime.raw.blobs, done["media"])
    before = path.read_bytes()
    path.unlink()
    again = workout_media.video_file(runtime.engine, runtime.raw.blobs, done["media"])
    assert again.read_bytes() == before  # the same copy (bitexact), the same address


# --- the portal --------------------------------------------------------------------------


@needs_ffmpeg
def test_upload_and_play_a_video_in_parts(runtime, clip):
    run = _act(runtime, "1")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    files = [
        ("files", ("ride.mp4", clip, "video/mp4")),
        ("files", ("one.jpg", picture(1), "image/jpeg")),
    ]
    reply = client.post(
        "/api/activity-media/upload", params={"id": run}, files=files, headers=WRITE
    )
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert [(r["state"], r["kind"]) for r in data["results"]] == [
        ("added", "video"),
        ("added", "photo"),
    ]
    page = client.get("/api/activity-media", params={"id": run}).json()["data"]
    assert page["max_video_mb"] == workout_media.MAX_VIDEO_BYTES // (1024 * 1024)
    clip_id = next(p["id"] for p in page["photos"] if p["kind"] == "video")
    whole = client.get(f"/api/activity-media/video/{clip_id}")
    assert whole.status_code == 200 and whole.headers["content-type"] == "video/mp4"
    assert whole.headers.get("accept-ranges") == "bytes"
    assert PLACE.encode() not in whole.content and whole.content != clip
    part = client.get(f"/api/activity-media/video/{clip_id}", headers={"range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"].startswith("bytes 0-99/")
    assert part.content == whole.content[:100]
    poster = client.get(f"/api/activity-media/photo/{clip_id}", params={"size": "thumb"})
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/jpeg"
    assert "content-security-policy" in whole.headers  # the CSP stays on every answer
    # A photo is no video.
    photo_id = next(p["id"] for p in page["photos"] if p["kind"] == "photo")
    assert client.get(f"/api/activity-media/video/{photo_id}").status_code == 404
    # Remove and put back, as for a photo.
    removed = client.post(
        "/api/activity-media/remove", json={"id": run, "media": clip_id}, headers=WRITE
    )
    assert [p["kind"] for p in removed.json()["data"]["photos"]] == ["photo"]
    history = client.get("/api/activity-media", params={"id": run}).json()["data"]["history"]
    assert history[0]["kind"] == "video" and history[0]["action"] == "removed"
    back = client.post(
        "/api/activity-media/restore", json={"id": run, "media": clip_id}, headers=WRITE
    )
    assert len(back.json()["data"]["photos"]) == 2


def test_upload_without_ffmpeg_says_so(runtime, monkeypatch):
    run = _act(runtime, "1")
    monkeypatch.setattr(video.shutil, "which", lambda name: None)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    fake = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    files = [
        ("files", ("ride.mp4", fake, "video/mp4")),
        ("files", ("one.jpg", picture(1), "image/jpeg")),
    ]
    reply = client.post(
        "/api/activity-media/upload", params={"id": run}, files=files, headers=WRITE
    )
    results = reply.json()["data"]["results"]
    assert [r["state"] for r in results] == ["refused", "added"]
    assert results[0]["code"] == "no_video_tool"


@needs_ffmpeg
def test_a_share_link_plays_the_video_but_never_a_removed_one(runtime, clip, other_clip):
    from vibesynergy.api.app import create_app
    from vibesynergy.portal import share
    from vibesynergy.runtime import Runtime

    host = "server.example.ts.net"
    rt = Runtime.open(replace(runtime.settings, server={"public_url": f"https://{host}"}))
    try:
        run = _act(rt, "1")
        shown = workout_media.add(rt.engine, rt.raw.blobs, run, clip)["media"]
        hidden = workout_media.add(rt.engine, rt.raw.blobs, run, other_clip)["media"]
        workout_media.remove(rt.engine, run, hidden)
        code, _ = share.create(rt.engine, "Coach Anna", ["activities"], can_write=False)
        client = TestClient(create_app(rt, with_mcp=False), base_url=f"https://{host}")
        base = f"/vibesynergy/share/{code}/api"
        played = client.get(f"{base}/activity-media/video/{shown}")
        assert played.status_code == 200 and PLACE.encode() not in played.content
        assert played.content != clip  # never the original
        assert client.get(f"{base}/activity-media/video/{hidden}").status_code == 404
    finally:
        rt.close()


# --- Strava ------------------------------------------------------------------------------


class VideoReader(FakeReader):
    """A Strava activity with a photo and a video."""

    def __init__(self, store, videos):
        super().__init__(store)
        self.videos = videos  # {photo id: bytes}
        self.fetched: list[str] = []

    def photos(self, source_id):
        listed = super().photos(source_id)
        for pid in self.videos:
            listed.append({"photo_id": pid, "url": f"https://cdn.example.test/{pid}.m3u8",
                           "video": True, "caption": "Finish"})  # fmt: skip
        return listed

    def download_video(self, url, dest):
        self.fetched.append(url)
        dest.write_bytes(self.videos[url.rsplit("/", 1)[1].removesuffix(".m3u8")])


@needs_ffmpeg
def test_strava_videos_come_in_once(runtime, clip):
    run = _act(runtime, "11")
    _stored(runtime, "strava", "11", 2)
    reader = VideoReader({"11": [("p1", picture(1))]}, {"v1": clip})
    result = media_pull.pull_one(runtime, reader, "strava", "11")
    assert result["added"] == 2 and not result["errors"]
    kinds = sorted(p["kind"] for p in workout_media.photos(runtime.engine, run)["photos"])
    assert kinds == ["photo", "video"]
    again = media_pull.pull_one(runtime, reader, "strava", "11")
    assert again["added"] == 0 and reader.fetched == ["https://cdn.example.test/v1.m3u8"]


def test_a_strava_video_without_ffmpeg_waits_without_using_up_the_retry(runtime, monkeypatch):
    run = _act(runtime, "11")
    _stored(runtime, "strava", "11", 1)
    monkeypatch.setattr(video.shutil, "which", lambda name: None)
    fake = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    reader = VideoReader({}, {"v1": fake})
    result = media_pull.pull_one(runtime, reader, "strava", "11")
    assert result["added"] == 0 and not result["errors"] and result["waiting"]
    assert reader.fetched == []  # nothing downloaded that can't be used
    assert not workout_media.known(runtime.engine, "strava", "v1")
    later = datetime(2026, 11, 25, 12, tzinfo=UTC)
    for n in range(3):  # each wait is looked at again: it never uses up the one retry
        media_pull.run(runtime, now=later + media_pull.RETRY * n * 2, readers={"strava": reader})
        with runtime.engine.connect() as conn:
            check = conn.execute(select(media_check)).mappings().one()
        assert check["error"].startswith(media_pull.WAITING) and check["next_at"] is not None
    assert run and len(reader.reads) == 4


@needs_ffmpeg
def test_a_run_downloads_a_limited_number_of_videos(runtime, clip, other_clip, monkeypatch):
    run = _act(runtime, "11")
    _stored(runtime, "strava", "11", 2)
    monkeypatch.setattr(media_pull, "MAX_VIDEOS_PER_RUN", 1)
    reader = VideoReader({}, {"v1": clip, "v2": other_clip})
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    first = media_pull.run(runtime, now=now, readers={"strava": reader})
    assert first["added"] == 1 and len(reader.fetched) == 1
    second = media_pull.run(runtime, now=now + timedelta(minutes=30), readers={"strava": reader})
    assert second["added"] == 1 and len(reader.fetched) == 2
    assert workout_media.count(runtime.engine, run) == 2


class FakeWeb:
    """``requests.get`` for a few https addresses; records what was asked."""

    def __init__(self, pages: dict[str, bytes], types: dict[str, str] | None = None):
        self.pages, self.types, self.asked = pages, types or {}, []

    def get(self, url, **kw):
        self.asked.append(url)
        assert kw.get("verify", True) is True  # TLS is always verified
        web = self

        class Response:
            def __init__(self):
                self.url = url
                self.headers = {"content-type": web.types.get(url, "application/octet-stream")}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                if url not in web.pages:
                    raise OSError(f"404 {url}")

            def iter_content(self, size):
                data = web.pages[url]
                for i in range(0, len(data), size):
                    yield data[i : i + size]

        return Response()


@pytest.fixture
def hls(tmp_path, clip):
    """The clip as a streamed video: a playlist and its parts, read from memory."""
    folder = tmp_path / "hls"
    folder.mkdir()
    source = folder / "clip.mp4"
    source.write_bytes(clip)
    subprocess.run(
        # H.264 as Strava streams it (MPEG-4 part 2 in TS lacks the size ffmpeg needs).
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-f", "hls",
         "-hls_time", "1", "-hls_list_size", "0", str(folder / "index.m3u8")],
        check=True,
    )  # fmt: skip
    base = "https://video.strava.example/v1/"
    pages = {base + f.name: f.read_bytes() for f in folder.glob("*.ts")}
    pages[base + "index.m3u8"] = (folder / "index.m3u8").read_bytes()
    master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow/index.m3u8\n"
    master += b"#EXT-X-STREAM-INF:BANDWIDTH=900\nindex.m3u8\n"
    pages["https://video.strava.example/master.m3u8"] = master.replace(
        b"index.m3u8\n", b"v1/index.m3u8\n", 2
    ).replace(b"low/v1/index.m3u8", b"low/index.m3u8")
    return base, pages


def test_only_https_on_the_playlists_host_or_strava_hosts():
    allowed = media_pull._host_allowed
    assert allowed("https://cdn.example.test/a.ts", "cdn.example.test")
    assert allowed("https://dgtzuqphqg23d.cloudfront.net/a.ts", "cdn.example.test")
    assert allowed("https://videos.strava.com/a.ts", None)
    assert not allowed("http://cdn.example.test/a.ts", "cdn.example.test")
    assert not allowed("https://evil.example/a.ts", "cdn.example.test")
    assert not allowed("https://strava.com.evil.example/a.ts", "cdn.example.test")
    assert not allowed("file:///etc/passwd", None)


@needs_ffmpeg
def test_a_streamed_video_is_downloaded_with_verified_tls_and_put_together(
    tmp_path, hls, monkeypatch
):
    import requests

    base, pages = hls
    web = FakeWeb(pages)
    monkeypatch.setattr(requests, "get", web.get)
    dest = tmp_path / "video.mp4"
    media_pull.download_video("https://video.strava.example/master.m3u8", dest, 5)
    assert video.probe(dest).duration_s and video.looks_like_video(dest.read_bytes()[:16])
    assert web.asked[1] == base + "index.m3u8"  # the best stream of the master playlist
    assert all(u.startswith("https://video.strava.example/") for u in web.asked)


@needs_ffmpeg
def test_a_part_of_a_stream_on_another_host_is_refused(tmp_path, hls, monkeypatch):
    import requests

    base, pages = hls
    playlist = pages[base + "index.m3u8"].decode()
    first = next(line for line in playlist.splitlines() if line.endswith(".ts"))
    pages[base + "index.m3u8"] = playlist.replace(
        first, f"https://evil.example/{first}", 1
    ).encode()
    web = FakeWeb(pages)
    monkeypatch.setattr(requests, "get", web.get)
    with pytest.raises(ValueError, match="somewhere else"):
        media_pull.download_video(base + "index.m3u8", tmp_path / "v.mp4", 5)
    assert not any("evil" in u for u in web.asked)


def test_a_video_download_has_one_deadline(tmp_path, monkeypatch):
    import requests

    web = FakeWeb({"https://cdn.example.test/v.mp4": b"\x00" * (3 * 1024 * 1024)})
    monkeypatch.setattr(requests, "get", web.get)
    with pytest.raises(TimeoutError):
        media_pull.download_video("https://cdn.example.test/v.mp4", tmp_path / "v", 5, 0)
    with pytest.raises(ValueError):
        media_pull.download_video("http://cdn.example.test/v.mp4", tmp_path / "w", 5)


@needs_ffmpeg
def test_remuxing_a_stream_reads_local_files_only(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\nhttps://evil.example/a.ts\n")
    with pytest.raises(video.NotAVideo):
        video.remux_hls(playlist, tmp_path / "out.mp4")


# --- the Strava archive ------------------------------------------------------------------


@needs_ffmpeg
def test_the_strava_archive_brings_the_videos(runtime, tmp_path, clip):
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow([*HEADER, "Media"])
    media = {"1001": "media/ride.mp4|media/again.mp4"}
    writer.writerows([*row, media.get(row[0], "")] for row in ROWS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export_2/activities.csv", table.getvalue())
        zf.writestr("export_2/media/ride.mp4", clip)
        zf.writestr("export_2/media/again.mp4", clip)  # the same file under another id
    path = tmp_path / "export_2.zip"
    path.write_bytes(buf.getvalue())
    result = {r.dataset: r for r in imports.import_strava_export(runtime, path)}["media"]
    assert (result.new_versions, result.videos, result.skipped) == (1, 1, 1)
    shown = workout_media.photos(runtime.engine, "strava_export:1001")["photos"]
    assert [p["kind"] for p in shown] == ["video"]
    again = {r.dataset: r for r in imports.import_strava_export(runtime, path)}["media"]
    assert again.new_versions == 0 and again.skipped == 2


# --- races and limits --------------------------------------------------------------------


@needs_ffmpeg
def test_the_same_video_added_at_once_is_kept_once(runtime, tmp_path, clip):
    run = _act(runtime, "1")
    paths = [_file(tmp_path, clip, f"c{n}.mp4") for n in range(4)]
    results, errors = [], []
    start = threading.Barrier(len(paths))

    def add(path, origin):
        try:
            start.wait()
            results.append(
                workout_media.add_video(
                    runtime.engine, runtime.raw.blobs, run, path, source="strava", origin=origin
                )
            )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    # Two of them are the same Strava video (one platform id), two come under other ids.
    origins = [("v1", "1"), ("v1", "1"), ("v2", "1"), ("v3", "1")]
    threads = [threading.Thread(target=add, args=a) for a in zip(paths, origins, strict=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    states = sorted(r["state"] for r in results)
    # One added; the others found it ("known" only if v1's twin came in after it finished).
    assert states[0] == "added" and set(states[1:]) <= {"same", "known"}
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(media_item)) == 1
        ids = {r.photo_id: r.media_id for r in conn.execute(select(media_origin))}
    assert set(ids) == {"v1", "v2", "v3"} and len(set(ids.values())) == 1


@needs_ffmpeg
def test_a_full_workout_is_checked_again_when_the_file_is_written(runtime, clip, monkeypatch):
    run = _act(runtime, "1")
    monkeypatch.setattr(workout_media, "MAX_PER_WORKOUT", 1)
    real = video.strip

    def slow_strip(src, dst):
        real(src, dst)
        # Meanwhile another request fills the workout.
        workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(1))

    monkeypatch.setattr(video, "strip", slow_strip)
    with pytest.raises(workout_media.TooMany):
        workout_media.add(runtime.engine, runtime.raw.blobs, run, clip)
    assert workout_media.count(runtime.engine, run) == 1


def test_a_photo_named_mp4_is_no_video(runtime, tmp_path):
    run = _act(runtime, "1")
    with pytest.raises(workout_media.NotAPhoto):
        workout_media.add_video(
            runtime.engine, runtime.raw.blobs, run, _file(tmp_path, picture(1), "x.mp4")
        )


def test_blobs_written_at_once_do_not_collide(tmp_path, monkeypatch):
    blobs = BlobStore(tmp_path / "blobs")
    big = tmp_path / "big.bin"
    big.write_bytes(bytes(range(256)) * 40_000)
    start = threading.Barrier(8)
    shas, errors = [], []

    def put():
        try:
            start.wait()
            shas.append(blobs.put_file(big))
            shas.append(blobs.put_plain_file(big, ".mp4"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(set(shas)) == 1
    assert blobs.get(shas[0]) == big.read_bytes()
    assert blobs.plain_path(shas[0], ".mp4").read_bytes() == big.read_bytes()
    assert not [p for p in (tmp_path / "blobs").rglob("*.tmp")]  # no leftovers
    # A move that fails because another writer just put the same blob there is fine.
    data = b"written by two"
    target = blobs.path_for(hashlib.sha256(data).hexdigest())
    real = os.replace

    def lost_race(src, dst):
        real(src, dst)
        raise OSError("busy")

    monkeypatch.setattr(os, "replace", lost_race)
    blobs.put(data)
    assert target.exists() and blobs.get(target.name.removesuffix(".zst")) == data


def test_an_upload_too_large_is_refused_before_it_is_read(runtime, monkeypatch):
    run = _act(runtime, "1")
    monkeypatch.setattr(workout_media, "MAX_UPLOAD_BYTES", 1000)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = client.post(
        "/api/activity-media/upload",
        params={"id": run},
        files=[("files", ("big.jpg", picture(1), "image/jpeg"))],
        headers=WRITE,
    )
    assert reply.status_code == 413 and reply.json()["code"] == "too_large"
    assert workout_media.count(runtime.engine, run) == 0


@needs_ffmpeg
def test_a_share_link_does_not_remake_a_video_in_its_own_request(runtime, tmp_path, clip):
    from vibesynergy.api.app import create_app
    from vibesynergy.portal import share
    from vibesynergy.runtime import Runtime

    host = "server.example.ts.net"
    rt = Runtime.open(replace(runtime.settings, server={"public_url": f"https://{host}"}))
    try:
        run = _act(rt, "1")
        media = workout_media.add(rt.engine, rt.raw.blobs, run, clip)["media"]
        shown = workout_media.photos(rt.engine, run)["photos"][0]
        assert shown["stream"] and len(shown["stream"]) == 16  # the ?v= of its address
        path = workout_media.video_file(rt.engine, rt.raw.blobs, media)
        path.unlink()
        code, _ = share.create(rt.engine, "Coach Anna", ["activities"], can_write=False)
        client = TestClient(create_app(rt, with_mcp=False), base_url=f"https://{host}")
        address = f"/vibesynergy/share/{code}/api/activity-media/video/{media}"
        busy = client.get(address)
        assert busy.status_code == 503 and busy.headers["retry-after"]
        for thread in [t for t in threading.enumerate() if t.name == "video-remux"]:
            thread.join(timeout=60)
        assert client.get(address).status_code == 200
    finally:
        rt.close()
