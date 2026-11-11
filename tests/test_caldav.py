"""Synthetic CalDAV snapshots: completeness, recurrence, identity and read-only semantics."""

import hashlib
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select

from vibesynergy.core.timeutil import Window
from vibesynergy.runtime import Runtime
from vibesynergy.sources.caldav.calendar import LEGACY_SOURCE, parse_object
from vibesynergy.sources.caldav.client import CalDAVClient
from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS, requests_timeout_pair
from vibesynergy.store.schema import planned_item

BASE = "https://calendar.example.test/account/workouts/"
WINDOW = Window(date(2026, 10, 1), date(2026, 11, 1))


def calendar(*events):
    return "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Synthetic//EN", *events, "END:VCALENDAR", ""]
    )


def event(uid="personal", start="20261010T090000", extra="", end="20261010T100000"):
    return "\r\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART;TZID=Europe/Amsterdam:{start}",
            f"DTEND;TZID=Europe/Amsterdam:{end}",
            "SUMMARY:Coaching appointment",
            extra,
            "END:VEVENT",
        ]
    )


def obj(data=None, name="personal.ics", etag='"r1"'):
    return {"url": BASE + name, "etag": etag, "data": data or calendar(event())}


def response(objects, status=207):
    rows = []
    for item in objects:
        rows.append(
            f"<d:response><d:href>{escape(item['url'])}</d:href><d:propstat><d:prop>"
            f"<d:getetag>{escape(item['etag'])}</d:getetag>"
            f"<c:calendar-data>{escape(item['data'])}</c:calendar-data></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    xml = (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(rows)
        + "</d:multistatus>"
    )
    return SimpleNamespace(status_code=status, content=xml.encode())


class FakeHTTP:
    def __init__(self):
        self.reply = response([obj()])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.reply


@pytest.fixture
def caldav_rt(runtime):
    settings = replace(
        runtime.settings,
        sources={"caldav": {"enabled": True, "calendars": [{"url": BASE, "name": "Workouts"}]}},
    )
    rt = Runtime.open(settings)
    fake = FakeHTTP()
    rt.registry.get("caldav")._client = CalDAVClient("example", "private", http=fake)
    yield rt, fake
    rt.close()


def items(rt):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(planned_item))]


def sync(rt):
    return rt.sync.sync("caldav", ["calendar"], WINDOW)[0]


def test_selected_calendar_is_read_only_and_archived(caldav_rt):
    rt, fake = caldav_rt
    result = sync(rt)
    assert result.error is None and result.normalise_errors == 0
    (item,) = items(rt)
    assert item["kind"] == "commitment" and item["sport"] is None
    assert item["extra"]["start"] == "2026-10-10T07:00:00+00:00"
    assert item["extra"]["counts_as_exercise"] is False
    assert item["extra"]["ownership"] == "personal"
    assert item["extra"]["etag"] == '"r1"'
    method, url, args = fake.calls[0]
    assert (method, url) == ("REPORT", BASE)
    assert args["allow_redirects"] is False
    assert args["timeout"] == requests_timeout_pair(DEFAULT_HTTP_TIMEOUT_SECONDS)
    assert rt.raw.load(next(rt.raw.current_ids("caldav", "calendar"))).json()["complete"] is True


def test_snapshot_revision_updates_and_removes_only_selected_context(caldav_rt):
    rt, fake = caldav_rt
    sync(rt)
    before = items(rt)[0]
    fake.reply = response(
        [obj(calendar(event(start="20261011T090000", end="20261011T100000")), etag='"r2"')]
    )
    assert sync(rt).normalise_errors == 0
    (after,) = items(rt)
    assert after["source_id"] == before["source_id"] and after["date"] == date(2026, 10, 11)
    fake.reply = response([])
    assert sync(rt).error is None and items(rt) == []
    assert len(list(rt.raw.all_ids("caldav", "calendar"))) == 3


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_failed_collection_preserves_previous_context(caldav_rt, status):
    rt, fake = caldav_rt
    sync(rt)
    before = items(rt)
    fake.reply = response([], status)
    assert sync(rt).error
    assert items(rt) == before


def test_partial_multistatus_is_not_empty_success(caldav_rt):
    rt, fake = caldav_rt
    sync(rt)
    fake.reply = response([obj()])
    fake.reply.content = fake.reply.content.replace(b"200 OK", b"404 Not Found")
    assert sync(rt).error and len(items(rt)) == 1


def test_duplicate_objects_with_same_etag_are_deduped():
    fake = FakeHTTP()
    fake.reply = response([obj(), obj()])
    found = CalDAVClient("user", "secret", http=fake).snapshot(BASE)
    assert len(found) == 1


