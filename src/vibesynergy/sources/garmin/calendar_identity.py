"""Native identities compatible with the retired mirror; never infer one from title/date."""

from uuid import UUID


def _positive(value):
    return str(value) if type(value) is int and value > 0 else None


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def native_calendar_key(item: dict) -> str | None:
    kind = item.get("itemType")
    if kind == "fbtAdaptiveWorkout":
        plan, workout = _positive(item.get("trainingPlanId")), _uuid(item.get("workoutUuid"))
        return f"garmin:coach:{plan}:{workout}" if plan and workout else None
    if kind == "workout":
        ident = _positive(item.get("id"))
        return f"garmin:workout:{ident}" if ident else None
    if kind == "event":
        if ident := _positive(item.get("id")):
            return f"garmin:event:{ident}"
        ident = _uuid(item.get("shareableEventUuid"))
        return f"garmin:event:uuid:{ident}" if ident else None
    return None
