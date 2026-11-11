"""The runner's switch, monthly cap, schedule and API key.

Defaults come from ``[coach_runner]`` in config.toml (``monthly_cap_usd`` 95, ``max_turns``,
``max_output_tokens``, ``effort``); the portal's switch and cap are stored in the encrypted
``coach_runner`` secret store and win, so a change applies at the next scheduler tick without a
restart. The schedule ("when the reports are made, time and day") is stored there
too: per report on or off, a local time and the weekdays (Monday is 0). The API key is stored
there too, encrypted, and never leaves it: not in a response, a log line or the audit log (only
whether one is set, and when it changed).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import audit_log

STORE = "coach_runner"
SETTINGS_DOC = "settings"
KEY_DOC = "api_key"
# AI providers: Anthropic's Messages API, and the OpenAI format for OpenAI, xAI and a
# local server (Ollama, LM Studio, vLLM). A key per provider, in this store; the local server
# needs its address and maybe no key. Signing in with a ChatGPT or Grok account is not offered
# by OpenAI or xAI to other apps, so it is an API key for those.
PROVIDERS = ("anthropic", "openai", "xai", "local")
PROVIDER_URLS = {"openai": "https://api.openai.com/v1", "xai": "https://api.x.ai/v1"}
TASKS = (
    "morning",
    "evening",
    "weekly",
    "monthly",
    "block",
    "race",
    "test",
    "thresholds",
    "kb",
    "injury",
    "checkin",
    "insight",
    "catch_up",
)
# Tasks started by an event, not a clock: their switch is "on" in the task; the
# reports keep theirs in the schedule.
EVENT_TASKS = ("test", "injury", "checkin", "insight", "catch_up")
STATE_DOC = "state"

DEFAULT_CAP_USD = 95.0
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_OUTPUT_TOKENS = 60_000  # per run, all turns together
RESPONSE_MAX_TOKENS = 16_000  # per model response
DEFAULT_EFFORT = "high"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_CAP_USD = 1000.0
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]
DEFAULT_SCHEDULE: dict[str, dict[str, Any]] = {
    "morning": {"on": True, "time": "09:04", "days": EVERY_DAY},
    "evening": {"on": True, "time": "21:45", "days": EVERY_DAY},
    "weekly": {"on": True, "time": "19:44", "days": [6]},
    # Monthly runs on a day of the month (1-28), not a weekday (task 14). Off by
    # default: the block review covers it; optional.
    "monthly": {"on": False, "time": "10:04", "days": EVERY_DAY, "monthday": 1},
    # Only on the Sunday a plan phase ends (task 12).
    "block": {"on": True, "time": "20:14", "days": [6]},
    # Only on the days a race is ``RACE_DAYS_BEFORE`` away (task 13).
    "race": {"on": True, "time": "18:04", "days": EVERY_DAY},
    # Only when the measured values disagree with the thresholds, once per signal (task 11).
    "thresholds": {"on": True, "time": "09:34", "days": [0]},
    # Knowledge-base care on a day of the month (task 18).
    "kb": {"on": True, "time": "10:34", "days": EVERY_DAY, "monthday": 15},
}
RACE_DAYS_BEFORE = (7, 1)
MONTHLY_JOBS = ("monthly", "kb")  # on a day of the month, not a weekday
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
KEY_PREFIX = "sk-ant-"


class RunnerSettingsError(ValueError):
    """A setting was not saved; the message is plain English for the page to translate."""


@dataclass(frozen=True)
class RunnerSettings:
    enabled: bool
    monthly_cap_usd: float
    max_turns: int
    max_output_tokens: int
    effort: str
    api_key_set: bool
    schedule: dict[str, dict[str, Any]]
    models: dict[str, str]
    tasks: dict[str, dict[str, str]]
    keys: dict[str, bool]
    local_url: str | None

    def usable(self, provider: str) -> bool:
        if provider == "local":
            return bool(self.local_url)
        return bool(self.keys.get(provider))


def store(rt) -> SecretStore:
    return SecretStore(rt.settings.secrets_dir, STORE)


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number > 0 else default


def load(rt) -> RunnerSettings:
    cfg = rt.settings.coach_runner or {}
    saved = store(rt).get(SETTINGS_DOC) or {}
    effort = str(cfg.get("effort") or DEFAULT_EFFORT)
    return RunnerSettings(
        enabled=bool(saved.get("enabled", False)),
        monthly_cap_usd=_number(
            saved.get("monthly_cap_usd"), _number(cfg.get("monthly_cap_usd"), DEFAULT_CAP_USD)
        ),
        max_turns=int(_number(cfg.get("max_turns"), DEFAULT_MAX_TURNS)),
        max_output_tokens=int(_number(cfg.get("max_output_tokens"), DEFAULT_MAX_OUTPUT_TOKENS)),
        effort=effort if effort in EFFORTS else DEFAULT_EFFORT,
        api_key_set=any(api_key(rt, p) for p in PROVIDERS) or bool(saved.get("local_url")),
        schedule=_schedule(saved.get("schedule")),
        models=(models := _models(saved.get("models"))),
        tasks=_tasks(saved.get("tasks"), models),
        keys={p: bool(api_key(rt, p)) for p in PROVIDERS},
        local_url=saved.get("local_url") or None,
    )


def _tasks(saved: Any, models: dict[str, str]) -> dict[str, dict[str, str]]:
    """Provider and model per task; without a choice, Anthropic with the kind's model."""
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for task in TASKS:
        mine = saved.get(task) if isinstance(saved.get(task), dict) else {}
        default = models["insight" if task == "insight" else "coach"]
        out[task] = {
            "provider": mine.get("provider") if mine.get("provider") in PROVIDERS else "anthropic",
            "model": str(mine.get("model") or default),
        }
        if task in EVENT_TASKS:
            out[task]["on"] = mine.get("on") is not False
    return out