def test_icloud_href_forms_and_outside_objects(caplog):
    base = "https://p01-caldav.icloud.com/12345/calendars/home/"
    fake = FakeHTTP()
    fake.reply = SimpleNamespace(
        status_code=207,
        content=(Path(__file__).parent / "fixtures/icloud_multistatus.xml").read_bytes(),
    )
    found = CalDAVClient("user", "secret", http=fake).snapshot(base)
    assert [o["url"] for o in found] == [base + n for n in ("A.ics", "B.ics", "C.ics")]
    assert "Skipped 2 CalDAV objects" in caplog.text
    assert len(fake.calls) == 1 and fake.calls[0][1] == base


@pytest.mark.parametrize("self_href", ["/12345/calendars/home/", "/12345/calendars/h%6Fme"])
def test_icloud_collection_response_is_silent(caplog, self_href):
    base = "https://p01-caldav.icloud.com/12345/calendars/home/"
    root = ET.fromstring((Path(__file__).parent / "fixtures/icloud_multistatus.xml").read_bytes())
    rows = root.findall("{DAV:}response")
    rows[0].find("{DAV:}href").text = self_href
    ET.SubElement(rows[0], "{DAV:}error")
    for row in rows[4:]:
        root.remove(row)
    fake = FakeHTTP()
    fake.reply = SimpleNamespace(status_code=207, content=ET.tostring(root), url=base)

    found = CalDAVClient("user", "secret", http=fake).snapshot(base)

    assert [o["url"] for o in found] == [base + n for n in ("A.ics", "B.ics", "C.ics")]
    assert not caplog.records


def test_snapshot_resolves_relative_href_against_effective_response_url():
    base = "https://calendar.example.test/account/workouts/"
    fake = FakeHTTP()
    fake.reply = response([{**obj(), "url": "../../workouts/personal.ics"}])
    fake.reply.url = "https://calendar.example.test/account/other/sub/"

    assert CalDAVClient("user", "secret", http=fake).snapshot(base)[0]["url"] == obj()["url"]


def test_recurrence_exception_cancellation_and_dst(caldav_rt):
    rt, fake = caldav_rt
    master = event(
        start="20261018T090000", end="20261018T100000", extra="RRULE:FREQ=WEEKLY;COUNT=3"
    )
    moved = event(
        start="20261026T110000",
        end="20261026T120000",
        extra="RECURRENCE-ID;TZID=Europe/Amsterdam:20261025T090000",
    )
    cancelled = event(
        start="20261101T090000",
        end="20261101T100000",
        extra="RECURRENCE-ID;TZID=Europe/Amsterdam:20261101T090000\r\nSTATUS:CANCELLED",
    )
    fake.reply = response([obj(calendar(master, moved, cancelled))])
    result = sync(rt)
    assert result.error is None and result.normalise_errors == 0
    rows = sorted(items(rt), key=lambda row: row["date"])
    assert [r["date"] for r in rows] == [date(2026, 10, 18), date(2026, 10, 26)]
    assert rows[0]["extra"]["start"] == "2026-10-18T07:00:00+00:00"
    assert rows[1]["extra"]["start"] == "2026-10-26T10:00:00+00:00"
    assert rows[1]["extra"]["recurrence_id"] == "2026-10-25T08:00:00+00:00"


def mirror(key="garmin:workout:17"):
    digest = hashlib.sha256(key.encode()).hexdigest()
    data = calendar(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                f"UID:{digest}@{LEGACY_SOURCE}",
                f"X-HERMES-SOURCE:{LEGACY_SOURCE}",
                f"X-HERMES-KEY:{key}",
                "DTSTAMP:20261001T000000Z",
                "SEQUENCE:0",
                "DTSTART;VALUE=DATE:20261010",
                "DTEND;VALUE=DATE:20261011",
                "SUMMARY:Synthetic Garmin mirror",
                "END:VEVENT",
            ]
        )
    )
    return obj(data, digest + ".ics")


def test_legacy_mirror_requires_full_proof(caldav_rt):
    rt, fake = caldav_rt
    fake.reply = response([mirror()])
    assert sync(rt).normalise_errors == 0
    (item,) = items(rt)
    assert item["kind"] == "mirror" and item["extra"]["mirror_key"] == "garmin:workout:17"
    assert item["extra"]["all_day"] is True
    for changed in (
        {**mirror(), "url": BASE + "copied.ics"},
        {**mirror(), "data": mirror()["data"].replace("garmin:workout:17", "garmin:workout:18")},
    ):
        with pytest.raises(ValueError):
            parse_object(changed, BASE)


