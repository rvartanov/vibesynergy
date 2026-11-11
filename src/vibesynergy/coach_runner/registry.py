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
        triggers=(Trigger("schedule", "daily 09:04"), Trigger("manual", "test now")),
        contract=Contract("morning", ("summary", "for_date"), max_words=70),
        prompt="morning",
        chain=("today", "recovery", "plan_horizon", "weather", "replies", "recent_reports"),
        writes=("report", "fill day 9 of the horizon"),
        approval=("change today or tomorrow",),
    ),
    Task(
        id="evening",
        title="Evening review",
        triggers=(Trigger("schedule", "daily 21:45"), Trigger("manual", "test now")),
        contract=Contract("evening", ("summary", "for_date"), max_words=70),
        prompt="evening",
        chain=("today", "week", "recovery", "plan_horizon", "weather", "replies"),
        writes=("report", "predictions"),
        approval=("move sessions",),
    ),
    Task(
        id="weekly",
        title="Week review",
        triggers=(Trigger("schedule", "weekly sun 19:44"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=150),
        prompt="weekly",
        chain=("week", "fitness", "plan_next_week", "replies", "recent_reports"),
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
        triggers=(Trigger("event", "pain logged"), Trigger("manual", "test now")),
        contract=Contract("note", ("summary", "for_date"), max_words=70),
        prompt="injury",
        chain=("today", "recovery", "plan_horizon", "replies"),
        writes=("report",),
        approval=("plan changes",),
        notes="Pain 4/10 or more, or pain that affects movement, logged for today; once a day.",
    ),
    Task(
        id="checkin",
        title="Check-in reaction",
        triggers=(
            Trigger("event", "low check-in after the morning report"),
            Trigger("manual", "test now"),
        ),
        contract=Contract("note", ("summary", "for_date"), max_words=70),
        prompt="checkin",
        chain=("today", "recovery", "plan_horizon", "replies"),
        writes=("report",),
        approval=("swapping today's session",),
        notes="Two or more low scores, after the morning report and before 20:00; once a day.",
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
        chain=("replies", "recent_reports", "plan_horizon"),
        writes=("answer",),
        approval=("the approved action itself",),
        status="planned",
        notes="Today replies are read by the next morning/evening run.",
    ),
)

BY_ID = {task.id: task for task in TASKS}


def chain_for(task_id: str) -> tuple[str, ...]:
    """The task's full context: the fixed base, then its own blocks."""
    task = BY_ID[task_id]
    return BASE_CONTEXT + tuple(b for b in task.chain if b not in BASE_CONTEXT)


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
