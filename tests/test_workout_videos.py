"""Videos of a workout (#114): kept as they came, played from a copy without the file's
metadata (so not where it was filmed), with a poster frame, one video once, from the portal,
Strava and the Strava archive.

Synthetic videos made with ffmpeg in the test; the tests skip without it."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_strava_export import HEADER, ROWS
from test_workout_media import WRITE, FakeReader, _act, _stored, picture

from vibesynergy import imports
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import media_pull, video, workout_media
from vibesynergy.store.schema import media_item

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


def test_a_strava_video_without_ffmpeg_waits_for_the_next_look(runtime, monkeypatch):
    _act(runtime, "11")
    monkeypatch.setattr(video.shutil, "which", lambda name: None)
    fake = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    reader = VideoReader({}, {"v1": fake})
    result = media_pull.pull_one(runtime, reader, "strava", "11")
    assert result["added"] == 0 and "NoVideoTool" in result["errors"][0]
    assert not workout_media.known(runtime.engine, "strava", "v1")  # tried again later


def test_a_streamed_strava_video_is_fetched_with_ffmpeg(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(video, "fetch_hls", lambda url, dest, limit: calls.append(url) or
                        dest.write_bytes(b"x"))  # fmt: skip
    media_pull.download_video("https://cdn.example.test/v/index.m3u8", tmp_path / "v", 5)
    assert calls == ["https://cdn.example.test/v/index.m3u8"]
    with pytest.raises(ValueError):
        media_pull.download_video("http://cdn.example.test/v.mp4", tmp_path / "w", 5)


@needs_ffmpeg
def test_hls_reads_only_over_https(tmp_path):
    playlist = tmp_path / "local.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-ENDLIST\n")
    with pytest.raises(video.NotAVideo):
        video.fetch_hls(playlist.as_uri(), tmp_path / "out.mp4", 10_000)


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