def test_all_day_end_is_exclusive(caldav_rt):
    rt, fake = caldav_rt
    fake.reply = response(
        [
            obj(
                calendar(
                    "\r\n".join(
                        [
                            "BEGIN:VEVENT",
                            "UID:day",
                            "DTSTART;VALUE=DATE:20261005",
                            "DTEND;VALUE=DATE:20261007",
                            "SUMMARY:Two days",
                            "END:VEVENT",
                        ]
                    )
                )
            )
        ]
    )
    assert sync(rt).normalise_errors == 0
    (item,) = items(rt)
    assert item["extra"]["start"] == "2026-10-05" and item["extra"]["end"] == "2026-10-07"


def test_personal_commitments_never_count_as_completed_exercise(caldav_rt):
    from vibesynergy.query.plan import plan

    rt, _ = caldav_rt
    sync(rt)
    result = plan(rt.engine, WINDOW, date(2026, 10, 12))
    assert {k: result["adherence"][k] for k in ("planned", "done")} == {"planned": 0, "done": 0}
    assert result["items"][0]["status"] == "past"
    assert result["items"][0]["write_authority"] is False


def test_matching_mirror_is_hidden_and_linked_to_garmin(caldav_rt):
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query.plan import plan

    rt, fake = caldav_rt
    fake.reply = response([mirror()])
    sync(rt)
    raw = rt.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    original = PlannedItem(
        "workout:17", date(2026, 10, 10), "workout", title="Synthetic Garmin mirror"
    )
    with rt.engine.begin() as conn:
        write_records(conn, [original], source="garmin", raw_item_id=raw.id)
    (row,) = plan(rt.engine, WINDOW, date(2026, 10, 1))["items"]
    assert row["source"] == "garmin" and row["mirrors"][0]["schedule_matches"] is True
    assert row["mirrors"][0]["write_authority"] is False

    original = replace(original, extra={"native_calendar_key": "garmin:workout:17"})
    with rt.engine.begin() as conn:
        write_records(conn, [original], source="garmin", raw_item_id=raw.id)
    (row,) = plan(rt.engine, WINDOW, date(2026, 10, 1))["items"]
    assert row["source"] == "garmin" and len(row["mirrors"]) == 1


def test_unmatched_mirror_stays_visible_with_calendar_name(caldav_rt):
    from vibesynergy.query.plan import plan

    rt, fake = caldav_rt
    fake.reply = response([mirror("garmin:workout:99")])
    sync(rt)
    (row,) = plan(rt.engine, WINDOW, date(2026, 10, 1))["items"]
    assert row["kind"] == "mirror" and row["extra"]["calendar_name"] == "Workouts"


def test_ambiguous_title_date_mirror_is_not_deduped(caldav_rt):
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query.plan import plan

    rt, fake = caldav_rt
    fake.reply = response([mirror("garmin:workout:99")])
    sync(rt)
    raw = rt.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    title = "Synthetic Garmin mirror"
    for ident in ("a", "b"):
        with rt.engine.begin() as conn:
            write_records(
                conn,
                [PlannedItem(ident, date(2026, 10, 10), "workout", title=title)],
                source="garmin",
                raw_item_id=raw.id,
            )
    kinds = {row["kind"] for row in plan(rt.engine, WINDOW, date(2026, 10, 1))["items"]}
    assert kinds == {"mirror", "workout"}


def test_malformed_event_does_not_erase_existing_context(caldav_rt):
    rt, fake = caldav_rt
    sync(rt)
    fake.reply = response([obj("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR", etag='"r2"')])
    assert sync(rt).error and len(items(rt)) == 1


def test_outside_object_in_second_calendar_is_skipped(caldav_rt):
    rt, _ = caldav_rt
    sync(rt)
    source = rt.registry.get("caldav")
    source.ctx.config["calendars"].append({"url": "https://calendar.example.test/account/second/"})
    before = len(list(rt.raw.all_ids("caldav", "calendar")))
    assert sync(rt).error is None  # fake object belongs to the first collection
    assert len(items(rt)) == 1
    assert len(list(rt.raw.all_ids("caldav", "calendar"))) == before + 1


def test_ongoing_commitment_is_visible_and_deleted_when_removed(caldav_rt):
    from vibesynergy.query.plan import plan

    rt, fake = caldav_rt
    fake.reply = response([obj(calendar(event(start="20260928T090000", end="20261012T100000")))])
    assert sync(rt).normalise_errors == 0
    assert len(plan(rt.engine, WINDOW, WINDOW.start)["commitments"]) == 1
    fake.reply = response([])
    assert sync(rt).normalise_errors == 0
    assert items(rt) == []


