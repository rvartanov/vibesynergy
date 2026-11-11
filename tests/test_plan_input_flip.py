"""Plan item order in a capture ignores receipt metadata."""

import json
from datetime import UTC, datetime

from test_evidence import activity, put

from vibesynergy.query import assessments

PLAN_ID = 42
FETCHED = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _calendar(*days):
    return {
        "calendarItems": [
            {"itemType": "event", "id": 99, "date": "2026-09-27", "title": "Race"},
            *[
                {
                    "itemType": "fbtAdaptiveWorkout",
                    "date": day,
                    "title": f"Run {day}",
                    "sportTypeKey": "running",
                    "trainingPlanId": PLAN_ID,
                    "workoutUuid": f"workout-{day}",
                }
                for day in days
            ],
        ]
    }


def _put(rt, dataset, payload, key):
    stored = rt.raw.put(
        source="garmin",
        dataset=dataset,
        item_key=key,
        payload=json.dumps(payload).encode(),
        content_type="application/json",
        fetched_at=FETCHED,
    )
    assert rt.normaliser.normalise(stored.id).error is None


def test_plan_items_have_stable_order_and_ignore_receipt_metadata(runtime, monkeypatch):
    put(runtime, "garmin", activity())
    for rid in runtime.raw.current_ids("garmin"):
        runtime.raw.mark_normalised(rid)
    _put(runtime, "scheduled_workouts", _calendar("2026-09-21", "2026-09-23"), "2026-09")
    original = assessments.plan.plan

    def reordered(*args, **kwargs):
        result = original(*args, **kwargs)
        result["items"] = list(reversed(result["items"]))
        for item in result["items"]:
            item["last_seen_at"] = "different receipt"
            item["etag"] = "different etag"
        return result

    first = assessments.capture(runtime, "garmin:1")
    monkeypatch.setattr(assessments.plan, "plan", reordered)
    second = assessments.capture(runtime, "garmin:1")
    assert second["input_version"] == first["input_version"]
    assert second["inputs"]["plan"] == first["inputs"]["plan"]
