"""Plan horizon guard."""

from __future__ import annotations

from datetime import date

from vibesynergy.query import horizon

TODAY = date(2026, 11, 5)


def _fake(monkeypatch, items):
    monkeypatch.setattr(
        horizon.plan_q,
        "plan",
        lambda *a, **k: {"items": items, "not_in_plan": []},
    )


def _item(day, title, sport, copies):
    return {
        "source": "intervals_icu",
        "kind": "plan_workout",
        "date": day,
        "title": title,
        "sport": sport,
        "garmin_copies": [{"match": m} for m in copies],
    }


def test_missing_and_double_sessions_are_reported(monkeypatch) -> None:
    _fake(
        monkeypatch,
        [
            _item(TODAY, "Duurloop Z2 40'", "running", ["name"]),
            _item(date(2026, 11, 7), "Zwemmen duur 40'", "swimming", []),
            _item(date(2026, 11, 8), "Lange rit", "cycling", ["name", "name"]),
            _item(date(2026, 11, 8), "Kracht AA 40'", "strength_training", ["name", "name"]),
            _item(date(2026, 11, 8), "Brick", "cycling", ["plan_tag", "name"]),
        ],
    )
    out = horizon.status(None, TODAY)
    assert out["level"] == "warn" and out["checked"] == 5
    assert out["missing"] == [{"date": "2026-11-07", "title": "Zwemmen duur 40'"}]
    assert out["duplicates"] == [{"date": "2026-11-08", "title": "Lange rit"}]


def test_all_on_garmin_is_ok(monkeypatch) -> None:
    _fake(monkeypatch, [_item(TODAY, "Pilates 60'", "strength_training", ["name"])])
    assert horizon.status(None, TODAY)["level"] == "ok"


def test_vibesynergy_sports_are_checked_further_and_may_have_two_copies(monkeypatch) -> None:
    _fake(
        monkeypatch,
        [
            _item(date(2026, 11, 12), "Duurloop", "running", []),  # day 8: VibeSynergy's window
            _item(date(2026, 11, 12), "Zwift rit", "virtual_ride", []),  # past the push window
            _item(date(2026, 11, 6), "Zwemmen", "swimming", ["name", "name"]),  # parallel week
        ],
    )
    out = horizon.status(None, TODAY, 9, ("run", "swim"))
    assert out["missing"] == [{"date": "2026-11-12", "title": "Duurloop"}]
    assert out["duplicates"] == [] and out["checked"] == 2


def test_compare_tells_both_copies_apart(monkeypatch) -> None:
    # before the intervals.icu push goes off per sport, which copy is on the Garmin.
    from datetime import date

    from vibesynergy.query import horizon
    from vibesynergy.query import plan as plan_q

    day = date(2026, 11, 10)
    items = [
        {
            "source": plan_q.INTERVALS,
            "kind": "workout",
            "date": day,
            "sport": "running",
            "title": "Duurloop Z2 30'",
            "garmin_copies": [{"title": "Duurloop Z2 30'"}, {"title": "🏃 Duurloop Z2 30'"}],
        },
        {
            "source": plan_q.INTERVALS,
            "kind": "workout",
            "date": day,
            "sport": "cycling",
            "title": "Lange rit",
            "garmin_copies": [{"title": "🚴 Lange rit"}, {"title": "Brick handmade"}],
        },
        {
            # Since 7-11 VibeSynergy's copy has no emoji: the delivery ledger says it is ours.
            "source": plan_q.INTERVALS,
            "kind": "workout",
            "date": day,
            "sport": "strength_training",
            "title": "Kracht MT-A",
            "garmin_copies": [{"title": "Kracht MT-A", "source_id": "workout:42"}],
        },
    ]
    monkeypatch.setattr(plan_q, "plan", lambda *a, **k: {"items": items})
    monkeypatch.setattr(horizon, "_delivered", lambda engine: {"workout:42"})
    rows = horizon.compare(None, day, 3)
    assert [(r["intervals"], r["vibesynergy"]) for r in rows] == [
        (True, True),
        (False, True),
        (False, True),
    ]
    assert rows[1]["other"] == ["Brick handmade"]


def test_an_optional_session_is_not_missing() -> None:
    # A choice day ("Vrijdag optie: ...", "Korte rit (optioneel)") need not be on the watch.
    from vibesynergy.query import horizon

    assert horizon.optional("Vrijdag optie: rust, pilates of zwemmen")
    assert horizon.optional("Korte rit Z1-Z2 30' (optioneel)")
    assert not horizon.optional("Duurloop Z2 30'")