def test_commitments_are_not_todays_training_and_can_be_excluded(caldav_rt):
    from vibesynergy.query.plan import plan

    rt, _ = caldav_rt
    sync(rt)
    result = plan(rt.engine, WINDOW, date(2026, 10, 10))
    assert result["today"] == [] and len(result["commitments"]) == 1
    assert plan(rt.engine, WINDOW, date(2026, 10, 10), include_calendar=False)["items"] == []


def test_calendar_api_exposes_agenda_and_validates_range(caldav_rt):
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    rt, _ = caldav_rt
    sync(rt)
    client = TestClient(create_portal_app(rt), base_url="http://localhost")
    reply = client.get("/api/calendar", params={"start": "2026-10-01", "end": "2026-11-01"})
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert len(data["agenda"]) == 1 and data["calendar_sync"]["problems"] == []
    assert (
        client.get("/api/calendar", params={"start": "2026-10-01", "end": "2026-09-01"}).status_code
        == 400
    )


def test_floating_time_uses_configured_timezone(caldav_rt):
    rt, fake = caldav_rt
    fake.reply = response([obj(calendar(event()).replace(";TZID=Europe/Amsterdam", ""))])
    assert sync(rt).normalise_errors == 0
    assert items(rt)[0]["extra"]["start"] == "2026-10-10T07:00:00+00:00"


class IncrementalHTTP:
    """Synthetic ETag inventory and multiget provider; no external requests."""

    def __init__(self, objects):
        self.objects = {o["url"]: o for o in objects}
        self.calls = []
        self.partial = False
        self.race = False
        self.inventory_404: set[str] = set()

    def request(self, method, url, **kwargs):
        body = kwargs["data"]
        self.calls.append(body)
        if b"calendar-multiget" in body:
            root = ET.fromstring(body)
            hrefs = [e.text for e in root.findall("{DAV:}href")]
            found = [self.objects[h] for h in hrefs if h in self.objects]
            if self.partial:
                found = found[:-1]
            reply = response(found)
            if self.race and found:
                self.objects[found[0]["url"]] = {**found[0], "etag": '"raced"'}
            return reply
        if b"calendar-data" not in body and self.inventory_404:
            rows = []
            for item in self.objects.values():
                if item["url"] in self.inventory_404:
                    rows.append(
                        f"<d:response><d:href>{escape(item['url'])}</d:href>"
                        "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
                    )
                else:
                    rows.append(
                        f"<d:response><d:href>{escape(item['url'])}</d:href><d:propstat><d:prop>"
                        f"<d:getetag>{escape(item['etag'])}</d:getetag></d:prop>"
                        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                    )
            xml = (
                '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                + "".join(rows)
                + "</d:multistatus>"
            )
            return SimpleNamespace(status_code=207, content=xml.encode())
        reply = response(list(self.objects.values()))
        if b"calendar-data" not in body:
            root = ET.fromstring(reply.content)
            for prop in root.iter("{DAV:}prop"):
                for data in list(prop):
                    if data.tag.endswith("calendar-data"):
                        prop.remove(data)
            reply.content = ET.tostring(root)
        return reply


def test_incremental_snapshot_fetches_only_changed_objects_and_reconciles_deletions():
    old = [obj(name="keep.ics"), obj(name="change.ics"), obj(name="gone.ics")]
    changed = obj(name="change.ics", etag='"r2"')
    new = obj(name="new.ics")
    fake = IncrementalHTTP([old[0], changed, new])
    client = CalDAVClient("synthetic", "synthetic", http=fake)
    result = client.incremental_snapshot(BASE, old)
    assert [(o["url"], o["etag"]) for o in result] == sorted(
        [(o["url"], o["etag"]) for o in [old[0], changed, new]]
    )
    assert all("BEGIN:VEVENT" in o["data"] for o in result)
    multiget = next(body for body in fake.calls if b"calendar-multiget" in body)
    assert b"keep.ics" not in multiget and b"gone.ics" not in multiget
    assert b"change.ics" in multiget and b"new.ics" in multiget
    fake.calls.clear()
    assert client.incremental_snapshot(BASE, result) == result
    assert len(fake.calls) == 1 and b"calendar-data" not in fake.calls[0]


