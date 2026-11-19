"""The AI harness's task registry (docs/AI_HARNESS.md).

Every task the harness runs is declared here: its triggers, what it must deliver (the output
contract), which tools it may use and what it may write on its own, what needs the athlete's
approval, what a model must be able to do, its budget, its prompt and its context chain.
The registry is code (with tests); the portal makes the choices and texts editable
(on/off, trigger times, model, prompt, chain, budget) per task.

Tasks 1-5 of the task list are the ones running today (morning, evening, week review,
insight, reply). The others follow one by one, each switchable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The fixed base every task gets before its own chain (decision 9-11).
BASE_CONTEXT = ("athlete", "communication_rule", "safety", "coach_memory")


@dataclass(frozen=True)
class Trigger:
    kind: str  # "schedule", "event" or "manual"
    detail: str = ""  # "daily 09:04", "weekly sun 19:44", "activity.published", "reply"


@dataclass(frozen=True)
class Contract:
    entry_type: str | None  # the coach entry the task writes, or None (e.g. the insight)
    required: tuple[str, ...] = ()  # fields the entry must carry
    max_words: int | None = None  # the report's summary, for the communication rule
    # A daily advice carries the structured safety outcome (level, colour, reasons), checked
    # against the rules' floor when it is written (coach_runner.contract, #88; #89 reads it).
    safety: bool = False
    # The day the entry must be about: "today", "today_or_tomorrow", or "" (not checked).
    target: str = ""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    triggers: tuple[Trigger, ...]
    contract: Contract
    prompt: str  # the KB prompt (coach-prompt-<prompt>)
    chain: tuple[str, ...]  # context blocks after BASE_CONTEXT, in order
    writes: tuple[str, ...]  # what the task may do on its own
    approval: tuple[str, ...]  # what waits for the athlete's yes
    requires: tuple[str, ...] = ("tool_use",)  # model capabilities (tool use always)
    runs: str = "model"  # "model", or "code": a routine without a model (no provider)
    scopes: tuple[str, ...] = ()  # tool scopes beyond the coach's own (read, coach/plan write)
    default_on: bool = True
    status: str = "live"  # live, planned
    notes: str = field(default="", compare=False)


TASKS: tuple[Task, ...] = (
    Task(
        id="morning",
        title="Morning advice",
        # The fixed advice run (#90); new data after it makes a new version of today's.
        triggers=(
            Trigger("schedule", "daily 07:00"),
            Trigger("event", "new data for today's advice"),
            Trigger("manual", "test now"),
        ),
        contract=Contract(
            "morning", ("summary", "for_date"), max_words=70, safety=True, target="today"
        ),
        prompt="morning",
        chain=(
            "today",
            "recovery",
            "plan_horizon",
            "weather",
            "replies",
            "coach_team",
            "recent_reports",
        ),
        # The standing approval (#118): a new session in a free slot of a week that has
        # its note is written directly; every other plan write needs an approval reference.
        writes=("report", "fill day 9 of the horizon"),
        approval=("change today or tomorrow",),
    ),
    Task(
        id="evening",
        title="Evening review",
        # Its advice is for tomorrow (#90); new data after it makes a new version of that.
        triggers=(
            Trigger("schedule", "daily 21:45"),
            Trigger("event", "new data for tomorrow's advice"),
            Trigger("manual", "test now"),
        ),
        contract=Contract(
            "evening",
            ("summary", "for_date"),
            max_words=70,
            safety=True,
            target="today_or_tomorrow",
        ),
        prompt="evening",
        chain=("today", "week", "recovery", "plan_horizon", "weather", "replies", "coach_team"),
        writes=("report", "predictions"),
        approval=("move sessions",),
    ),
    Task(
        id="weekly",
        title="Week review",
        triggers=(Trigger("schedule", "weekly sun 19:44"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=150),
        prompt="weekly",
        chain=("week", "fitness", "plan_next_week", "replies", "coach_team", "recent_reports"),
        writes=("report",),
        approval=("next week's changes",),
    ),
    Task(
        id="monthly",
        title="Month report",
        triggers=(Trigger("schedule", "monthly 1st 10:04"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=150),
        prompt="monthly",
        chain=("month", "fitness", "races", "recent_reports", "replies"),
        writes=("report",),
        approval=(),
    ),
    Task(
        id="block",
        title="Block review",
        triggers=(Trigger("schedule", "Sunday a phase ends, 20:14"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=150),
        prompt="block",
        chain=("block", "fitness", "races", "recent_reports", "replies"),
        writes=("report",),
        approval=("the next block",),
    ),
    Task(
        id="race",
        title="Race brief",
        triggers=(Trigger("schedule", "race -7 and -1 days, 18:04"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=150),
        prompt="race",
        chain=("races", "plan_horizon", "recovery", "fitness", "weather", "replies"),
        writes=("report",),
        approval=("taper changes",),
    ),
    Task(
        id="test",
        title="Test evaluation",
        triggers=(Trigger("event", "planned test session done"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=100),
        prompt="test",
        chain=("activity", "plan_match", "fitness", "replies"),
        writes=("report", "threshold proposal"),
        approval=("new thresholds",),
        scopes=("plan.propose",),
        notes="A planned session named test, FTP or CSS, with an activity of that sport that day.",
    ),
    Task(
        id="thresholds",
        title="Threshold check",
        triggers=(
            Trigger("schedule", "Monday 09:34, only on a new signal"),
            Trigger("manual", "test now"),
        ),
        contract=Contract("note", ("summary", "for_date"), max_words=70),
        prompt="thresholds",
        chain=("threshold_signals", "fitness", "replies"),
        writes=("report", "threshold proposal"),
        approval=("new thresholds",),
        scopes=("plan.propose",),
        notes="Each set of signals runs once; the same signal never comes back weekly.",
    ),
    Task(
        id="kb",
        title="Knowledge base care",
        triggers=(Trigger("schedule", "monthly 15th 10:34"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=100),
        prompt="kb",
        chain=("kb_due", "replies"),
        writes=("report", "KB change proposal"),
        approval=("KB changes",),
        scopes=("plan.propose",),
        notes="Five articles a month, oldest first; vibesynergy_kb_upsert is refused in this task.",
    ),
    Task(
        id="injury",
        title="Injury check",
        triggers=(Trigger("event", "pain or an injury logged"), Trigger("manual", "test now")),
        contract=Contract(
            "note", ("summary", "for_date"), max_words=70, safety=True, target="today"
        ),
        prompt="injury",
        chain=("today", "recovery", "plan_horizon", "replies", "coach_team"),
        writes=("report",),
        approval=("plan changes",),
        notes="Pain or an injury logged for today makes a new version of today's advice at "
        "once (#90), within the daily maximum of updates.",
    ),
    Task(
        id="checkin",
        title="Check-in reaction",
        triggers=(
            Trigger("event", "check-in saved after the fixed advice run"),
            Trigger("manual", "test now"),
        ),
        contract=Contract(
            "note", ("summary", "for_date"), max_words=70, safety=True, target="today"
        ),
        prompt="checkin",
        chain=("today", "recovery", "plan_horizon", "replies", "coach_team"),
        writes=("report",),
        approval=("swapping today's session",),
        notes="A check-in saved after the fixed advice run makes a new version of today's "
        "advice a few minutes later (#90), within the daily maximum of updates. One saved "
        "before it is read by the fixed run.",
    ),
    Task(
        id="insight",
        title="Line under a public activity",
        triggers=(Trigger("event", "activity.published"),),
        contract=Contract("activity_assessment", ("summary",)),
        prompt="insight",
        chain=("activity", "plan_match"),
        writes=("insight",),
        approval=(),
    ),
    Task(
        id="catch_up",
        title="Catch up a missed report",
        triggers=(Trigger("event", "coach.run_missed"),),
        contract=Contract(None),
        prompt="",
        chain=(),
        writes=("queues today's report once",),
        approval=(),
        requires=(),
        runs="code",
        notes="Only when the window has passed and no run of the day exists.",
    ),
    Task(
        id="reply",
        title="Reply to a report",
        triggers=(Trigger("event", "reply"),),
        contract=Contract(None),
        prompt="evening",
        chain=("replies", "coach_team", "recent_reports", "plan_horizon"),
        writes=("answer",),
        approval=("the approved action itself",),
        status="planned",
        notes="A reply makes a new version of the current advice (#90), which reads it; "
        "an answer of its own is not built yet.",
    ),
)

BY_ID = {task.id: task for task in TASKS}


def chain_for(task_id: str) -> tuple[str, ...]:
    """The task's full context: the fixed base, then its own blocks."""
    task = BY_ID[task_id]
    return BASE_CONTEXT + tuple(b for b in task.chain if b not in BASE_CONTEXT)


def daily() -> tuple[str, ...]:
    """The daily advice tasks: their entry carries the structured safety outcome."""
    return tuple(t.id for t in TASKS if t.contract.safety)


def model_ok(task_id: str, capabilities: set[str]) -> bool:
    """A model may run the task only when it has every required capability (tool use)."""
    return set(BY_ID[task_id].requires) <= capabilities


def live() -> tuple[str, ...]:
    """The tasks the runner runs with a model, in registry order."""
    return tuple(t.id for t in TASKS if t.status == "live" and t.runs == "model")


def scheduled() -> tuple[str, ...]:
    """Live tasks on a clock: the reports (entry, mail, schedule settings)."""
    return tuple(
        t.id for t in TASKS if t.status == "live" and any(g.kind == "schedule" for g in t.triggers)
    )


def reports() -> tuple[str, ...]:
    """Live tasks that write a report (an entry that is mailed): on a clock or on an event."""
    return tuple(i for i in live() if "report" in BY_ID[i].writes)


def entry_types() -> dict[str, str]:
    return {i: BY_ID[i].contract.entry_type or i for i in reports()}
