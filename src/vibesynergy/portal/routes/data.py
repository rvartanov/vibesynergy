"""Portal API routes: data (split from portal/api.py). Order matters: api.py
registers these modules in sequence, so routes match exactly as before."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from vibesynergy import dropfolder, history, imports, uploads
from vibesynergy.i18n import translator
from vibesynergy.portal.api import (
    WRONG_ARCHIVE,
    CheckinRequest,
    InjuryRequest,
    NoteRequest,
    PortalKit,
    UploadFinish,
    _date,
)
from vibesynergy.query import (
    journal,
    periods,
    views,
)
from vibesynergy.query import series as qs


def register(kit: PortalKit) -> None:
    router = kit.router
    rt = kit.rt
    engine = kit.engine
    settings = kit.settings
    today = kit.today
    envelope = kit.envelope
    diary_changed = kit.diary_changed

    @router.post("/history/uploads/{upload_id}")
    async def history_upload_chunk(upload_id: str, offset: int, request: Request) -> Any:
        """One chunk, as the request body, at ``offset``; a mismatch answers 409 with where
        the upload really is."""
        try:
            return envelope(await uploads.append(settings, upload_id, offset, request.stream()))
        except uploads.OffsetMismatch as exc:
            return JSONResponse({"error": str(exc), "received": exc.received}, status_code=409)

    @router.post("/history/uploads/{upload_id}/finish")
    def history_upload_finish(
        upload_id: str, request: UploadFinish | None = None, lang: str | None = None
    ) -> dict[str, Any]:
        """The upload is complete: check it's an account export (the one the page asked for,
        when it names one), then import it."""
        path = uploads.finish(settings, upload_id)
        kind = imports.archive_kind(path)
        expect = request.expect if request else None
        if kind is None or (expect and kind != expect):
            dropfolder.delete_archive(settings, path)
            if expect:
                raise ValueError(translator(lang)(WRONG_ARCHIVE[expect]))
            raise ValueError(
                f"{path.name} isn't a Garmin or Strava account export; activity files go "
                "under Import files"
            )
        return envelope(history.start_archive(rt, path, kind))

    @router.post("/history/uploads/{upload_id}/cancel")
    def history_upload_cancel(upload_id: str) -> dict[str, Any]:
        uploads.cancel(settings, upload_id)
        return envelope({"cancelled": upload_id})

    # After the upload routes: /history/uploads/<id> must not be taken for a run id.
    @router.post("/history/{fill_id}/{action}")
    def history_act(fill_id: int, action: str) -> dict[str, Any]:
        """pause | resume | cancel | retry (fetch the partial months again)."""
        return envelope(history.act(rt, fill_id, action))

    # Diary: notes, check-in, injuries.

    @router.get("/injuries")
    def injuries_page(lang: str = "en") -> dict[str, Any]:
        """Health → Complaints & injuries: open ones first with their pain log and
        warnings, then resolved ones; the same as vibesynergy_injuries."""
        return envelope(
            {
                "injuries": journal.injuries(
                    engine, today(), include_resolved=True, days=120, tr=translator(lang)
                )
            }
        )

    @router.get("/diary")
    def diary(
        period: str = "last_28d",
        start: str | None = None,
        end: str | None = None,
        lang: str = "en",
    ) -> dict[str, Any]:
        """The Diary page: the whole day, not just what you wrote down. Notes and check-ins in
        the period, the workouts that happened (linking back to each activity), injuries (open
        ones, and those resolved in the period) with their pain log, and daily training load
        and time to show pain next to what was done."""
        tdy = today()
        if start and end and start > end:
            raise ValueError("Choose a valid date range (start on or before end)")
        span = periods.resolve(period, start, end, today=tdy, default="last_28d")
        items = [
            i
            for i in journal.injuries(
                engine, tdy, include_resolved=True, days=len(span), tr=translator(lang)
            )
            if i["status"] != "resolved" or (i["resolved_on"] or "") >= str(span.start)
        ]
        load = qs.series(
            engine,
            ["activity.load", "activity.duration"],
            span,
            granularity="day",
            max_points=10_000,
        )
        events = views.activities(engine, span, sort="date", limit=5000)["activities"]
        return envelope(
            {
                "notes": journal.notes(engine, span),
                "checkins": journal.checkins(engine, span),
                "events": events,
                "injuries": items,
                "load": {"columns": load["columns"], "rows": load["rows"]},
                "choices": {
                    "tags": journal.SUGGESTED_TAGS,
                    "checkin": journal.CHECKIN_FIELDS,
                    "body_parts": journal.BODY_PARTS,
                    "sides": journal.SIDES,
                    "kinds": journal.KINDS,
                    "statuses": journal.STATUSES,
                    "timings": journal.TIMINGS,
                },
                "note": journal.NOT_A_DIAGNOSIS,
            },
            period={"start": str(span.start), "end": str(span.end)},
        )

    @router.post("/diary/note")
    def diary_note(request: NoteRequest) -> dict[str, Any]:
        """Add a note; or, with ``id``, change or delete one. When switched on, the day's
        notes are then copied to intervals.icu, only if they stay private there."""
        from vibesynergy.query import icu_notes

        days: set[date] = set()
        if request.id:
            days.add(date.fromisoformat(journal.get_note(engine, request.id)["start_date"]))
        reply = _diary_note(request)
        saved = reply["data"]
        if isinstance(saved, dict) and saved.get("start_date"):
            days.add(date.fromisoformat(saved["start_date"]))
        icu_notes.schedule(engine, settings.data_dir, days)
        return reply

    def _diary_note(request: NoteRequest) -> dict[str, Any]:
        if request.id:
            if request.delete:
                journal.delete_note(engine, request.id)
                return diary_changed({"deleted": request.id})
            return diary_changed(
                journal.edit_note(
                    engine,
                    request.id,
                    text=request.text,
                    tags=request.tags,
                    start=_date(request.date) if request.date else None,
                    end=_date(request.end) if request.end else None,
                )
            )
        if request.text is None:
            raise ValueError("text is empty")
        group = views.resolve_activity(engine, request.activity_id) if request.activity_id else None
        return diary_changed(
            journal.add_note(
                engine,
                text=request.text,
                start=_date(request.date) if request.date else today(),
                end=_date(request.end) if request.end else None,
                tags=request.tags,
                activity_id=group,
                source="portal",
            )
        )

    @router.post("/diary/checkin")
    def diary_checkin(request: CheckinRequest) -> dict[str, Any]:
        when = _date(request.date) if request.date else today()
        if when > today():
            raise ValueError("the date is in the future")
        scores = request.model_dump(exclude={"date"})
        return diary_changed(journal.set_checkin(engine, when, **scores))

    @router.post("/diary/injury")
    def diary_injury(request: InjuryRequest) -> dict[str, Any]:
        return diary_changed(
            journal.log_injury(
                engine,
                today=today(),
                injury_id=request.injury_id,
                body_part=request.body_part,
                side=request.side,
                kind=request.kind,
                title=request.title,
                when=_date(request.date) if request.date else None,
                pain=request.pain,
                timing=request.timing,
                affects_movement=request.affects_movement,
                note_text=request.note,
                status=request.status,
                notes_text=request.notes,
            )
        )
