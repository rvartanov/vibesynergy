"""Withings source: every dataset of the design through the official API.

Configuration (``[sources.withings]``)::

    enabled = true
    request_interval = 0.6        # seconds between calls (limit: 120 per minute)

``vibesynergy auth withings`` asks for the app's Client ID and Secret once, then does the
consent step. Instant updates (webhooks) are set up with ``vibesynergy webhooks subscribe``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window
from vibesynergy.sources import oauth
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.sources.withings import mappers
from vibesynergy.sources.withings.client import AUTHORIZE_URL, SCOPES, WithingsClient

ACTIVITY_FIELDS = (
    "steps,distance,elevation,soft,moderate,intense,active,calories,totalcalories,"
    "hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3"
)
INTRADAY_FIELDS = "steps,elevation,calories,distance,stroke,pool_lap,duration,heart_rate,spo2_auto"
WORKOUT_FIELDS = (
    "calories,intensity,manual_distance,manual_calories,hr_average,hr_min,hr_max,"
    "hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3,pause_duration,algo_pause_duration,"
    "spo2_average,steps,distance,elevation,pool_laps,strokes,pool_length"
)
SLEEP_SUMMARY_FIELDS = (
    "breathing_disturbances_intensity,deepsleepduration,durationtosleep,durationtowakeup,"
    "hr_average,hr_max,hr_min,lightsleepduration,remsleepduration,rr_average,rr_max,rr_min,"
    "sleep_score,snoring,snoringepisodecount,wakeupcount,wakeupduration,total_sleep_time,"
    "total_timeinbed,sleep_efficiency,sleep_latency,wakeup_latency,waso,nb_rem_episodes,"
    "apnea_hypopnea_index,out_of_bed_count,night_events,mvt_score_avg,mvt_active_duration"
)
SLEEP_SERIES_FIELDS = "hr,rr,snoring,sdnn_1,rmssd,hrv_quality,mvt_score,chest_movement_rate"

REFETCH_DAYS = 7


def _spec(name: str, kind: DatasetKind, description: str, **kw: Any) -> DatasetSpec:
    if kind is not DatasetKind.SNAPSHOT:
        kw.setdefault("initial_days", 31 if kind is DatasetKind.RANGE else 7)
        kw.setdefault("refetch_days", REFETCH_DAYS)
    return DatasetSpec(name, kind, description, **kw)


DATASETS = [
    _spec(
        "measures",
        DatasetKind.RANGE,
        "All body measures: weight, composition, BP, SpO2, "
        "temperature, PWV, vascular age, nerve health, ECG intervals (every meastype)",
    ),
    _spec("activity", DatasetKind.RANGE, "Daily activity: steps, distance, calories, HR zones"),
    _spec("intraday_activity", DatasetKind.DAILY, "Intraday steps, heart rate, SpO2"),
    _spec("workouts", DatasetKind.RANGE, "Workouts"),
    _spec(
        "sleep_summary",
        DatasetKind.RANGE,
        "Sleep summaries: score, stages, HR, breathing, snoring, apnoea index",
    ),
    _spec("sleep_series", DatasetKind.DAILY, "Sleep stages and HR/RR/snoring during the night"),
    _spec("heart", DatasetKind.RANGE, "ECG recordings (with raw signal) and BPM readings"),
    _spec("devices", DatasetKind.SNAPSHOT, "Devices: model, battery, firmware"),
    _spec("goals", DatasetKind.SNAPSHOT, "Goals"),
]


class WithingsSource(Source):
    id = "withings"
    name = "Withings"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._client: WithingsClient | None = None

    @property
    def client(self) -> WithingsClient:
        if self._client is None:
            interval = float(self.ctx.config.get("request_interval", 0.6))
            from vibesynergy.sources.http_timeout import http_timeout_seconds

            self._client = WithingsClient(
                self.ctx.secrets,
                interval=interval,
                timeout=http_timeout_seconds(self.ctx.settings, self.id),
            )
        return self._client

    def datasets(self) -> Sequence[DatasetSpec]:
        return DATASETS

    def mappers(self) -> Mapping[str, Mapper]:
        return mappers.build(self.ctx.settings.timezone)

    def authenticate(self) -> None:
        redirect = oauth.redirect_uri(self.ctx.settings, self.id)
        app = oauth.ask_app(self.ctx.secrets, "Withings")
        code = oauth.paste_back_code(
            AUTHORIZE_URL,
            {
                "response_type": "code",
                "client_id": app.client_id,
                "scope": SCOPES,
                "redirect_uri": redirect,
            },
            handoff=SecretStore(self.ctx.settings.secrets_dir, oauth.HANDOFF),
        )
        self.client.exchange_code(app, code, redirect)

    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]:
        c = self.client
        name = dataset.name
        if name == "devices":
            yield FetchedItem("current", c.call("/v2/user", "getdevice"))
            return
        if name == "goals":
            yield FetchedItem("current", c.call("/v2/user", "getgoals"))
            return
        assert window is not None
        if dataset.kind is DatasetKind.DAILY:
            for day in window.days():
                yield self._daily(name, day)
            return
        for month in window.months():
            end = min(month.end, window.end)
            payload = self._month(name, month.start, end)
            yield FetchedItem(
                month.start.strftime("%Y-%m"),
                payload,
                request={"dataset": name, "start": str(month.start), "end": str(end)},
                window=Window(month.start, end),
            )

    # Per dataset ------------------------------------------------------------------------

    def _month(self, name: str, start: date, end: date) -> dict[str, Any]:
        c = self.client
        ymd = {"startdateymd": start.isoformat(), "enddateymd": end.isoformat()}
        if name == "measures":
            return c.paged(
                "/measure", "getmeas", "measuregrps", category=1, **self._epochs(start, end)
            )
        if name == "activity":
            return c.paged(
                "/v2/measure", "getactivity", "activities", data_fields=ACTIVITY_FIELDS, **ymd
            )
        if name == "workouts":
            return c.paged(
                "/v2/measure", "getworkouts", "series", data_fields=WORKOUT_FIELDS, **ymd
            )
        if name == "sleep_summary":
            return c.paged(
                "/v2/sleep", "getsummary", "series", data_fields=SLEEP_SUMMARY_FIELDS, **ymd
            )
        if name == "heart":
            listing = c.paged("/v2/heart", "list", "series", **self._epochs(start, end))
            signals = {}
            for entry in listing.get("series", []):
                signal_id = (entry.get("ecg") or {}).get("signalid")
                if signal_id:
                    signals[str(signal_id)] = c.call("/v2/heart", "get", signalid=signal_id)
            return {**listing, "signals": signals}
        raise KeyError(name)

    def _daily(self, name: str, day: date) -> FetchedItem:
        c = self.client
        if name == "intraday_activity":
            start, end = self._local(day, time()), self._local(day + timedelta(days=1), time())
            payload = c.call(
                "/v2/measure",
                "getintradayactivity",
                startdate=start,
                enddate=end - 1,
                data_fields=INTRADAY_FIELDS,
            )
        else:  # sleep_series: the night ending on this day, noon to noon
            start = self._local(day - timedelta(days=1), time(12))
            end = self._local(day, time(12))
            payload = c.call(
                "/v2/sleep",
                "get",
                startdate=start,
                enddate=end - 1,
                data_fields=SLEEP_SERIES_FIELDS,
            )
        return FetchedItem(
            day.isoformat(),
            payload,
            request={"dataset": name, "date": day.isoformat()},
            window=Window(day, day),
        )

    def _local(self, day: date, at: time) -> int:
        tz = ZoneInfo(self.ctx.settings.timezone)
        return int(datetime.combine(day, at, tzinfo=tz).timestamp())

    def _epochs(self, start: date, end: date) -> dict[str, int]:
        return {
            "startdate": self._local(start, time()),
            "enddate": self._local(end + timedelta(days=1), time()) - 1,
        }