def _models(saved: Any) -> dict[str, str]:
    from vibesynergy.coach_runner import MODEL, MODEL_KINDS, MODELS

    saved = saved if isinstance(saved, dict) else {}
    return {kind: saved.get(kind) if saved.get(kind) in MODELS else MODEL for kind in MODEL_KINDS}


def _schedule(saved: Any) -> dict[str, dict[str, Any]]:
    """The defaults with what was saved on top, per report."""
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for job, default in DEFAULT_SCHEDULE.items():
        mine = saved.get(job) if isinstance(saved.get(job), dict) else {}
        out[job] = {**default, **mine}
    return out


def _check_schedule(value: Any, current: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SCHEDULE):
        raise RunnerSettingsError("Unknown report.")
    out = {job: dict(spec) for job, spec in current.items()}
    for job, change in value.items():
        allowed = {"on", "time", "days"} | ({"monthday"} if job in MONTHLY_JOBS else set())
        if not isinstance(change, dict) or set(change) - allowed:
            raise RunnerSettingsError("Unknown setting.")
        spec = {**out[job], **change}
        if not isinstance(spec["on"], bool):
            raise RunnerSettingsError("Choose on or off.")
        if not isinstance(spec["time"], str) or not _TIME.match(spec["time"]):
            raise RunnerSettingsError("Give the time as hours and minutes, e.g. 21:45.")
        days = spec["days"]
        if (
            not isinstance(days, list)
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)
            or len(set(days)) != len(days)
        ):
            raise RunnerSettingsError("Choose the days from Monday to Sunday.")
        if spec["on"] and not days:
            raise RunnerSettingsError("Choose at least one day, or switch this report off.")
        if job in MONTHLY_JOBS:
            monthday = spec.get("monthday")
            if (
                isinstance(monthday, bool)
                or not isinstance(monthday, int)
                or not 1 <= monthday <= 28
            ):
                raise RunnerSettingsError("Choose a day of the month from 1 to 28.")
        if job in ("weekly", "block", "thresholds") and len(days) > 1:
            raise RunnerSettingsError("The week review runs on one day.")
        spec["days"] = sorted(days)
        out[job] = spec
    return out


def _key_doc(provider: str) -> str:
    return KEY_DOC if provider == "anthropic" else f"{KEY_DOC}_{provider}"


def api_key(rt, provider: str = "anthropic") -> str | None:
    """The stored key, for the model client only. Never return it to a page."""
    value = (store(rt).get(_key_doc(provider)) or {}).get("key")
    return str(value) if value else None


def _audit(rt, action: str, fields: list[str], actor: str) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor=actor,
                action=action,
                detail={"fields": fields},
                target="coach_runner",
                outcome="ok",
            )
        )


