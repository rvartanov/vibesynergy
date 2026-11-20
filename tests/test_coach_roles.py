"""More than one AI coach (#118): a role per connection, write rights and approval enforced
on the server, entry keys per author, and the advisory AI coach's second opinion."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select, update

from vibesynergy.coach_runner import context, registry
from vibesynergy.coach_runner.tools import COACH_SCOPES as RUNNER_SCOPES
from vibesynergy.coach_runner.tools import ToolBox
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.mcp import roles
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import (
    coach_replies,
    coach_tasks,
    daily_advice,
    inbox,
    plan_model,
    plan_proposals,
)
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import (
    coach_decision_log,
    coach_reply,
    plan_proposal,
    plan_session,
)

HOST = "https://server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": HOST}
OWN_AGENT, ADVISOR = "oa-own", "oa-advisor"
FULL = frozenset({"read", "coach.write", "diary.write", "plan.propose", "plan.write", "publish"})
SUGGEST = frozenset({"read", "coach.write", "plan.propose"})
RIDE = "Warmup\n- 10m 50-60%\n\nMain set\n- 40m 65-75%\n\nCooldown\n- 10m 50-60%"
OK = {"level": "ok", "colour": "green", "reasons": ["Goed hersteld."]}
EASE = {"level": "ease", "colour": "amber", "reasons": ["HRV onder normaal."]}


@pytest.fixture
def rt(runtime, monkeypatch):
    monkeypatch.setattr(daily_advice, "_runner", lambda rt_: (True, False))
    roles.set_role(runtime.engine, OWN_AGENT, roles.OWN)
    roles.set_role(runtime.engine, ADVISOR, roles.ADVISORY)
    return runtime


def _today(rt) -> date:
    return today_in(rt.settings.timezone)


def _call(rt, client: str, scopes: frozenset[str], tool: str, **arguments):
    """One tool call as a connection with a token: its role is the stored one."""

    async def go():
        tokens = set_connection(client, scopes=scopes, label=f"App {client}")
        try:
            async with Client(build_server(rt)) as session:
                return await session.call_tool(tool, arguments)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def _text(result) -> str:
    return result.content[0].text


def _data(result) -> dict:
    assert not result.is_error, _text(result)
    return result.structured_content["data"]


def _plan(rt) -> str:
    start = _today(rt) - timedelta(days=_today(rt).weekday())
    plan_model.create_plan(
        rt.engine,
        name="Season",
        start=start,
        end=start + timedelta(days=27),
        reason="new season",
        author="athlete",
    )
    return plan_model.set_session(
        rt.engine,
        session_id=None,
        day=_today(rt) + timedelta(days=1),
        name="Duurrit Z2 60'",
        sport="Ride",
        description=RIDE,
        reason="plan",
        author="athlete",
    )["session_id"]


def _session_day(rt, session_id: str) -> date:
    with rt.engine.connect() as conn:
        return conn.execute(
            select(plan_session.c.date).where(plan_session.c.id == session_id)
        ).scalar_one()


def _morning(
    rt, client, scopes, *, safety=OK, key=None, sessions=None, summary="Train.", because=None
):
    day = _today(rt).isoformat()
    extra = {"changed_because": because} if because else {}
    return _call(
        rt,
        client,
        scopes,
        "vibesynergy_coach_write",
        entry_type="morning",
        summary=summary,
        idempotency_key=key or f"morning-{day}",
        for_date=day,
        safety=safety,
        sessions=sessions,
        **extra,
    )


def _runner_morning(rt, **kwargs) -> dict:
    """The primary AI coach's advice, written the way the runner's tool box writes."""
    box = ToolBox(rt, RUNNER_SCOPES)
    day = _today(rt).isoformat()
    text, is_error = box.call(
        "vibesynergy_coach_write",
        {
            "entry_type": "morning",
            "summary": kwargs.get("summary", "Rustig aan vandaag."),
            "idempotency_key": f"morning-{day}",
            "for_date": day,
            "safety": kwargs.get("safety", EASE),
            "sessions": kwargs.get("sessions"),
        },
    )
    assert not is_error, text
    import json

    return json.loads(text)["data"]


# --- the roles ----------------------------------------------------------------------------------


def test_default_roles_and_exactly_one_primary(runtime) -> None:
    engine = runtime.engine
    assert roles.role_of(engine, roles.RUNNER_CLIENT) == roles.PRIMARY
    assert roles.role_of(engine, "oa-new") == roles.ADVISORY  # until the athlete chooses
    roles.set_role(engine, "oa-new", roles.OWN)
    assert roles.role_of(engine, "oa-new") == roles.OWN
    # Giving the primary role to an app takes it from the AI coach inside VibeSynergy.
    roles.set_role(engine, "oa-coach", roles.PRIMARY)
    assert roles.primary_client(engine) == "oa-coach"
    assert roles.role_of(engine, roles.RUNNER_CLIENT) == roles.ADVISORY
    roles.set_role(engine, "oa-other", roles.PRIMARY)
    found = roles.stored(engine)
    assert [c for c, r in found.items() if r == roles.PRIMARY] == ["oa-other"]
    assert found["oa-coach"] == roles.ADVISORY
    # Taking it away gives it back to the AI coach inside VibeSynergy: never none.
    roles.set_role(engine, "oa-other", roles.ADVISORY)
    assert roles.role_of(engine, roles.RUNNER_CLIENT) == roles.PRIMARY
    with pytest.raises(ValueError):
        roles.set_role(engine, "oa-new", "boss")
    with pytest.raises(ValueError):
        roles.set_role(engine, roles.RUNNER_CLIENT, roles.OWN)


def test_a_local_caller_is_the_athlete_himself(runtime) -> None:
    assert roles.current(runtime.engine) == roles.OWN  # no token: stdio, the portal, a test
    tokens = set_connection("oa-unknown", scopes=frozenset({"read"}))
    try:
        assert roles.current(runtime.engine) == roles.ADVISORY
    finally:
        reset_connection(tokens)


def test_every_write_tool_has_a_role_policy(runtime) -> None:
    """A new write tool cannot skip the question of who may call it."""
    from vibesynergy.mcp.scopes import READ_SCOPES

    by_scope = build_server(runtime).vibesynergy_tool_scopes
    writes = {name for name, scope in by_scope.items() if scope not in READ_SCOPES}
    assert writes == set(roles.WRITE_POLICY), writes ^ set(roles.WRITE_POLICY)
    plan_tools = {name for name in writes if name.startswith("vibesynergy_plan_")}
    assert plan_tools == set(plan_proposals.PLAN_TOOLS)


def test_the_handbook_tells_a_connection_its_role_and_key(rt) -> None:
    own = _data(_call(rt, OWN_AGENT, SUGGEST, "vibesynergy_coach_handbook", include_protocol=False))
    assert own["connection"]["role"] == "own" and own["connection"]["writes_daily_advice"]
    adv = _data(_call(rt, ADVISOR, SUGGEST, "vibesynergy_coach_handbook", include_protocol=False))
    found = adv["connection"]
    assert found["role"] == "advisory" and found["writes_daily_advice"] is False
    assert found["key_suffix"] == ADVISOR
    assert found["idempotency_key_example"] == f"morning-{_today(rt).isoformat()}-{ADVISOR}"
    assert "second opinion" in found["may"]


# --- write rights per role ------------------------------------------------------------------------


def test_two_clients_with_different_roles_write_the_plan_differently(rt) -> None:
    session = _plan(rt)
    target = (_today(rt) + timedelta(days=3)).isoformat()
    before = _session_day(rt, session)
    # The advisory AI coach, even with plan.write on its token: a proposal, nothing changed.
    held = _data(
        _call(
            rt,
            ADVISOR,
            FULL,
            "vibesynergy_plan_session_move",
            reason="Rust na de zware rit",
            session_id=session,
            date=target,
        )
    )
    assert held["written"] is False and held["proposal"]["status"] == "open"
    assert held["proposal"]["role"] == "advisory"
    assert held["proposal"]["summary"]["action"] == "move"
    assert _session_day(rt, session) == before
    # With plan.propose only it reaches the same proposal (a retry stores nothing new).
    again = _data(
        _call(
            rt,
            ADVISOR,
            SUGGEST,
            "vibesynergy_plan_session_move",
            reason="Rust na de zware rit",
            session_id=session,
            date=target,
        )
    )
    assert again["proposal"]["id"] == held["proposal"]["id"]
    # The athlete's own agent: written at once, his request is the approval.
    moved = _data(
        _call(
            rt,
            OWN_AGENT,
            FULL,
            "vibesynergy_plan_session_move",
            reason="Robert vroeg het",
            session_id=session,
            date=target,
        )
    )
    assert "proposal" not in moved
    assert _session_day(rt, session).isoformat() == target
    # Without plan.write his own agent cannot write the plan: the scope still counts.
    denied = _call(
        rt,
        OWN_AGENT,
        SUGGEST,
        "vibesynergy_plan_session_skip",
        reason="Robert vroeg het",
        session_id=session,
    )
    assert denied.is_error and "isn't allowed" in _text(denied)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "vibesynergy_icu_create_event",
            {"reason": "r", "category": "WORKOUT", "start_date": "2026-11-20", "name": "Strides"},
        ),
        ("vibesynergy_icu_delete_event", {"reason": "r", "expected_version": "1", "event_id": "9"}),
        ("vibesynergy_icu_revert", {"audit_id": 1, "reason": "r"}),
        ("vibesynergy_coach_memory_write", {"action": "add", "reason": "r", "text": "x"}),
        ("vibesynergy_kb_upsert", {"slug": "hrv-basics", "body_md": "Nieuw."}),
        ("vibesynergy_harness_run", {"task": "morning"}),
        (
            "vibesynergy_activity_publish",
            {"id": "garmin:1", "destination": "strava", "preview_version": "1"},
        ),
        ("vibesynergy_race_set_priority", {"source": "vibesynergy", "source_id": "x"}),
    ],
)
def test_an_advisory_write_outside_its_role_is_refused_with_a_clear_error(
    rt, tool, arguments
) -> None:
    refused = _call(rt, ADVISOR, FULL, tool, **arguments)
    assert refused.is_error
    text = _text(refused)
    assert "Refused by this connection's role (advisory AI coach)" in text
    assert tool in text and "Instead:" in text
    # The refusal is in the audit log, like a refused entry.
    from vibesynergy.store.schema import audit_log

    with rt.engine.connect() as conn:
        outcome = conn.execute(
            select(audit_log.c.outcome)
            .where(audit_log.c.action == tool, audit_log.c.client_id == ADVISOR)
            .order_by(audit_log.c.id.desc())
        ).scalar()
    assert outcome.startswith("refused:")


def test_an_advisory_ai_coach_cannot_publish_through_an_entry(rt) -> None:
    refused = _call(
        rt,
        ADVISOR,
        FULL,
        "vibesynergy_coach_write",
        entry_type="activity_assessment",
        summary="Mooi gelopen.",
        idempotency_key="aa-1",
        for_date=_today(rt).isoformat(),
        activity_id="garmin:1",
        public_insight="Sterke duurloop.",
    )
    assert refused.is_error and "does not publish" in _text(refused)


def test_an_ai_coach_on_its_own_never_softens_a_hard_rule(rt, monkeypatch) -> None:
    from vibesynergy.query import coach_brief

    sick = {"safety": {"level": "refuse", "rules": ["sick"]}}
    monkeypatch.setattr(coach_brief, "today_brief", lambda rt_, day: sick)
    eased = {**OK, "override_reason": "Het voelt wel weer goed."}
    refused = _morning(rt, ADVISOR, SUGGEST, safety=eased)
    assert refused.is_error and "can never be softened" in _text(refused)
    # The athlete's own agent acts on his request: valid with its reason.
    assert not _morning(rt, OWN_AGENT, SUGGEST, safety=eased).is_error


# --- the approval reference ----------------------------------------------------------------------


def _runner_move(rt, session: str, target: str, **extra):
    box = ToolBox(rt, RUNNER_SCOPES)
    return box.call(
        "vibesynergy_plan_session_move",
        {"reason": "Rust na de zware rit", "session_id": session, "date": target, **extra},
    )


def _reply(rt, text: str) -> int:
    entry = _runner_morning(rt, summary="Voorstel: de rit naar donderdag. Antwoord 'akkoord'.")
    return coach_replies.add(rt.engine, entry["id"], text)["id"]


def test_the_primary_without_an_approval_reference_makes_a_proposal(rt) -> None:
    import json

    session = _plan(rt)
    before = _session_day(rt, session)
    target = (_today(rt) + timedelta(days=3)).isoformat()
    text, is_error = _runner_move(rt, session, target)
    assert not is_error, text
    held = json.loads(text)["data"]
    assert held["written"] is False and held["proposal"]["role"] == "primary"
    assert "approval_id" in held["note"]
    assert _session_day(rt, session) == before
    waiting = inbox.inbox(rt, _today(rt))
    assert waiting["count"] == 1 and waiting["decisions"][0]["kind"] == "plan_proposal"
    assert inbox.count(rt.engine) == 1


def test_the_athletes_akkoord_reply_is_an_approval_reference(rt) -> None:
    session = _plan(rt)
    target = (_today(rt) + timedelta(days=3)).isoformat()
    no = _reply(rt, "Nee, niet akkoord. Laat maar staan.")
    text, is_error = _runner_move(rt, session, target, approval_id=f"reply:{no}")
    assert is_error and "not an explicit 'akkoord'" in text
    text, is_error = _runner_move(rt, session, target, approval_id="reply:9999")
    assert is_error and "no reply 9999" in text
    text, is_error = _runner_move(rt, session, target, approval_id="akkoord")
    assert is_error and "is not a reference" in text
    assert plan_proposals.pending(rt.engine) == []  # a bad reference is refused, not parked
    yes = coach_replies.add(rt.engine, 1, "Akkoord, doe maar.")["id"]
    # An old approval is no approval.
    with rt.engine.begin() as conn:
        old = utc_now() - timedelta(hours=plan_proposals.APPROVAL_HOURS + 1)
        conn.execute(
            update(coach_reply).where(coach_reply.c.id == yes).values(at=old, updated_at=old)
        )
    text, is_error = _runner_move(rt, session, target, approval_id=f"reply:{yes}")
    assert is_error and "older than" in text
    with rt.engine.begin() as conn:
        conn.execute(
            update(coach_reply)
            .where(coach_reply.c.id == yes)
            .values(at=utc_now(), updated_at=utc_now())
        )
    text, is_error = _runner_move(rt, session, target, approval_id=f"reply:{yes}")
    assert not is_error, text
    assert _session_day(rt, session).isoformat() == target


def test_approving_a_proposal_carries_it_out_and_is_a_reference(rt) -> None:
    import json

    session = _plan(rt)
    target = (_today(rt) + timedelta(days=3)).isoformat()
    text, _err = _runner_move(rt, session, target)
    proposal = json.loads(text)["data"]["proposal"]["id"]
    # Not approved yet: no reference.
    text, is_error = box_week(rt, approval_id=f"proposal:{proposal}")
    assert is_error and "is open, not approved" in text
    settings = replace(rt.settings, server={"public_url": HOST})
    portal_rt = Runtime.open(settings)
    try:
        client = TestClient(create_portal_app(portal_rt), base_url=HOST)
        listed = client.get("/api/inbox").json()["data"]
        assert listed["decisions"][0]["change"]["action"] == "move"
        done = client.post(f"/api/plan_proposals/{proposal}/approve", json={}, headers=WRITE)
        assert done.status_code == 200, done.text
        assert done.json()["data"]["proposal"]["status"] == "approved"
        assert done.json()["data"]["inbox"]["count"] == 0
        twice = client.post(f"/api/plan_proposals/{proposal}/approve", json={}, headers=WRITE)
        assert twice.status_code == 400
    finally:
        portal_rt.close()
    assert _session_day(rt, session).isoformat() == target
    # The plan version carries the proposer and the approval.
    from vibesynergy.store.schema import plan_version

    with rt.engine.connect() as conn:
        last = (
            conn.execute(select(plan_version).order_by(plan_version.c.n.desc())).mappings().first()
        )
    assert last["author"] == "client:coach-runner"
    assert f"proposal {proposal}" in last["reason"]
    # The approved proposal covers the coupled week note (COACH-5).
    text, is_error = box_week(rt, approval_id=f"proposal:{proposal}")
    assert not is_error, text


def box_week(rt, **extra):
    return ToolBox(rt, RUNNER_SCOPES).call(
        "vibesynergy_plan_week_set",
        {
            "reason": "Week in de pas met de verplaatste rit",
            "week": 1,
            "note": "Rit op do.",
            **extra,
        },
    )


def test_a_rejected_proposal_changes_nothing(rt) -> None:
    session = _plan(rt)
    before = _session_day(rt, session)
    held = _data(
        _call(
            rt,
            ADVISOR,
            SUGGEST,
            "vibesynergy_plan_session_skip",
            reason="Te veel deze week",
            session_id=session,
        )
    )
    plan_proposals.reject(rt.engine, held["proposal"]["id"])
    assert plan_proposals.pending(rt.engine) == []
    assert _session_day(rt, session) == before
    with rt.engine.connect() as conn:
        status = conn.execute(select(plan_proposal.c.status)).scalar_one()
    assert status == "rejected"


def test_a_proposal_that_cannot_be_carried_out_is_refused_when_made(rt) -> None:
    _plan(rt)
    bad = _call(
        rt,
        ADVISOR,
        SUGGEST,
        "vibesynergy_plan_session_move",
        reason="x",
        session_id="nope",
        date=_today(rt).isoformat(),
    )
    assert bad.is_error and "no session 'nope'" in _text(bad)
    assert plan_proposals.pending(rt.engine) == []


def test_the_primary_needs_an_approval_reference_for_intervals_icu(rt) -> None:
    text, is_error = ToolBox(rt, RUNNER_SCOPES).call(
        "vibesynergy_icu_create_event",
        {"reason": "r", "category": "WORKOUT", "start_date": "2026-11-20", "name": "Strides"},
    )
    assert is_error
    assert "Refused by this connection's role (primary AI coach)" in text
    assert "approval_id" in text


def test_is_yes() -> None:
    for text in ("akkoord", "Ja, doe maar", "Approved!", "ok, prima"):
        assert plan_proposals.is_yes(text), text
    for text in ("niet akkoord", "nee", "No, not yet", "misschien", ""):
        assert not plan_proposals.is_yes(text), text


# --- keys per author, and the second opinion ------------------------------------------------


def test_entry_keys_are_per_author_and_never_collide(rt) -> None:
    day = _today(rt).isoformat()
    primary = _runner_morning(rt)
    assert primary["idempotency_key"] == f"morning-{day}"
    # The advisory AI coach uses the same key: its own entry, under its own key.
    second = _data(_morning(rt, ADVISOR, SUGGEST, key=f"morning-{day}"))
    assert second["id"] != primary["id"]
    assert second["idempotency_key"] == f"morning-{day}-{ADVISOR}"
    assert second["author"] == f"client:{ADVISOR}"
    # A retry, with either spelling of the key, writes nothing new.
    assert _data(_morning(rt, ADVISOR, SUGGEST, key=f"morning-{day}"))["id"] == second["id"]
    retry = _data(_morning(rt, ADVISOR, SUGGEST, key=f"morning-{day}-{ADVISOR}"))
    assert retry["id"] == second["id"]
    # And the primary's retry still finds its own.
    assert _runner_morning(rt)["id"] == primary["id"]
    # Another author who meets a taken key gets it under its own suffix as well.
    # The athlete's own agent writes the next version of the day's advice (#115).
    because = {"previous": primary["id"], "reason": "Op verzoek opnieuw bekeken, zelfde advies."}
    own = _data(_morning(rt, OWN_AGENT, SUGGEST, key=f"morning-{day}", because=because))
    assert own["idempotency_key"] == f"morning-{day}-{OWN_AGENT}"
    with rt.engine.connect() as conn:
        assert len(conn.execute(select(coach_decision_log.c.id)).all()) == 3


def test_only_the_primarys_entry_is_the_daily_advice(rt) -> None:
    opinion = _morning(rt, ADVISOR, SUGGEST, safety=OK, summary="Gewoon trainen.")
    stored = _data(opinion)
    assert stored["meta"]["harness"]["role"] == "advisory"
    assert any("second opinion" in w for w in opinion.structured_content["meta"]["warnings"])
    # Alone, an advisory entry is no advice: the rules still give it.
    alone = daily_advice.current(rt, _today(rt), trigger=False)
    assert alone["source"] == "rules" and alone["fallback"]["reason"] == "no_run"
    assert [o["id"] for o in alone["second_opinions"]] == [stored["id"]]
    primary = _runner_morning(rt, safety=EASE)
    found = daily_advice.current(rt, _today(rt), trigger=False)
    assert found["source"] == "ai" and found["entry"]["id"] == primary["id"]
    assert found["safety_level"] == "ease"
    assert [v["id"] for v in found["versions"]] == [primary["id"]]  # no version of the advice
    # A second opinion is no run of the morning task either.
    _last, ran = coach_tasks._runs(
        rt.engine, next(t for t in coach_tasks.TASKS if t.id == "morning"), _today(rt)
    )
    assert ran == {_today(rt)}
    with rt.engine.begin() as conn:
        conn.execute(coach_decision_log.delete().where(coach_decision_log.c.id == primary["id"]))
    assert coach_tasks._runs(
        rt.engine, next(t for t in coach_tasks.TASKS if t.id == "morning"), _today(rt)
    ) == (None, set())


def test_a_second_opinion_that_differs_is_flagged_and_reaches_the_primary(rt) -> None:
    sessions = [{"title": "Duurrit Z2 60'", "decision": "keep"}]
    primary = _runner_morning(rt, safety=EASE, sessions=sessions)
    agrees = daily_advice.current(rt, _today(rt), trigger=False)
    assert agrees["second_opinions"] == []
    _data(
        _morning(
            rt,
            ADVISOR,
            SUGGEST,
            safety={"level": "flag", "colour": "amber", "reasons": ["Slecht geslapen."]},
            sessions=[{"title": "duurrit z2 60'", "decision": "skip"}],
            summary="Sla de rit over.",
        )
    )
    found = daily_advice.current(rt, _today(rt), trigger=False)
    assert found["entry"]["id"] == primary["id"] and found["safety_level"] == "ease"
    (opinion,) = found["second_opinions"]
    assert opinion["disagrees"] and opinion["author_name"] == f"App {ADVISOR}"
    assert opinion["differences"] == [
        {"field": "safety_level", "primary": "ease", "advisory": "flag"},
        {
            "field": "sessions",
            "changes": [{"title": "Duurrit Z2 60'", "primary": "keep", "advisory": "skip"}],
        },
    ]
    # Flagged to the athlete under Waiting for you.
    notices = [
        n for n in inbox.inbox(rt, _today(rt))["notices"] if n["kind"] == "coach_disagreement"
    ]
    assert len(notices) == 1 and notices[0]["date"] == _today(rt).isoformat()
    # And the primary AI coach gets it in its next run.
    assert "coach_team" in registry.chain_for("morning")
    loaded = context.preload(rt, "morning", _today(rt))["text"]
    assert '<context name="coach_team"' in loaded
    assert "differs_from_your_advice" in loaded and "Sla de rit over." in loaded
    # The same in the coach feed and for a connection that reads the day over MCP.
    from vibesynergy.query import coach_workouts

    feed = coach_workouts.coach_feed(rt)
    assert [e["second_opinion"] for e in feed] == [True, False]
    today = _data(_call(rt, OWN_AGENT, SUGGEST, "vibesynergy_today"))
    assert "Sla de rit over." in str(today)


def test_a_second_opinion_that_agrees_is_listed_without_a_flag(rt) -> None:
    _runner_morning(rt, safety=EASE)
    _data(_morning(rt, ADVISOR, SUGGEST, safety=EASE, summary="Ook rustig aan."))
    (opinion,) = daily_advice.current(rt, _today(rt), trigger=False)["second_opinions"]
    assert opinion["disagrees"] is False and opinion["differences"] == []
    assert not [
        n for n in inbox.inbox(rt, _today(rt))["notices"] if n["kind"] == "coach_disagreement"
    ]


# --- Settings → Sharing → Coach apps --------------------------------------------------------


def test_the_role_is_set_in_settings(runtime) -> None:
    from vibesynergy.api.oauth import create_personal_access_token

    create_personal_access_token(runtime.engine, "Second opinion", scopes=["read", "coach.write"])
    settings = replace(runtime.settings, server={"public_url": HOST})
    portal_rt = Runtime.open(settings)
    try:
        client = TestClient(create_portal_app(portal_rt), base_url=HOST)
        data = client.get("/api/mcp_clients").json()["data"]
        token = data["tokens"][0]
        assert token["role"] == "advisory"  # a new connection starts as advisory
        assert data["runner"] == {"client_id": "coach-runner", "role": "primary"}
        assert [r["id"] for r in data["roles"]] == ["own", "primary", "advisory"]
        saved = client.post(
            "/api/mcp/role", json={"client_id": f"pat:{token['id']}", "role": "own"}, headers=WRITE
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["tokens"][0]["role"] == "own"
        unknown = client.post(
            "/api/mcp/role", json={"client_id": "oa-nobody", "role": "own"}, headers=WRITE
        )
        assert unknown.status_code == 400
        bad = client.post(
            "/api/mcp/role", json={"client_id": f"pat:{token['id']}", "role": "boss"}, headers=WRITE
        )
        assert bad.status_code == 400
        primary = client.post(
            "/api/mcp/role",
            json={"client_id": f"pat:{token['id']}", "role": "primary"},
            headers=WRITE,
        ).json()["data"]
        assert primary["tokens"][0]["role"] == "primary"
        assert primary["runner"]["role"] == "advisory"
    finally:
        portal_rt.close()


def test_the_migration_keeps_what_is_connected_as_the_athletes_own(tmp_path) -> None:
    """Apps and tokens that had access before #118 keep writing as before (role own)."""
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = importlib.import_module("vibesynergy.store.migrations.versions.0054_coach_roles")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE oauth_client (client_id TEXT PRIMARY KEY, approved_at TEXT, "
                "revoked_at TEXT)"
            )
        )
        conn.execute(sa.text("CREATE TABLE mcp_pat (id TEXT PRIMARY KEY, revoked_at TEXT)"))
        conn.execute(
            sa.text(
                "INSERT INTO oauth_client VALUES ('oa-1', '2026-11-01', NULL), "
                "('oa-pending', NULL, NULL), ('oa-gone', '2026-11-01', '2026-11-02')"
            )
        )
        conn.execute(sa.text("INSERT INTO mcp_pat VALUES ('t1', NULL), ('t2', '2026-11-02')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        found = dict(conn.execute(sa.text("SELECT client_id, role FROM coach_role")).all())
    assert found == {"coach-runner": "primary", "oa-1": "own", "pat:t1": "own"}


def test_a_second_opinion_is_no_day_decision_and_no_message(rt) -> None:
    """Only the primary's entry is the day's decision and a notice; the second opinion
    shows with the advice and is never sent as a message from the AI coach."""
    from vibesynergy import alerts
    from vibesynergy.query import daily_coach

    since = utc_now() - timedelta(seconds=5)
    _data(_morning(rt, ADVISOR, SUGGEST, safety=OK, summary="Gewoon trainen."))
    assert alerts._daily(rt, since) == []
    assert daily_coach.read(rt, _today(rt))["morning"]["state"] == "no_coach"
    own = _data(_morning(rt, OWN_AGENT, SUGGEST, safety=EASE, summary="Rustig aan."))
    # The own agent's daily advice is a version in the day thread (#115): it has the day's
    # advice notice, linked to it, and is no second message.
    assert alerts._daily(rt, since) == []
    notices = [e for e in alerts.for_portal(rt.settings)["recent"] if e["kind"] == "advice"]
    assert [n["entry_id"] for n in notices] == [own["id"]]
    assert "Rustig aan." in str(daily_coach.read(rt, _today(rt))["morning"])


# --- the standing approval: filling the horizon ------------------------------------------------


def _runner_add(rt, day: date, **extra):
    import json

    text, is_error = ToolBox(rt, RUNNER_SCOPES).call(
        "vibesynergy_plan_session_set",
        {
            "reason": "Horizon aanvullen volgens de weeknotitie",
            "date": day.isoformat(),
            "name": "Duurloop Z2 40'",
            "sport": "Run",
            "description": "- 40m Z2 HR",
            **extra,
        },
    )
    assert not is_error, text
    return json.loads(text)["data"]


def _week_note(rt, day: date) -> None:
    start = _today(rt) - timedelta(days=_today(rt).weekday())
    plan_model.set_week(
        rt.engine,
        week=(day - start).days // 7 + 1,
        note="Rustige week: drie duurtrainingen.",
        reason="plan",
        author="athlete",
    )


def test_the_primary_fills_a_free_slot_of_an_approved_week_without_approval(rt) -> None:
    session = _plan(rt)  # a ride tomorrow
    day = _today(rt) + timedelta(days=8)
    # No week note yet: the week is not approved, so the fill is a proposal.
    held = _runner_add(rt, day)
    assert held["written"] is False and held["proposal"]["summary"]["action"] == "add"
    plan_proposals.reject(rt.engine, held["proposal"]["id"])
    _week_note(rt, day)
    added = _runner_add(rt, day)
    assert "proposal" not in added and added["session_id"].endswith(f"{day.isoformat()}:run")
    # The slot is taken now: writing it again is a change and needs approval.
    again = _runner_add(rt, day, name="Duurloop Z2 60'")
    assert again["written"] is False
    # Another slot on the same day is still free.
    assert "proposal" not in _runner_add(rt, day, sport="Swim", description="- 30m Z2 Pace")
    # Changing, moving or skipping an existing session is never a fill.
    _week_note(rt, _session_day(rt, session))
    changed = _runner_add(rt, _session_day(rt, session), session_id=session, sport="Ride")
    assert changed["written"] is False
    text, is_error = _runner_move(rt, session, day.isoformat())
    assert not is_error and '"written":false' in text
    # A skipped session's slot is not free: putting it back is a change.
    plan_model.skip_session(
        rt.engine, session_id=added["session_id"], reason="ziek", author="athlete"
    )
    assert _runner_add(rt, day)["written"] is False
    # A week note or a target is never a fill.
    text, is_error = box_week(rt)
    assert not is_error and '"written":false' in text


def test_an_advisory_ai_coach_never_fills_the_horizon_itself(rt) -> None:
    _plan(rt)
    day = _today(rt) + timedelta(days=8)
    _week_note(rt, day)
    held = _data(
        _call(
            rt,
            ADVISOR,
            FULL,
            "vibesynergy_plan_session_set",
            reason="Aanvullen",
            date=day.isoformat(),
            name="Duurloop Z2 40'",
            sport="Run",
            description="- 40m Z2 HR",
        )
    )
    assert held["written"] is False and held["proposal"]["role"] == "advisory"
