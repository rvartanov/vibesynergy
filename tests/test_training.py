def test_vibesynergy_load_from_own_data_when_chosen(runtime) -> None:
    # [user] load_source = "vibesynergy" makes VibeSynergy's own model the load everywhere.
    from datetime import UTC, date, datetime

    from vibesynergy.core.timeutil import Window
    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import thresholds, training

    day = date(2026, 11, 3)
    stored = runtime.raw.put(
        source="garmin", dataset="t", item_key="v", payload=b"v", content_type="text/plain"
    )
    ride = Activity(
        "r1",
        datetime(2026, 11, 3, 9, tzinfo=UTC),
        day,
        "cycling",
        moving_s=3600.0,
        norm_power=200.0,
    )
    with runtime.engine.begin() as conn:
        write_records(conn, [ride], source="garmin", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)
    thresholds.set_value(runtime.engine, sport="bike", metric="ftp", value=200.0, date_set=day)
    span = Window(day, day)
    daily = training.daily_vibesynergy(runtime.engine, span)
    assert round(daily[day]) == 100  # an hour at FTP is 100
    out = training.training_load(
        runtime.engine,
        span,
        load="intervals",
        athlete={"load_source": "vibesynergy"},
        warmup_days=0,
    )
    assert out["source"] == "vibesynergy" and out["columns"][7] == "source"
    assert out["rows"][-1][1] == 100.0 and out["current"]["form_zone"]
    # The coach, the assessments and the rhythm read hr_profile (8-11: a None crashed them).
    assert "max_hr" in out["hr_profile"]
    assert (
        training.training_load(runtime.engine, span, load="intervals", warmup_days=0)["load"]
        == "intervals"
    )
