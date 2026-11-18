"""Coach → Settings → Coach via API, and replies to coach reports.

The API key is write-only: the page sends it once and only ever gets ``api_key_set`` back.
Errors are plain English sentences the page translates; they never include the key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from vibesynergy.query import coach_replies
from vibesynergy.query import coach_runner as runner_q
from vibesynergy.runtime import Runtime


class RunnerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    monthly_cap_usd: float | None = None
    schedule: dict[str, dict[str, Any]] | None = None
    models: dict[str, str] | None = None
    tasks: dict[str, dict[str, str | bool]] | None = None  # bool: an event task's "on"
    local_url: str | None = None
    advice: dict[str, int] | None = None  # {"max_runs": automatic updates per day}


class TestRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: str


class ApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    provider: str = "anthropic"


class KeyClear(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "anthropic"


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class NewReply(ReplyBody):
    entry_id: int


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def build_coach_runner_router(rt: Runtime) -> APIRouter:
    router = APIRouter()

    @router.get("/coach-runner")
    def runner_overview() -> dict[str, Any]:
        return {"data": runner_q.overview(rt)}

    @router.post("/coach-runner/settings")
    def runner_settings(body: RunnerChange) -> Any:
        try:
            return {"data": runner_q.save_settings(rt, body.model_dump(exclude_none=True))}
        except runner_q.SettingsError as exc:
            return _bad(str(exc))

    @router.post("/coach-runner/test")
    def runner_test(body: TestRun) -> Any:
        try:
            return {"data": runner_q.test_run(rt, body.job)}
        except runner_q.TestRunError as exc:
            return _bad(str(exc))

    @router.post("/coach-runner/resend")
    def runner_resend(body: TestRun) -> Any:
        try:
            return {"data": runner_q.resend(rt, body.job)}
        except runner_q.TestRunError as exc:
            return _bad(str(exc))

    @router.post("/coach-runner/key")
    def runner_key(body: ApiKey) -> Any:
        try:
            return {"data": runner_q.set_key(rt, body.value, body.provider)}
        except runner_q.SettingsError as exc:
            return _bad(str(exc))

    @router.post("/coach-runner/key/clear")
    def runner_key_clear(body: KeyClear | None = None) -> dict[str, Any]:
        return {"data": runner_q.clear_key(rt, (body or KeyClear()).provider)}

    @router.get("/advice")
    def current_advice(date: str | None = None, lang: str = "en") -> Any:
        """The day's current advice (#89): the same as the dashboard, the coach page,
        vibesynergy_today and the mail. Default today."""
        from datetime import date as _date

        from vibesynergy.i18n import translator
        from vibesynergy.query import daily_advice

        try:
            day = _date.fromisoformat(date) if date else None
        except ValueError:
            return _bad("Not a date.")
        try:
            return {"data": daily_advice.current(rt, day, tr=translator(lang))}
        except RuntimeError:
            return _bad("The advice could not be computed.", 503)

    @router.post("/advice/refresh")
    def advice_refresh() -> Any:
        """Sync every source, then one AI advice run (the dashboard's Update advice)."""
        from vibesynergy.query import daily_advice

        try:
            return {"data": daily_advice.refresh(rt)}
        except runner_q.TestRunError as exc:
            return _bad(str(exc))

    @router.get("/advice/refresh")
    def advice_refresh_status(key: str, jobs: str = "") -> Any:
        from vibesynergy.query import daily_advice

        try:
            ids = [int(i) for i in jobs.split(",") if i.strip()]
            return {"data": daily_advice.refresh_status(rt, key, ids)}
        except ValueError:
            return _bad("Unknown refresh.")

    @router.get("/coach/report/{entry_id}")
    def coach_report(entry_id: int) -> Any:
        try:
            return {"data": runner_q.report(rt, entry_id)}
        except LookupError:
            return _bad("This report no longer exists.", 404)

    @router.post("/coach/replies")
    def add_reply(body: NewReply) -> Any:
        try:
            return {"data": coach_replies.add(rt.engine, body.entry_id, body.text)}
        except LookupError:
            return _bad("This report no longer exists.", 404)
        except coach_replies.ReplyError as exc:
            return _bad(str(exc))

    @router.post("/coach/replies/{reply_id}")
    def edit_reply(reply_id: int, body: ReplyBody) -> Any:
        try:
            return {"data": coach_replies.edit(rt.engine, reply_id, body.text)}
        except LookupError:
            return _bad("This reply no longer exists.", 404)
        except coach_replies.ReplyError as exc:
            return _bad(str(exc))

    return router
