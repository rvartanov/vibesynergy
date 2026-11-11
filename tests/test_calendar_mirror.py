"""Guarded Garmin → CalDAV mirror. Synthetic calendars only; copying stays opt-in."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import insert

from vibesynergy.core.timeutil import utc_now
from vibesynergy.model import PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import calendar_mirror
from vibesynergy.query.calendar_mirror import Desired, reconcile
from vibesynergy.query.plan import plan
from vibesynergy.sources.caldav.calendar import parse_object
from vibesynergy.sources.caldav.client import CalDAVClient
from vibesynergy.sources.caldav.ics import href_for, render_event, uid_for
from vibesynergy.sources.caldav.writer import (
    CalendarWriter,
    MirrorTimeout,
    MirrorWriteError,
    PreconditionFailed,
)
from vibesynergy.store.schema import dataset_state
from vibesynergy.sync.scheduler import Scheduler

BASE = "https://calendar.example.test/account/workouts/"
DAY = date(2026, 10, 10)
STAMP = "20261005T120000Z"
KEY = "garmin:workout:17"
COVERED = date(2026, 12, 31)


class MemoryCalendar:
    def __init__(self, objects=None):
        self.objects = {item["url"]: dict(item) for item in objects or []}
        self.calls = []
        self.broken = False

    def snapshot(self, url):
        if self.broken:
            from vibesynergy.sources.caldav.writer import MirrorWriteError

            raise MirrorWriteError("calendar_read_failed")
        return [dict(item) for item in self.objects.values() if item["url"].startswith(url)]

    def get(self, href):
        item = self.objects.get(href)
        return dict(item) if item else None

    def put(self, href, body, *, match):
        self.calls.append(("PUT", href, match))
        current = self.objects.get(href)
        if match is None and current is not None:
            raise PreconditionFailed()
        if match is not None and (current is None or current["etag"] != match):
            raise PreconditionFailed()
        etag = f'"e{len(self.calls)}"'
        self.objects[href] = {"url": href, "etag": etag, "data": body.decode()}
        return etag

    def delete(self, href, match):
        self.calls.append(("DELETE", href, match))
        current = self.objects.get(href)
        if current is None or current["etag"] != match:
            raise PreconditionFailed()
        del self.objects[href]


def personal(name="personal.ics"):
    data = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:personal",
            "DTSTART;TZID=Europe/Amsterdam:20261010T090000",
            "DTEND;TZID=Europe/Amsterdam:20261010T100000",
            "SUMMARY:Coaching appointment",
            "RRULE:FREQ=WEEKLY;COUNT=3",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    return {"url": BASE + name, "etag": '"personal"', "data": data}


def mirror(key=KEY, title="Tempo", day=DAY, sequence=0, etag='"remote"'):
    body = render_event(key, day, title, sequence=sequence, stamp=STAMP).decode()
    return {"url": href_for(BASE, key), "etag": etag, "data": body}


def run(rt, calendar, desired, **kwargs):
    return reconcile(
        rt.engine,
        calendar,
        calendar_url=BASE,
        account_id=kwargs.pop("account_id", "acct"),
        today=date(2026, 10, 5),
        desired=desired,
        unkeyed_future=kwargs.pop("unkeyed_future", 0),
        garmin_complete=kwargs.pop("garmin_complete", True),
        covered_until=kwargs.pop("covered_until", COVERED),
        stamp=STAMP,
    )


def puts(calendar):
    return [call for call in calendar.calls if call[0] == "PUT"]


def deletes(calendar):
    return [call for call in calendar.calls if call[0] == "DELETE"]


def test_rendered_mirror_round_trips_through_the_legacy_proof():
    body = render_event(KEY, DAY, "Tempo, hills", sequence=2, stamp=STAMP)
    parsed, key = parse_object(
        {"url": href_for(BASE, KEY), "etag": '"1"', "data": body.decode()}, BASE
    )
    assert key == KEY
    event = parsed.walk("VEVENT")[0]
    assert "RRULE" not in event
    assert str(event["UID"]) == uid_for(KEY)


def test_create_is_conditional_and_a_second_run_does_not_duplicate(runtime):
    calendar = MemoryCalendar([personal()])
    wanted = [Desired(KEY, DAY, "Tempo")]
    first = run(runtime, calendar, wanted)
    assert first["created"] == 1 and first["deleted"] == 0
    assert puts(calendar) == [("PUT", href_for(BASE, KEY), None)]
    assert personal()["url"] in calendar.objects
    assert parse_object(calendar.objects[href_for(BASE, KEY)], BASE)[1] == KEY
    calendar.calls.clear()
    second = run(runtime, calendar, wanted)
    assert second["created"] == 0 and puts(calendar) == [] and deletes(calendar) == []
    assert calendar.objects[personal()["url"]]["etag"] == '"personal"'


def test_personal_and_recurring_events_are_never_written(runtime):
    calendar = MemoryCalendar([personal()])
    run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    run(runtime, calendar, [])
    assert all(call[1] != personal()["url"] for call in calendar.calls)
    assert calendar.objects[personal()["url"]]["data"].count("RRULE") == 1


def test_existing_mirror_is_adopted_before_it_is_changed_or_removed(runtime):
    calendar = MemoryCalendar([mirror(title="Old title")])
    wanted = [Desired(KEY, DAY, "New title")]
    first = run(runtime, calendar, wanted)
    assert first["updated"] == 0 and puts(calendar) == []
    second = run(runtime, calendar, wanted)
    assert second["updated"] == 1
    assert puts(calendar)[-1][2] == '"remote"'
    parsed, key = parse_object(calendar.objects[href_for(BASE, KEY)], BASE)
    assert key == KEY and str(parsed.walk("VEVENT")[0]["SUMMARY"]) == "New title"
    removed = run(runtime, calendar, [])
    assert removed["deleted"] == 1 and deletes(calendar)
    assert href_for(BASE, KEY) not in calendar.objects


def test_past_mirror_and_incomplete_garmin_read_are_not_deleted(runtime):
    future = mirror(key="garmin:workout:4", title="Later", day=date(2026, 10, 20))
    past = mirror(key="garmin:event:4", title="Done", day=date(2026, 10, 1))
    calendar = MemoryCalendar([future, past])
    run(runtime, calendar, [])  # record both, delete nothing yet
    assert run(runtime, calendar, [], garmin_complete=False)["deleted"] == 0
    assert run(runtime, calendar, [], unkeyed_future=1)["deleted"] == 0
    assert href_for(BASE, "garmin:workout:4") in calendar.objects
    gone = run(runtime, calendar, [])
    assert gone["deleted"] == 1
    assert href_for(BASE, "garmin:workout:4") not in calendar.objects
    assert href_for(BASE, "garmin:event:4") in calendar.objects


def test_future_mirror_without_a_baseline_is_not_deleted_in_the_same_run(runtime):
    calendar = MemoryCalendar([mirror(key="garmin:workout:99", day=date(2026, 10, 20))])
    assert run(runtime, calendar, [])["deleted"] == 0
    assert run(runtime, calendar, [])["deleted"] == 1


def test_a_failed_calendar_read_does_not_delete(runtime):
    calendar = MemoryCalendar([mirror()])
    run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    calendar.broken = True
    calendar.calls.clear()
    failed = run(runtime, calendar, [])
    assert failed["skipped"] == "calendar_read_failed"
    assert calendar.calls == []
    assert href_for(BASE, KEY) in calendar.objects


def test_unreadable_calendar_objects_are_ignored_instead_of_aborting(runtime):
    broken = {
        "url": BASE + "broken.ics",
        "etag": '"broken"',
        "data": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
    }
    calendar = MemoryCalendar([broken])
    result = run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    assert result["skipped"] is None
    assert result["ignored_unreadable"] == 1
    assert result["created"] == 1
    assert href_for(BASE, KEY) in calendar.objects


def test_an_external_edit_is_not_overwritten(runtime):
    calendar = MemoryCalendar()
    run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    obj = calendar.objects[href_for(BASE, KEY)]
    obj["data"] = obj["data"].replace("SUMMARY:Tempo", "SUMMARY:Kept by me")
    obj["etag"] = '"edited"'
    calendar.calls.clear()
    edited = run(runtime, calendar, [Desired(KEY, DAY, "Replacement")])
    assert edited["updated"] == 0 and puts(calendar) == []
    assert "Kept by me" in calendar.objects[href_for(BASE, KEY)]["data"]
    assert run(runtime, calendar, [])["deleted"] == 0


def test_reconcile_processes_large_changes_in_batches_instead_of_aborting(runtime):
    wanted = [Desired(f"garmin:workout:{i}", DAY, f"Workout {i}") for i in range(505)]
    calendar = MemoryCalendar()
    first = run(runtime, calendar, wanted)
    assert first["created"] == calendar_mirror.MAX_ACTIONS
    assert first["skipped"] == "action_batch_limited"
    assert first["batch_limited"] is True
    second = run(runtime, calendar, wanted)
    assert second["created"] == 5
    assert second["skipped"] is None
    assert second["batch_limited"] is False


def test_title_and_date_without_a_native_key_do_not_create(runtime):
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [PlannedItem("workout:17", DAY, "workout", title="Tempo")],
            source="garmin",
            raw_item_id=raw.id,
        )
    wanted, unkeyed = calendar_mirror.load_desired(runtime.engine, date(2026, 10, 5))
    assert wanted == [] and unkeyed == 1
    calendar = MemoryCalendar()
    assert run(runtime, calendar, wanted, unkeyed_future=unkeyed)["created"] == 0
    assert calendar.calls == []


def test_load_desired_ignores_garmin_history_outside_create_window(runtime):
    old = date(2024, 1, 1)
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "workout:old",
                    old,
                    "workout",
                    title="Old plan",
                    extra={"native_calendar_key": "garmin:workout:old"},
                )
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    wanted, _unkeyed = calendar_mirror.load_desired(runtime.engine, date(2026, 10, 5))
    assert wanted == []


def test_coach_copy_stays_on_the_calendar_and_not_on_garmin(runtime):
    key = "garmin:coach:9:11111111-1111-1111-1111-111111111111"
    calendar = MemoryCalendar()
    run(runtime, calendar, [Desired(key, DAY, "Coach")])
    assert puts(calendar)[0][1].startswith(BASE)
    assert all(call[1].startswith(BASE) for call in calendar.calls)


def test_ambiguous_timeout_does_not_create_a_second_object(runtime):
    class TimeoutAfterWrite(MemoryCalendar):
        def put(self, href, body, *, match):
            super().put(href, body, match=match)
            raise MirrorTimeout()

    calendar = TimeoutAfterWrite()
    first = run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    assert first["created"] == 0 and first["conflicts"] == 1
    calendar.calls.clear()
    run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    assert puts(calendar) == []
    assert len([item for item in calendar.objects if item.endswith(".ics")]) == 1


class FlakyCalendar(MemoryCalendar):
    """Times out on a conditional PUT or DELETE, optionally after the change was applied."""

    def __init__(self, objects=None, *, lands=False):
        super().__init__(objects)
        self.lands = lands
        self.fail = False

    def put(self, href, body, *, match):
        if self.fail and match is not None:
            if self.lands:
                super().put(href, body, match=match)
            raise MirrorTimeout()
        return super().put(href, body, match=match)

    def delete(self, href, match):
        if self.fail:
            if self.lands:
                super().delete(href, match)
            raise MirrorTimeout()
        super().delete(href, match)


class ReadbackFailureCalendar(MemoryCalendar):
    """Fails the first read-back GET right after a successful write."""

    def __init__(self, objects=None, *, readback_code: str):
        super().__init__(objects)
        self.readback_code = readback_code
        self._fail_readback = False

    def put(self, href, body, *, match):
        etag = super().put(href, body, match=match)
        self._fail_readback = True
        return etag

    def delete(self, href, match):
        super().delete(href, match)
        self._fail_readback = True

    def get(self, href):
        if self._fail_readback:
            self._fail_readback = False
            if self.readback_code == "timeout":
                raise MirrorTimeout()
            raise MirrorWriteError(self.readback_code)
        return super().get(href)


def _ledger(rt):
    return {row["origin_id"]: row for row in calendar_mirror._rows(rt.engine, BASE)}


def test_an_update_that_landed_before_a_timeout_is_verified_not_a_conflict(runtime):
    calendar = FlakyCalendar([mirror(title="Old")], lands=True)
    wanted = [Desired(KEY, DAY, "New")]
    run(runtime, calendar, wanted)  # adopt
    calendar.fail = True
    assert run(runtime, calendar, wanted)["conflicts"] == 1
    assert _ledger(runtime)[KEY]["state"] == "uncertain"
    calendar.fail = False
    calendar.calls.clear()
    run(runtime, calendar, wanted)
    row = _ledger(runtime)[KEY]
    assert row["state"] == "verified" and row["last_error"] is None
    assert puts(calendar) == [] and deletes(calendar) == []


def test_an_update_that_did_not_land_is_retried_with_the_current_etag(runtime):
    calendar = FlakyCalendar([mirror(title="Old")])
    wanted = [Desired(KEY, DAY, "New")]
    run(runtime, calendar, wanted)
    calendar.fail = True
    run(runtime, calendar, wanted)
    assert _ledger(runtime)[KEY]["state"] == "uncertain"
    calendar.fail = False
    calendar.calls.clear()
    assert run(runtime, calendar, wanted)["updated"] == 1
    assert puts(calendar) == [("PUT", href_for(BASE, KEY), '"remote"')]
    assert _ledger(runtime)[KEY]["state"] == "verified"


def test_an_uncertain_row_with_a_third_party_edit_stays_a_conflict(runtime):
    calendar = FlakyCalendar([mirror(title="Old")])
    wanted = [Desired(KEY, DAY, "New")]
    run(runtime, calendar, wanted)
    calendar.fail = True
    run(runtime, calendar, wanted)
    calendar.fail = False
    obj = calendar.objects[href_for(BASE, KEY)]
    obj["data"] = obj["data"].replace("SUMMARY:Old", "SUMMARY:Edited by me")
    obj["etag"] = '"edited"'
    calendar.calls.clear()
    run(runtime, calendar, wanted)
    assert puts(calendar) == [] and deletes(calendar) == []
    assert _ledger(runtime)[KEY]["state"] == "conflict"
    assert "Edited by me" in calendar.objects[href_for(BASE, KEY)]["data"]


def test_a_delete_that_landed_before_a_timeout_settles_as_absent(runtime):
    calendar = FlakyCalendar([mirror(day=date(2026, 10, 20))], lands=True)
    run(runtime, calendar, [])
    calendar.fail = True
    run(runtime, calendar, [])
    assert _ledger(runtime)[KEY]["state"] == "uncertain"
    assert href_for(BASE, KEY) not in calendar.objects
    calendar.fail = False
    calendar.calls.clear()
    run(runtime, calendar, [])
    assert _ledger(runtime)[KEY]["state"] == "absent" and calendar.calls == []
    assert calendar_mirror.summary(runtime.engine)["revisions"] == []


def test_a_delete_that_did_not_land_is_retried_while_the_content_is_unchanged(runtime):
    calendar = FlakyCalendar([mirror(day=date(2026, 10, 20))])
    run(runtime, calendar, [])
    calendar.fail = True
    run(runtime, calendar, [])
    calendar.fail = False
    assert run(runtime, calendar, [])["deleted"] == 1
    assert href_for(BASE, KEY) not in calendar.objects


@pytest.mark.parametrize("readback_code", ["http_503", "http_500", "timeout"])
def test_update_readback_failure_keeps_pending_intent_for_recovery(runtime, readback_code):
    calendar = ReadbackFailureCalendar([mirror(title="Old")], readback_code=readback_code)
    wanted = [Desired(KEY, DAY, "New")]
    run(runtime, calendar, wanted)
    result = run(runtime, calendar, wanted)
    assert result["updated"] == 0 and result["conflicts"] == 1
    row = _ledger(runtime)[KEY]
    assert row["state"] == "uncertain"
    assert row["pending_write"]["op"] == "update"
    assert row["last_error"] == f"readback_{readback_code}"
    calendar.calls.clear()
    run(runtime, calendar, wanted)
    row = _ledger(runtime)[KEY]
    assert row["state"] == "verified"
    assert row["pending_write"] is None
    assert puts(calendar) == [] and deletes(calendar) == []


@pytest.mark.parametrize("readback_code", ["http_503", "http_500", "timeout"])
def test_delete_readback_failure_keeps_pending_intent_for_recovery(runtime, readback_code):
    calendar = ReadbackFailureCalendar(
        [mirror(day=date(2026, 10, 20))], readback_code=readback_code
    )
    run(runtime, calendar, [])
    result = run(runtime, calendar, [])
    assert result["deleted"] == 0 and result["conflicts"] == 1
    row = _ledger(runtime)[KEY]
    assert row["state"] == "uncertain"
    assert row["pending_write"]["op"] == "delete"
    assert row["last_error"] == f"readback_{readback_code}"
    assert href_for(BASE, KEY) not in calendar.objects
    calendar.calls.clear()
    run(runtime, calendar, [])
    row = _ledger(runtime)[KEY]
    assert row["state"] == "absent"
    assert row["pending_write"] is None
    assert calendar.calls == []


def test_nothing_is_deleted_beyond_what_the_garmin_read_covered(runtime):
    far = date(2027, 3, 1)
    calendar = MemoryCalendar([mirror(day=far)])
    run(runtime, calendar, [])
    assert run(runtime, calendar, [], covered_until=date(2026, 12, 31))["deleted"] == 0
    assert run(runtime, calendar, [], covered_until=None)["deleted"] == 0
    assert href_for(BASE, KEY) in calendar.objects
    assert run(runtime, calendar, [], covered_until=date(2027, 3, 1))["deleted"] == 1


def test_a_ledger_row_from_another_account_is_adopted_again_before_any_write(runtime):
    calendar = MemoryCalendar([mirror(title="Old")])
    wanted = [Desired(KEY, DAY, "New")]
    run(runtime, calendar, wanted, account_id="other")
    assert _ledger(runtime)[KEY]["account_id"] == "other"
    calendar.calls.clear()
    first = run(runtime, calendar, wanted)
    assert first["updated"] == 0 and puts(calendar) == []
    assert _ledger(runtime)[KEY]["account_id"] == "acct"
    assert run(runtime, calendar, wanted)["updated"] == 1


def test_disabled_reconcile_is_not_scheduled_and_makes_no_request(runtime):
    assert calendar_mirror.due(runtime) is False
    assert calendar_mirror.run(runtime)["skipped"] == "disabled"
    queued = Scheduler(
        runtime.settings,
        runtime.registry,
        runtime.jobs,
        services={calendar_mirror.JOB: lambda: calendar_mirror.due(runtime)},
        clock=lambda: 0,
    ).run_once()
    assert calendar_mirror.JOB not in queued


def test_plan_exposes_ownership_freshness_and_revision_without_write_authority(runtime):
    calendar = MemoryCalendar()
    run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "workout:17",
                    DAY,
                    "workout",
                    title="Tempo",
                    extra={"native_calendar_key": KEY},
                )
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
        conn.execute(
            insert(dataset_state).values(
                source="garmin",
                dataset="scheduled_workouts",
                last_attempt_at=utc_now(),
                last_success_at=utc_now(),
                synced_from=date(2026, 10, 1),
                synced_until=date(2026, 10, 31),
            )
        )
    result = plan(runtime.engine, calendar_mirror_window(), date(2026, 10, 5))
    row = next(item for item in result["items"] if item["source"] == "garmin")
    assert row["ownership"] == "garmin"
    assert row["write_authority"] is False
    assert row["status"] == "upcoming"
    assert row["mirror_state"] == "verified" and row["mirror_revision"] >= 1
    assert {k: result["adherence"][k] for k in ("planned", "done")} == {"planned": 0, "done": 0}
    assert result["calendar_mirror"]["counts"]["verified"] == 1
    assert result["calendar_mirror"]["revisions"][0]["origin_id"] == KEY
    assert "etag" not in result["calendar_mirror"]["revisions"][0]
    assert "Tempo" not in str(result["calendar_mirror"])
    assert result["schedule_sync"]["datasets"][0]["dataset"] == "scheduled_workouts"


def calendar_mirror_window():
    from vibesynergy.core.timeutil import Window

    return Window(date(2026, 10, 1), date(2026, 11, 1))


def _response(status, headers=None):
    return type(
        "R",
        (),
        {
            "status_code": status,
            "headers": headers or {},
            "content": b"",
            "text": "",
        },
    )()


def test_writer_sends_etag_preconditions_and_not_the_password_in_the_url():
    class FakeHTTP:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "GET":
                return _response(404)
            if method == "PUT":
                return _response(201, headers={"ETag": '"1"'})
            return _response(204)

    fake = FakeHTTP()
    writer = CalendarWriter(CalDAVClient("synthetic-user", "synthetic-password", http=fake))
    href = href_for(BASE, KEY)
    assert writer.put(href, b"calendar", match=None) == '"1"'
    method, url, kwargs = fake.calls[0]
    assert method == "PUT" and url == href and "synthetic-password" not in url
    assert kwargs["headers"]["If-None-Match"] == "*"
    assert kwargs["auth"] == ("synthetic-user", "synthetic-password")
    writer.put(href, b"calendar", match='"1"')
    assert fake.calls[-1][2]["headers"]["If-Match"] == '"1"'
    with pytest.raises(PreconditionFailed):
        fake.calls.clear()

        def reject(method, url, **kwargs):
            return _response(412)

        fake.request = reject
        writer.put(href, b"calendar", match='"1"')


def test_stale_garmin_coverage_blocks_writes(runtime):
    stale = utc_now() - timedelta(days=3)
    with runtime.engine.begin() as conn:
        conn.execute(
            insert(dataset_state).values(
                source="garmin",
                dataset="scheduled_workouts",
                last_attempt_at=stale,
                last_success_at=stale,
                synced_from=date(2026, 10, 1),
                synced_until=date(2026, 10, 31),
            )
        )
    assert calendar_mirror.garmin_coverage(runtime.engine) is None


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_process_interruption_recovers_saved_intent_before_new_revision(runtime, operation):
    class InterruptedCalendar(MemoryCalendar):
        crash = False

        def put(self, href, body, *, match):
            result = super().put(href, body, match=match)
            if self.crash:
                raise SystemExit("synthetic process stop after PUT")
            return result

        def delete(self, href, match):
            super().delete(href, match)
            if self.crash:
                raise SystemExit("synthetic process stop after DELETE")

    calendar = InterruptedCalendar([] if operation == "create" else [mirror(title="Old")])
    wanted = [] if operation == "delete" else [Desired(KEY, DAY, "Version B")]
    if operation != "create":
        run(runtime, calendar, wanted)
    calendar.crash = True
    with pytest.raises(SystemExit):
        run(runtime, calendar, wanted)
    row = _ledger(runtime)[KEY]
    assert row["state"] in {"reserved", "uncertain"}
    assert row["pending_write"]["op"] == operation
    calendar.crash = False
    calendar.calls.clear()
    newer = [] if operation == "delete" else [Desired(KEY, DAY, "Version C")]
    run(runtime, calendar, newer)
    assert puts(calendar) == deletes(calendar) == []
    row = _ledger(runtime)[KEY]
    assert row["state"] == ("absent" if operation == "delete" else "verified")
    assert row["pending_write"] is None
    if operation != "delete":
        assert run(runtime, calendar, newer)["updated"] == 1
        assert "Version C" in calendar.objects[href_for(BASE, KEY)]["data"]


def test_timeout_recovery_uses_saved_revision_when_garmin_changes(runtime):
    calendar = FlakyCalendar([mirror(title="Old")], lands=True)
    run(runtime, calendar, [Desired(KEY, DAY, "Version B")])
    calendar.fail = True
    run(runtime, calendar, [Desired(KEY, DAY, "Version B")])
    calendar.fail = False
    run(runtime, calendar, [Desired(KEY, DAY, "Version C")])
    assert _ledger(runtime)[KEY]["state"] == "verified"
    assert run(runtime, calendar, [Desired(KEY, DAY, "Version C")])["updated"] == 1


@pytest.mark.parametrize("wanted, op", [([], "delete"), ([Desired(KEY, DAY, "New")], "update")])
def test_preview_projects_adoption_without_modifying_ledger(runtime, monkeypatch, wanted, op):
    calendar = MemoryCalendar([mirror(title="Old")])
    monkeypatch.setattr(calendar_mirror, "load_desired", lambda *_: (wanted, 0))
    monkeypatch.setattr(calendar_mirror, "garmin_coverage", lambda *_: COVERED)
    preview = calendar_mirror.preview(
        runtime.engine,
        calendar,
        calendar_url=BASE,
        account_id="acct",
        today=date(2026, 10, 5),
    )
    assert preview["counts"][op] == 1
    assert preview["total_actions"] == 1
    assert _ledger(runtime) == {}
    assert calendar.calls == []
    assert run(runtime, calendar, wanted)["adopted"] == 1
    assert run(runtime, calendar, wanted)[{"delete": "deleted", "update": "updated"}[op]] == 1


def test_preview_includes_external_edit_conflicts(runtime, monkeypatch):
    calendar = MemoryCalendar([mirror(title="Old")])
    wanted = [Desired(KEY, DAY, "New")]
    run(runtime, calendar, wanted)
    calendar.objects[href_for(BASE, KEY)] = mirror(title="Personal edit")
    monkeypatch.setattr(calendar_mirror, "load_desired", lambda *_: (wanted, 0))
    monkeypatch.setattr(calendar_mirror, "garmin_coverage", lambda *_: COVERED)
    result = calendar_mirror.preview(
        runtime.engine,
        calendar,
        calendar_url=BASE,
        account_id="acct",
        today=date(2026, 10, 5),
    )
    assert result["counts"]["conflict"] == 1
    assert result["counts"]["update"] == 0


def test_no_creates_or_updates_outside_recent_coverage(runtime):
    calendar = MemoryCalendar([mirror(title="Old")])
    wanted = [Desired(KEY, DAY, "New"), Desired("garmin:workout:18", DAY, "New")]
    run(runtime, calendar, wanted, covered_until=date(2026, 10, 9))
    result = run(runtime, calendar, wanted, covered_until=date(2026, 10, 9))
    assert result["created"] == result["updated"] == 0
