"""Photos of a workout in VibeSynergy (#114, phase 6): added in the portal, taken in from
Strava and Garmin, one photo once, removed only here, shown on share links.

Synthetic pictures and fake platforms only; nothing here reaches Strava or Garmin."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select
from test_evidence import put

from vibesynergy.core.errors import RateLimited
from vibesynergy.model import Activity
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import media_pull, workout_media
from vibesynergy.store.schema import media_check, media_item, media_origin

T = datetime(2026, 11, 20, 7, tzinfo=UTC)
WRITE = {"x-vibesynergy": "portal"}


def picture(seed: int = 0, size=(640, 480), fmt="JPEG", **save) -> bytes:
    """A synthetic photo: a gradient with shapes that differ per ``seed``."""
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    w, h = size
    for x in range(0, w, 4):
        draw.rectangle([x, 0, x + 4, h], fill=(x * 255 // w, (seed * 70) % 255, 120))
    for i in range(4):
        x = (seed * 97 + i * 151) % (w - 120)
        y = (seed * 53 + i * 89) % (h - 120)
        draw.ellipse(
            [x, y, x + 80 + i * 10, y + 60 + i * 15], fill=(250 - i * 40, 30 * i, seed * 40 % 255)
        )
    if fmt == "PNG" and save.pop("alpha", False):
        img = img.convert("RGBA")
    out = io.BytesIO()
    img.save(out, fmt, **save)
    return out.getvalue()


def with_location(data: bytes) -> bytes:
    """The same JPEG with EXIF: a GPS position and the camera turned (orientation 6)."""
    img = Image.open(io.BytesIO(data))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x8825] = {1: "N", 2: (53.0, 12.0, 0.0), 3: "E", 4: (5.0, 47.0, 0.0)}
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes(), quality=95)
    return out.getvalue()


def _act(rt, sid, source="strava", start=T):
    put(
        rt,
        source,
        Activity(
            sid,
            start,
            start.date(),
            "running",
            name=f"Run {sid}",
            distance_m=10_000.0,
            moving_s=3600,
            elapsed_s=3600,
        ),
    )
    return f"{source}:{sid}"


def _stored(rt, source, sid, photos):
    """The copy the normal sync stores, saying how many photos the activity has."""
    if source == "strava":
        payload = {"summary": {"id": int(sid)}, "detail": {"photos": {"count": photos}}}
    else:
        images = [{"imageId": f"x{i}"} for i in range(photos)]
        payload = {"summary": {}, "activity": {"metadataDTO": {"activityImages": images}}}
    rt.raw.put(
        source=source,
        dataset="activities",
        item_key=sid,
        payload=json.dumps(payload).encode(),
        content_type="application/json",
    )


class FakeReader:
    """A platform with photos: ``{activity id: [(photo id, bytes)]}``. Read calls only."""

    def __init__(self, photos):
        self.store = photos
        self.reads: list[str] = []
        self.downloads: list[str] = []
        self.limited = False

    def photos(self, source_id):
        if self.limited:
            raise RateLimited("budget spent")
        self.reads.append(source_id)
        return [
            {"photo_id": pid, "url": f"https://cdn.example.test/{pid}", "caption": None}
            for pid, _ in self.store.get(source_id, [])
        ]

    def download(self, url):
        self.downloads.append(url)
        pid = url.rsplit("/", 1)[1]
        for photos in self.store.values():
            for found, data in photos:
                if found == pid:
                    return data
        raise LookupError(url)


# --- reading a photo ---------------------------------------------------------------------


def test_a_photo_is_shown_upright_and_without_its_location():
    data = with_location(picture(1))
    assert Image.open(io.BytesIO(data)).getexif().get(0x8825)
    photo = workout_media.process(data)
    shown = Image.open(io.BytesIO(photo.display))
    assert shown.format == "JPEG" and not shown.getexif()  # no GPS, no camera data
    assert (photo.width, photo.height) == (480, 640)  # turned as the camera said
    thumb = Image.open(io.BytesIO(photo.thumb))
    assert max(thumb.size) == workout_media.THUMB_PX


def test_png_with_transparency_webp_and_gif_are_photos_too():
    for data in (
        picture(2, fmt="PNG", alpha=True),
        picture(3, fmt="WEBP"),
        picture(4, fmt="GIF"),
    ):
        assert Image.open(io.BytesIO(workout_media.process(data).display)).mode == "RGB"


@pytest.mark.parametrize("data", [b"", b"not a photo", b"%PDF-1.7 something"])
def test_what_is_not_a_photo_is_refused(data):
    with pytest.raises(workout_media.NotAPhoto):
        workout_media.process(data)


def test_too_large_is_refused(monkeypatch):
    monkeypatch.setattr(workout_media, "MAX_BYTES", 100)
    with pytest.raises(workout_media.TooLarge):
        workout_media.process(picture(1))


def test_the_same_picture_in_another_size_has_a_close_hash():
    first = workout_media.process(picture(5))
    smaller = workout_media.process(picture(5, size=(640, 480), quality=40))
    other = workout_media.process(picture(6))
    assert workout_media.distance(first.phash, smaller.phash) <= workout_media.SAME_PHOTO_BITS
    assert workout_media.distance(first.phash, other.phash) > workout_media.SAME_PHOTO_BITS


# --- a workout's photos ------------------------------------------------------------------


def test_add_remove_and_put_back_keep_every_change(runtime):
    run = _act(runtime, "1")
    blobs = runtime.raw.blobs
    added = workout_media.add(runtime.engine, blobs, run, picture(1))
    assert added["state"] == "added"
    again = workout_media.add(runtime.engine, blobs, run, picture(1))
    assert again == {"state": "same", "media": added["media"], "kind": "photo", "restored": False}
    workout_media.add(runtime.engine, blobs, run, picture(2))
    listed = workout_media.photos(runtime.engine, run)
    assert [p["source"] for p in listed["photos"]] == ["vs", "vs"]
    workout_media.remove(runtime.engine, run, added["media"])
    listed = workout_media.photos(runtime.engine, run)
    assert len(listed["photos"]) == 1 and listed["removed"][0]["id"] == added["media"]
    # Still there: removed only hides it.
    assert workout_media.file(runtime.engine, blobs, added["media"], "thumb")
    with pytest.raises(LookupError):
        workout_media.file(runtime.engine, blobs, added["media"], "thumb", shared=True)
    workout_media.restore(runtime.engine, run, added["media"])
    actions = [h["action"] for h in workout_media.photos(runtime.engine, run)["history"]]
    assert actions == ["restored", "removed", "added", "added"]
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(media_item.c.removed_at).limit(1)) is None


def test_adding_a_removed_photo_again_puts_it_back(runtime):
    run = _act(runtime, "1")
    first = workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(1))
    workout_media.remove(runtime.engine, run, first["media"])
    again = workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(1, quality=60))
    assert again["state"] == "same" and again["restored"]
    assert len(workout_media.photos(runtime.engine, run)["photos"]) == 1


def test_a_photo_of_another_workout_is_not_reachable_through_this_one(runtime):
    one, two = _act(runtime, "1"), _act(runtime, "2", start=T + timedelta(days=1))
    photo = workout_media.add(runtime.engine, runtime.raw.blobs, one, picture(1))
    with pytest.raises(LookupError):
        workout_media.remove(runtime.engine, two, photo["media"])


def test_a_workout_holds_a_limited_number_of_photos(runtime, monkeypatch):
    monkeypatch.setattr(workout_media, "MAX_PER_WORKOUT", 2)
    run = _act(runtime, "1")
    workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(1))
    workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(2))
    with pytest.raises(workout_media.TooMany):
        workout_media.add(runtime.engine, runtime.raw.blobs, run, picture(3))


# --- photos from Strava and Garmin -------------------------------------------------------


def test_photos_come_in_from_strava_and_garmin_once(runtime):
    strava = _act(runtime, "11")
    _act(runtime, "22", source="garmin")  # the same workout, recorded on the watch
    group = workout_media.photos(runtime.engine, strava)["group"]
    assert workout_media.photos(runtime.engine, "garmin:22")["group"] == group
    _stored(runtime, "strava", "11", 2)
    _stored(runtime, "garmin", "22", 1)
    readers = {
        "strava": FakeReader({"11": [("s-a", picture(1)), ("s-b", picture(2))]}),
        # Garmin's copy of the first photo: same picture, smaller and recompressed.
        "garmin": FakeReader({"22": [("g-a", picture(1, quality=50))]}),
    }
    summary = media_pull.run(runtime, readers=readers, now=T)
    assert summary["added"] == 2 and summary["same"] == 1 and summary["read"] == 2
    photos = workout_media.photos(runtime.engine, strava)["photos"]
    assert [p["places"] for p in photos] == [["strava", "garmin"], ["strava"]]
    # Nothing changed on the platforms' stored copies: they are not read again.
    again = media_pull.run(runtime, readers=readers, now=T + timedelta(hours=1))
    assert again["read"] == 0 and readers["strava"].reads == ["11"]


def test_a_removed_platform_photo_never_comes_back(runtime):
    run = _act(runtime, "11")
    _stored(runtime, "strava", "11", 1)
    reader = FakeReader({"11": [("s-a", picture(1))]})
    media_pull.run(runtime, readers={"strava": reader}, now=T)
    photo = workout_media.photos(runtime.engine, run)["photos"][0]
    workout_media.remove(runtime.engine, run, photo["id"])
    # A new version of the copy (Strava updated it): looked at again, photo not taken again.
    _stored(runtime, "strava", "11", 2)
    reader.store["11"].append(("s-b", picture(2)))
    media_pull.run(runtime, readers={"strava": reader}, now=T + timedelta(hours=1))
    listed = workout_media.photos(runtime.engine, run)
    assert len(listed["photos"]) == 1 and len(listed["removed"]) == 1
    assert reader.downloads.count("https://cdn.example.test/s-a") == 1


def test_a_copy_without_photos_costs_no_call(runtime):
    _act(runtime, "11")
    _stored(runtime, "strava", "11", 0)
    reader = FakeReader({})
    summary = media_pull.run(runtime, readers={"strava": reader}, now=T)
    assert summary["looked"] == 1 and summary["read"] == 0 and reader.reads == []


def test_strava_rate_limit_ends_the_run_and_the_rest_waits(runtime):
    _act(runtime, "11")
    _act(runtime, "12", start=T + timedelta(days=1))
    _stored(runtime, "strava", "11", 1)
    _stored(runtime, "strava", "12", 1)
    reader = FakeReader({"11": [("a", picture(1))], "12": [("b", picture(2))]})
    reader.limited = True
    assert media_pull.run(runtime, readers={"strava": reader}, now=T)["rate_limited"]
    reader.limited = False
    summary = media_pull.run(runtime, readers={"strava": reader}, now=T + timedelta(minutes=30))
    assert summary["added"] == 2


def test_a_photo_that_is_not_one_is_skipped_for_good(runtime):
    _act(runtime, "11")
    _stored(runtime, "strava", "11", 1)
    reader = FakeReader({"11": [("v", b"<html>not a photo</html>")]})
    media_pull.run(runtime, readers={"strava": reader}, now=T)
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(media_origin.c.media_id)) == ""
        assert conn.scalar(select(media_check.c.next_at)) is None


def test_a_photo_still_being_processed_is_looked_at_once_more(runtime):
    run = _act(runtime, "11")
    _stored(runtime, "strava", "11", 2)
    reader = FakeReader({"11": [("a", picture(1))]})  # the second is not listed yet
    media_pull.run(runtime, readers={"strava": reader}, now=T)
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(media_check.c.next_at)) is not None
    reader.store["11"].append(("b", picture(2)))
    assert (
        media_pull.run(runtime, readers={"strava": reader}, now=T + timedelta(minutes=10))["read"]
        == 0
    )  # not before the wait
    media_pull.run(runtime, readers={"strava": reader}, now=T + media_pull.RETRY)
    assert len(workout_media.photos(runtime.engine, run)["photos"]) == 2


def test_photo_counts_come_from_the_stored_copies():
    assert media_pull.photo_count("strava", {"detail": {"photos": {"count": 3}}}) == 3
    assert media_pull.photo_count("strava", {"summary": {"total_photo_count": 2}}) == 2
    garmin = {"activity": {"metadataDTO": {"activityImages": [{}, {}]}}}
    assert media_pull.photo_count("garmin", garmin) == 2
    assert media_pull.photo_count("garmin", {"activity": None}) == 0


def test_the_strava_reader_lists_photos_and_videos_and_skips_placeholders():
    reader = media_pull.StravaReader.__new__(media_pull.StravaReader)

    class Client:
        def get(self, path, **params):
            assert path == "/activities/11/photos" and params["size"] == 2048
            return [
                {"unique_id": "p1", "urls": {"100": "https://x/s", "2048": "https://x/l"}},
                {"unique_id": "v1", "urls": {"2048": "https://x/v"}, "video_url": "https://x/m"},
                {"unique_id": "p2", "urls": {"2048": "https://x/placeholder-photo.png"}},
            ]

    reader.client = Client()
    listed = reader.photos("11")
    assert [(p["photo_id"], p["url"], p["video"]) for p in listed] == [
        ("p1", "https://x/l", False),
        ("v1", "https://x/m", True),  # the video itself, not its poster
    ]


def test_a_photo_link_must_be_https():
    with pytest.raises(ValueError):
        media_pull.download("http://example.test/photo.jpg", 5)


def test_the_strava_token_follows_photos_added_later():
    from vibesynergy.sources.strava.source import revision

    plain = {"id": 1, "name": "Run"}
    assert revision(plain) == revision({**plain, "total_photo_count": 0})
    assert revision(plain) != revision({**plain, "total_photo_count": 1})


# --- the portal --------------------------------------------------------------------------


def test_add_photos_on_the_workout_page(runtime):
    run = _act(runtime, "1")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    files = [
        ("files", ("one.jpg", picture(1), "image/jpeg")),
        ("files", ("again.jpg", picture(1), "image/jpeg")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ]
    reply = client.post(
        "/api/activity-media/upload", params={"id": run}, files=files, headers=WRITE
    )
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert [r["state"] for r in data["results"]] == ["added", "same", "refused"]
    assert data["results"][2]["code"] == "not_a_photo"
    media = data["photos"][0]["id"]
    photo = client.get(f"/api/activity-media/photo/{media}", params={"size": "thumb"})
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert (
        client.get(f"/api/activity-media/photo/{media}", params={"size": "original"}).status_code
        == 400
    )
    removed = client.post(
        "/api/activity-media/remove", json={"id": run, "media": media}, headers=WRITE
    )
    assert removed.json()["data"]["photos"] == []
    page = client.get("/api/activity-media", params={"id": run}).json()["data"]
    assert page["removed"][0]["id"] == media and page["history"]
    back = client.post(
        "/api/activity-media/restore", json={"id": run, "media": media}, headers=WRITE
    )
    assert len(back.json()["data"]["photos"]) == 1
    # Writes need the portal's header.
    assert (
        client.post("/api/activity-media/remove", json={"id": run, "media": media}).status_code
        == 403
    )


def test_a_share_link_shows_the_photos_but_cannot_change_them(runtime):
    from vibesynergy.api.app import create_app
    from vibesynergy.portal import share
    from vibesynergy.runtime import Runtime

    host = "server.example.ts.net"
    rt = Runtime.open(replace(runtime.settings, server={"public_url": f"https://{host}"}))
    try:
        run = _act(rt, "1")
        shown = workout_media.add(rt.engine, rt.raw.blobs, run, picture(1))["media"]
        hidden = workout_media.add(rt.engine, rt.raw.blobs, run, picture(2))["media"]
        workout_media.remove(rt.engine, run, hidden)
        code, _ = share.create(rt.engine, "Coach Anna", ["activities"], can_write=True)
        client = TestClient(create_app(rt, with_mcp=False), base_url=f"https://{host}")
        base = f"/vibesynergy/share/{code}/api"
        data = client.get(f"{base}/activity-media", params={"id": run}).json()["data"]
        assert [p["id"] for p in data["photos"]] == [shown]
        assert "removed" not in data and "history" not in data and "from" not in data
        assert client.get(f"{base}/activity-media/photo/{shown}").status_code == 200
        assert client.get(f"{base}/activity-media/photo/{hidden}").status_code == 404
        write = {"x-vibesynergy": "portal", "origin": f"https://{host}"}
        body = {"id": run, "media": shown}
        for path in ("activity-media/remove", "activity-media/restore"):
            assert client.post(f"{base}/{path}", json=body, headers=write).status_code == 403
        upload = client.post(
            f"{base}/activity-media/upload",
            params={"id": run},
            files=[("files", ("x.jpg", picture(3), "image/jpeg"))],
            headers=write,
        )
        assert upload.status_code == 403
    finally:
        rt.close()