def save(rt, changes: dict[str, Any], *, actor: str = "portal") -> RunnerSettings:
    """Save the switch, the cap and/or the schedule. Switching on needs a stored key."""
    unknown = set(changes) - {
        "enabled",
        "monthly_cap_usd",
        "schedule",
        "models",
        "tasks",
        "local_url",
    }
    if unknown:
        raise RunnerSettingsError("Unknown setting.")
    doc = dict(store(rt).get(SETTINGS_DOC) or {})
    if "enabled" in changes:
        enabled = changes["enabled"]
        if not isinstance(enabled, bool):
            raise RunnerSettingsError("Choose on or off.")
        if enabled and not (
            any(api_key(rt, p) for p in PROVIDERS)
            or doc.get("local_url")
            or changes.get("local_url")
        ):
            raise RunnerSettingsError("Add an API key first.")
        doc["enabled"] = enabled
    if "monthly_cap_usd" in changes:
        cap = changes["monthly_cap_usd"]
        if isinstance(cap, bool) or not isinstance(cap, int | float) or not math.isfinite(cap):
            raise RunnerSettingsError("The monthly limit must be an amount in dollars.")
        if not 1 <= cap <= MAX_CAP_USD:
            raise RunnerSettingsError("The monthly limit must be between $1 and $1000.")
        doc["monthly_cap_usd"] = round(float(cap), 2)
    if "models" in changes:
        from vibesynergy.coach_runner import MODEL_KINDS, MODELS

        value = changes["models"]
        if not isinstance(value, dict) or set(value) - set(MODEL_KINDS):
            raise RunnerSettingsError("Unknown setting.")
        if any(model not in MODELS for model in value.values()):
            raise RunnerSettingsError("Choose one of the listed models.")
        doc["models"] = {**_models(doc.get("models")), **value}
    if "local_url" in changes:
        url = str(changes["local_url"] or "").strip()
        if url and not re.match(r"^https?://[^\s]+$", url):
            raise RunnerSettingsError(
                "Give the local server's address, e.g. http://localhost:11434/v1."
            )
        doc["local_url"] = url or None
    if "tasks" in changes:
        value = changes["tasks"]
        if not isinstance(value, dict) or set(value) - set(TASKS):
            raise RunnerSettingsError("Unknown setting.")
        current = _tasks(doc.get("tasks"), _models(doc.get("models")))
        for task, spec in value.items():
            allowed = {"provider", "model"} | ({"on"} if task in EVENT_TASKS else set())
            if not isinstance(spec, dict) or set(spec) - allowed:
                raise RunnerSettingsError("Unknown setting.")
            if "on" in spec and not isinstance(spec["on"], bool):
                raise RunnerSettingsError("Choose on or off.")
            merged = {**current[task], **spec}
            if merged["provider"] not in PROVIDERS:
                raise RunnerSettingsError("Choose one of the listed providers.")
            model = str(merged["model"] or "").strip()
            if not model or len(model) > 100 or " " in model:
                raise RunnerSettingsError("Give the model's name, e.g. gpt-5 or claude-opus-5-5.")
            current[task] = {"provider": merged["provider"], "model": model}
            if task in EVENT_TASKS:
                current[task]["on"] = merged.get("on") is not False
        doc["tasks"] = current
    if "schedule" in changes:
        doc["schedule"] = _check_schedule(changes["schedule"], _schedule(doc.get("schedule")))
    doc["updated_at"] = utc_now().isoformat()
    store(rt).set(SETTINGS_DOC, doc)
    _audit(rt, "coach_runner.settings", sorted(changes), actor)
    return load(rt)


def set_api_key(
    rt, key: str, *, provider: str = "anthropic", actor: str = "portal"
) -> RunnerSettings:
    if provider not in PROVIDERS:
        raise RunnerSettingsError("Choose one of the listed providers.")
    text = (key or "").strip()
    if provider == "anthropic" and not text.startswith(KEY_PREFIX):
        raise RunnerSettingsError("This does not look like a Claude API key (sk-ant-…).")
    if len(text) < 8 or len(text) > 512 or " " in text:
        raise RunnerSettingsError("This does not look like an API key.")
    store(rt).set(_key_doc(provider), {"key": text, "set_at": utc_now().isoformat()})
    _audit(rt, "coach_runner.api_key", [f"api_key:{provider}"], actor)
    return load(rt)


def clear_api_key(rt, *, provider: str = "anthropic", actor: str = "portal") -> RunnerSettings:
    """Remove a key; the runner switches off when no provider is left."""
    store(rt).delete(_key_doc(provider))
    doc = dict(store(rt).get(SETTINGS_DOC) or {})
    left = any(api_key(rt, p) for p in PROVIDERS) or doc.get("local_url")
    if doc.get("enabled") and not left:
        doc["enabled"] = False
        store(rt).set(SETTINGS_DOC, doc)
    _audit(rt, "coach_runner.api_key.clear", [f"api_key:{provider}"], actor)
    return load(rt)


def state(rt) -> dict[str, Any]:
    return dict(store(rt).get(STATE_DOC) or {})


def save_state(rt, value: dict[str, Any]) -> None:
    store(rt).set(STATE_DOC, value)