@pytest.mark.parametrize("failure", ["partial", "race"])
def test_incomplete_incremental_read_falls_back_to_full_snapshot(caldav_rt, failure):
    rt, _ = caldav_rt
    fake = IncrementalHTTP([obj()])
    rt.registry.get("caldav")._client = CalDAVClient("synthetic", "synthetic", http=fake)
    assert sync(rt).error is None
    before = items(rt)
    fake.objects[BASE + "personal.ics"] = obj(etag='"r2"')
    setattr(fake, failure, True)
    assert sync(rt).error is None
    after = items(rt)
    assert after and len(after) == len(before)
    assert {i["source_id"] for i in after} == {i["source_id"] for i in before}


def test_inventory_404_does_not_shrink_archived_calendar(caldav_rt):
    rt, _ = caldav_rt
    second = obj(name="other.ics", data=calendar(event(uid="other")))
    fake = IncrementalHTTP([obj(), second])
    rt.registry.get("caldav")._client = CalDAVClient("synthetic", "synthetic", http=fake)
    assert sync(rt).error is None
    assert len(items(rt)) == 2
    fake.inventory_404 = {second["url"]}
    result = sync(rt)
    assert result.error is not None
    assert len(items(rt)) == 2


def test_multiget_skips_failed_response_and_duplicate_href():
    ok = obj(name="ok.ics")
    dup_a = obj(name="dup.ics", etag='"e1"')
    dup_b = {**dup_a, "etag": '"e2"', "data": calendar(event(uid="other"))}
    failed_row = (
        f"<d:response><d:href>{escape(dup_a['url'])}</d:href>"
        f"<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
    )
    ok_row = (
        f"<d:response><d:href>{escape(ok['url'])}</d:href><d:propstat><d:prop>"
        f"<d:getetag>{escape(ok['etag'])}</d:getetag>"
        f"<c:calendar-data>{escape(ok['data'])}</c:calendar-data></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )
    dup_rows = "".join(
        f"<d:response><d:href>{escape(dup_a['url'])}</d:href><d:propstat><d:prop>"
        f"<d:getetag>{escape(item['etag'])}</d:getetag>"
        f"<c:calendar-data>{escape(item['data'])}</c:calendar-data></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        for item in (dup_a, dup_b)
    )
    xml = (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + failed_row
        + ok_row
        + dup_rows
        + "</d:multistatus>"
    ).encode()
    fake = FakeHTTP()
    fake.reply = SimpleNamespace(status_code=207, content=xml, url=BASE)
    client = CalDAVClient("synthetic", "synthetic", http=fake)
    body = (
        '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        f"<d:href>{escape(ok['url'])}</d:href>"
        f"<d:href>{escape(dup_a['url'])}</d:href>"
        "</c:calendar-multiget>"
    ).encode()
    result = client._report(BASE, body, tolerate_non_2xx=True)
    urls = {row["url"] for row in result}
    assert ok["url"] in urls and dup_a["url"] in urls
    assert result[[r["url"] for r in result].index(dup_a["url"])]["etag"] == '"e2"'


def test_incremental_restart_uses_archive_and_periodic_full_reconciliation(caldav_rt, monkeypatch):
    from datetime import timedelta

    from vibesynergy.core.timeutil import utc_now

    rt, _ = caldav_rt
    fake = IncrementalHTTP([obj()])
    rt.registry.get("caldav")._client = CalDAVClient("synthetic", "synthetic", http=fake)
    assert sync(rt).error is None
    # New source instance: no process-local object cache is required.
    rt.registry._instances.clear()
    rt.registry.get("caldav")._client = CalDAVClient("synthetic", "synthetic", http=fake)
    fake.calls.clear()
    assert sync(rt).error is None
    assert len(fake.calls) == 1 and b"calendar-data" not in fake.calls[0]
    later = utc_now() + timedelta(days=2)
    monkeypatch.setattr("vibesynergy.sources.caldav.utc_now", lambda: later)
    fake.calls.clear()
    assert sync(rt).error is None
    assert b"calendar-data" in fake.calls[0]


def test_collection_classification_survives_full_and_incremental_sync(caldav_rt):
    from vibesynergy.core import config_edit
    from vibesynergy.query.plan import plan

    rt, _ = caldav_rt
    config_edit.apply_changes(
        rt.settings.config_path,
        {
            "sources": {
                "caldav": {
                    "enabled": True,
                    "calendars": [{"url": BASE, "name": "Synthetic", "classification": "coaching"}],
                }
            }
        },
    )
    assert sync(rt).error is None
    assert items(rt)[0]["extra"]["classification"] == "coaching"
    assert sync(rt).error is None
    item = plan(rt.engine, WINDOW, WINDOW.start)["items"][0]
    assert item["extra"]["classification"] == "coaching"
    assert item["extra"]["counts_as_exercise"] is False and not item["write_authority"]
