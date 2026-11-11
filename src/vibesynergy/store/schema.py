"""Database schema (SQLAlchemy Core). Changes go through Alembic migrations.

Bookkeeping tables (raw archive, sync state, jobs, audit) and the canonical tables. Every canonical
row carries ``source`` and the ``raw_item_id`` it was
mapped from. Per-second streams and intraday wellness samples live in Parquet, not here.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from vibesynergy.store.types import UTCDateTime


def _provenance() -> list:
    return [
        Column("raw_item_id", String, ForeignKey("raw_item.id"), nullable=False, index=True),
        Column("updated_at", UTCDateTime, nullable=False),
    ]


metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

# One row per version of a logical item a source returned (a day of sleep, an activity,
# a profile snapshot). The payload itself lives in the blob store under ``sha256``.
raw_item = Table(
    "raw_item",
    metadata,
    Column("id", String, primary_key=True),
    Column("source", String, nullable=False),
    Column("dataset", String, nullable=False),
    Column("item_key", String, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("content_type", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("request", JSON, nullable=False),
    Column("window_start", Date),
    Column("window_end", Date),
    Column("fetched_at", UTCDateTime, nullable=False),
    Column("last_seen_at", UTCDateTime, nullable=False),
    Column("supersedes", String, ForeignKey("raw_item.id")),
    Column("is_current", Boolean, nullable=False),
    Column("normalised_at", UTCDateTime),
    Column("normalise_error", Text),
    Index(
        "uq_raw_item_current",
        "source",
        "dataset",
        "item_key",
        unique=True,
        sqlite_where=text("is_current = 1"),
    ),
    Index("ix_raw_item_sha256", "sha256"),
)

# Sync bookkeeping per source dataset: what has been collected and what went wrong.
dataset_state = Table(
    "dataset_state",
    metadata,
    Column("source", String, nullable=False),
    Column("dataset", String, nullable=False),
    Column("last_attempt_at", UTCDateTime),
    Column("last_success_at", UTCDateTime),
    Column("last_error", Text),
    Column("synced_from", Date),
    Column("synced_until", Date),
    Column("cursor", JSON),
    PrimaryKeyConstraint("source", "dataset"),
)

job = Table(
    "job",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String, nullable=False),  # queued | running | done | dead
    Column("priority", Integer, nullable=False),
    Column("run_after", UTCDateTime, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("dedupe_key", String),
    Column("created_at", UTCDateTime, nullable=False),
    Column("started_at", UTCDateTime),
    Column("finished_at", UTCDateTime),
    Column("locked_by", String),
    Column("last_error", Text),
    Index("ix_job_status_run_after", "status", "run_after"),
    Index(
        "uq_job_active_dedupe_key",
        "dedupe_key",
        unique=True,
        sqlite_where=text("status IN ('queued', 'running')"),
    ),
)

# Canonical daily scalar metrics (steps, resting HR, sleep score, HRV, …), one value per
# day, metric and source. Metric IDs follow the catalogue in docs/MCP.md §2.1.
daily_metric = Table(
    "daily_metric",
    metadata,
    Column("subject_id", Integer, nullable=False),
    Column("date", Date, nullable=False),
    Column("metric", String, nullable=False),
    Column("source", String, nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String, nullable=False),
    Column("raw_item_id", String, ForeignKey("raw_item.id"), nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("subject_id", "date", "metric", "source"),
    Index("ix_daily_metric_metric_date", "metric", "date"),
    Index("ix_daily_metric_raw_item_id", "raw_item_id"),
)

activity = Table(
    "activity",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("subject_id", Integer, nullable=False),
    Column("start_utc", UTCDateTime, nullable=False),
    Column("tz_offset_s", Integer),
    Column("local_date", Date, nullable=False),
    Column("sport", String, nullable=False),
    Column("sub_sport", String),
    Column("name", String),
    Column("elapsed_s", Float),
    Column("moving_s", Float),
    Column("distance_m", Float),
    Column("ascent_m", Float),
    Column("avg_hr", Float),
    Column("max_hr", Float),
    Column("avg_power", Float),
    Column("norm_power", Float),
    Column("avg_speed", Float),
    Column("calories", Float),
    Column("training_load", Float),
    Column("aerobic_te", Float),
    Column("anaerobic_te", Float),
    Column("device", String),
    Column("fit_id", String, index=True),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "source_id"),
    Index("ix_activity_local_date", "local_date"),
    Index("ix_activity_start_utc", "start_utc"),
)

# First observed stages for a source activity. Rebuilds and later corrections retain them.
activity_latency = Table(
    "activity_latency",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("source_event_at", UTCDateTime),
    Column("received_at", UTCDateTime),
    Column("queued_at", UTCDateTime),
    Column("fetch_started_at", UTCDateTime),
    Column("fetched_at", UTCDateTime),
    Column("processed_at", UTCDateTime),
    Column("dashboard_at", UTCDateTime),
    PrimaryKeyConstraint("source", "source_id"),
    Index("ix_activity_latency_finished_at", "finished_at"),
)

activity_lap = Table(
    "activity_lap",
    metadata,
    Column("source", String, nullable=False),
    Column("activity_source_id", String, nullable=False),
    Column("lap_index", Integer, nullable=False),
    Column("start_utc", UTCDateTime),
    Column("elapsed_s", Float),
    Column("moving_s", Float),
    Column("distance_m", Float),
    Column("avg_hr", Float),
    Column("max_hr", Float),
    Column("avg_power", Float),
    Column("avg_speed", Float),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "activity_source_id", "lap_index"),
)

activity_zone_time = Table(
    "activity_zone_time",
    metadata,
    Column("source", String, nullable=False),
    Column("activity_source_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("zone", Integer, nullable=False),
    Column("seconds", Float, nullable=False),
    Column("low", Float),
    *_provenance(),
    PrimaryKeyConstraint("source", "activity_source_id", "kind", "zone"),
)

sleep_session = Table(
    "sleep_session",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("subject_id", Integer, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("start_utc", UTCDateTime),
    Column("end_utc", UTCDateTime),
    Column("duration_s", Float),
    Column("deep_s", Float),
    Column("light_s", Float),
    Column("rem_s", Float),
    Column("awake_s", Float),
    Column("score", Float),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "source_id"),
    Index("ix_sleep_session_local_date", "local_date"),
)

sleep_stage = Table(
    "sleep_stage",
    metadata,
    Column("source", String, nullable=False),
    Column("session_source_id", String, nullable=False),
    Column("start_utc", UTCDateTime, nullable=False),
    Column("end_utc", UTCDateTime, nullable=False),
    Column("stage", String, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "session_source_id", "start_utc"),
)

body_measurement = Table(
    "body_measurement",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("metric", String, nullable=False),
    Column("subject_id", Integer, nullable=False),
    Column("measured_at", UTCDateTime, nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String, nullable=False),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "source_id", "metric"),
    Index("ix_body_measurement_metric_measured_at", "metric", "measured_at"),
)

# Training zones with history: the zones valid on a date are the row with the latest
# valid_from on or before it (per kind and sport).
zone_set = Table(
    "zone_set",
    metadata,
    Column("source", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("sport", String, nullable=False),
    Column("valid_from", Date, nullable=False),
    Column("subject_id", Integer, nullable=False),
    Column("boundaries", JSON, nullable=False),
    Column("threshold", Float),
    Column("max_value", Float),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "kind", "sport", "valid_from"),
)

# The training calendar (coaching-plan workouts, scheduled workouts, events such as races) and
# the coaching plans themselves; a re-planned month replaces that month's rows.
planned_item = Table(
    "planned_item",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("subject_id", Integer, nullable=False),
    Column("date", Date, nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("title", String),
    Column("sport", String),
    Column("plan_id", String),
    Column("is_race", Boolean, nullable=False),
    Column("primary", Boolean, nullable=False),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "source_id"),
)

# Chosen race priority (A, B or C). Calendar sync rewrites planned_item and does not
# touch this table, so a choice survives a re-sync. No row means unset, except a Garmin
# primary event, which is read as A.
race_priority = Table(
    "race_priority",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("priority", String, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("source", "source_id"),
)

training_plan = Table(
    "training_plan",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("subject_id", Integer, nullable=False),
    Column("name", String, nullable=False),
    Column("start_date", Date),
    Column("end_date", Date),
    Column("status", String),
    Column("plan_type", String),
    Column("level", String),
    Column("extra", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("source", "source_id"),
)

# One row per decoded FIT file. The same file can arrive through several routes (API
# download, account export, a folder import); fit_id is identical for all copies.
fit_file = Table(
    "fit_file",
    metadata,
    Column("source", String, nullable=False),
    Column("fit_id", String, nullable=False, index=True),
    Column("file_type", String, nullable=False),
    Column("time_created", UTCDateTime),
    Column("manufacturer", String),
    Column("product", String),
    Column("serial_number", Integer),
    Column("start_utc", UTCDateTime),
    Column("sport", String),
    Column("source_activity_id", String),
    Column("messages", JSON, nullable=False),
    *_provenance(),
    PrimaryKeyConstraint("raw_item_id", "fit_id"),
)

# Derived: rebuilt by vibesynergy.derive from the tables above.

# Which activities from different sources are the same session. group_id is the
# "source:source_id" key of the group's primary activity.
activity_link = Table(
    "activity_link",
    metadata,
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("group_id", String, nullable=False, index=True),
    Column(
        "rule",
        String,
        nullable=False,
    ),  # primary | same_id | fit_file | external_id | filename | time
    PrimaryKeyConstraint("source", "source_id"),
)

# One row per real-world activity, fields chosen by the best-source rules.
activity_unified = Table(
    "activity_unified",
    metadata,
    Column("group_id", String, primary_key=True),
    Column("subject_id", Integer, nullable=False),
    Column("start_utc", UTCDateTime, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("tz_offset_s", Integer),
    Column("sport", String, nullable=False),
    Column("name", String),
    Column("kind", String),  # NULL: personal workout; coaching: teaching a session
    Column("purpose", String),  # training | recovery | leisure | coached_by_me | race
    Column("elapsed_s", Float),
    Column("moving_s", Float),
    Column("distance_m", Float),
    Column("ascent_m", Float),
    Column("avg_hr", Float),
    Column("max_hr", Float),
    Column("avg_power", Float),
    Column("norm_power", Float),
    Column("avg_speed", Float),
    Column("calories", Float),
    Column("training_load", Float),
    Column("stream_key", String),  # parquet/streams/<stream_key>.parquet
    Column("primary_source", String, nullable=False),
    Column("sources", JSON, nullable=False),  # ["garmin:123", "strava:456", …]
    Column("updated_at", UTCDateTime, nullable=False),
    Index("ix_activity_unified_local_date", "local_date"),
)

# Source identities survive a rebuild of activity_unified and a change of primary source.
coaching_override = Table(
    "coaching_override",
    metadata,
    Column("source_ref", String, primary_key=True),
    Column("is_coaching", Boolean, nullable=False),
)

coaching_rule = Table(
    "coaching_rule",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("match_type", String, nullable=False),
    Column("pattern", String, nullable=False),
)

activity_purpose_override = Table(
    "activity_purpose_override",
    metadata,
    Column("source_ref", String, primary_key=True),
    Column("purpose", String),
    Column("reason", String),
)

equipment_location = Table(
    "equipment_location",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String, nullable=False),
    Column("name", String, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    UniqueConstraint("key", name="uq_equipment_location_key"),
)

equipment_item = Table(
    "equipment_item",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "location_id",
        Integer,
        ForeignKey("equipment_location.id"),
        nullable=False,
    ),
    Column("item", String, nullable=False),
    Column("category", String, nullable=False),
    Column("status", String, nullable=False),
    Column("source", String),
    Column("last_verified", Date),
    Column("gear_id", String),
    Column("garmin_catalogue_key", String),
    Column("notes", Text),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    UniqueConstraint(
        "location_id",
        "item",
        "category",
        name="uq_equipment_item_location_item_category",
    ),
    Index("ix_equipment_item_location_status", "location_id", "status"),
)

athlete_threshold = Table(
    "athlete_threshold",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sport", String, nullable=False),
    Column("metric", String, nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String, nullable=False),
    Column("method", String, nullable=False),
    Column("source_device", String),
    Column("confidence", Float),
    Column("date_set", Date),
    Column("next_review", Date),
    Column("status", String, nullable=False),
    Column("history", JSON, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column(
        "superseded_by",
        Integer,
        ForeignKey("athlete_threshold.id"),
    ),
    Index("ix_athlete_threshold_sport_metric_status", "sport", "metric", "status"),
    Index(
        "uq_athlete_threshold_current_sport_metric",
        "sport",
        "metric",
        unique=True,
        sqlite_where=text("status = 'current'"),
    ),
)

# One value per day and metric, taken from the best source that has it.
daily_unified = Table(
    "daily_unified",
    metadata,
    Column("subject_id", Integer, nullable=False),
    Column("date", Date, nullable=False),
    Column("metric", String, nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String, nullable=False),
    Column("source", String, nullable=False),
    PrimaryKeyConstraint("subject_id", "date", "metric"),
    Index("ix_daily_unified_metric_date", "metric", "date"),
)

# OAuth 2.1 for the MCP endpoint (single user; approval by passphrase). Codes and tokens
# are stored only as SHA-256 hashes, so a copy of the database grants no access.
oauth_client = Table(
    "oauth_client",
    metadata,
    Column("client_id", String, primary_key=True),
    Column("info", JSON, nullable=False),  # the registration (RFC 7591), as returned
    Column("created_at", UTCDateTime, nullable=False),
    Column("approved_at", UTCDateTime),  # first successful passphrase approval
    Column("revoked_at", UTCDateTime),
)

oauth_grant = Table(
    "oauth_grant",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("kind", String, nullable=False),  # code | access | refresh
    Column("client_id", String, ForeignKey("oauth_client.client_id"), nullable=False),
    Column("data", JSON, nullable=False),  # scopes, resource, PKCE challenge, redirect
    Column("expires_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("used_at", UTCDateTime),  # codes and refresh tokens are single use
    Index("ix_oauth_grant_client_id", "client_id"),
)

# Personal access tokens for MCP. Only a hash is stored; the token is shown once.
# expires_at is required for any token the loader will accept (there is no "never").
# scopes holds the granted names; the legacy value ["vibesynergy"] means every scope except publish.
mcp_pat = Table(
    "mcp_pat",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False, unique=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("token_prefix", String, nullable=False),  # first chars, for list/revoke hints
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_used_at", UTCDateTime),
    Column("revoked_at", UTCDateTime),
    Column("expires_at", UTCDateTime),
    Column("scopes", JSON),
    Column("display_name", String),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", UTCDateTime, nullable=False),
    Column("actor", String, nullable=False),
    Column("action", String, nullable=False),
    Column("detail", JSON, nullable=False),
    Column("client_id", String),
    Column("author", String),
    Column("scope", String),
    Column("target", String),
    Column("outcome", String),
    Column("ms", Integer),
)

# Secret links that show (parts of) the portal to someone without tailnet access. Only a hash
# of the code is kept; the link itself is shown once when it is made.
share_link = Table(
    "share_link",
    metadata,
    Column("id", String, primary_key=True),  # short public id, for revoking and listing
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("title", String, nullable=False),  # who it is for
    Column("pages", JSON, nullable=False),  # portal pages it shows
    Column("can_write", Boolean, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime),
    Column("revoked_at", UTCDateTime),
    Column("last_used_at", UTCDateTime),
    Column("uses", Integer, nullable=False),
)

# Your own diary: notes for a day, a span of days or one
# activity, with tags (sick, travel, alcohol, …); a short morning check-in; and an injury log
# with a pain score per day. Entered by you (through Claude, the portal or the CLI), or
# imported from a source (e.g. Strava descriptions), which then sets ``source_ref``.
note = Table(
    "note",
    metadata,
    Column("id", String, primary_key=True),  # note_…
    Column("subject_id", Integer, nullable=False),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),  # = start_date for a single day
    Column("activity_id", String),  # optional: the activity it is about
    Column("text", Text, nullable=False),
    Column("tags", JSON, nullable=False),
    Column("source", String, nullable=False),  # claude | portal | cli | garmin | strava
    Column("source_ref", String),  # imported notes: the source's id, so re-imports update
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Index("ix_note_start_date_end_date", "start_date", "end_date"),
    Index("ix_note_source_source_ref", "source", "source_ref", unique=True),
)

# Morning check-in, 1 (bad) to 5 (good) except soreness and stress, where 1 is none/low.
checkin = Table(
    "checkin",
    metadata,
    Column("subject_id", Integer, nullable=False),
    Column("date", Date, nullable=False),
    Column("sleep_quality", Integer),
    Column("soreness", Integer),
    Column("mood", Integer),
    Column("stress", Integer),
    Column("motivation", Integer),
    Column("note", Text),
    Column("updated_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("subject_id", "date"),
)

injury = Table(
    "injury",
    metadata,
    Column("id", String, primary_key=True),  # inj_…
    Column("subject_id", Integer, nullable=False),
    Column("body_part", String, nullable=False),  # see vibesynergy.query.journal.BODY_PARTS
    Column("side", String, nullable=False),  # left | right | both | none
    Column("kind", String, nullable=False),  # niggle | pain | injury
    Column("title", String, nullable=False),
    Column("onset", Date, nullable=False),
    Column("status", String, nullable=False),  # active | improving | resolved
    Column("resolved_on", Date),
    Column("notes", Text, nullable=False),  # diagnosis, physio advice, return-to-run steps
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

# One entry per injury and day: the worst pain of that day.
injury_log = Table(
    "injury_log",
    metadata,
    Column("injury_id", String, ForeignKey("injury.id"), nullable=False),
    Column("date", Date, nullable=False),
    Column("pain", Integer, nullable=False),  # 0-10
    Column("timing", String, nullable=False),  # during | after | morning | rest
    Column("affects_movement", Boolean, nullable=False),
    Column("note", Text),
    Column("updated_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("injury_id", "date"),
)

# History fills (the "filler"): one row per run. An API run fetches month by month through the
# job queue, oldest first; an archive run imports an account export (Garmin, Strava) in a
# thread. Progress lives here, so a restart continues where it stopped.
history_fill = Table(
    "history_fill",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String, nullable=False),
    Column("method", String, nullable=False),  # api | archive
    # running | waiting (API limit) | paused | done | stuck | cancelled | failed
    Column("status", String, nullable=False),
    Column("start", Date),  # api: the period to fill
    Column("end", Date),
    Column("next_month", Date),  # api: the first day of the next month to fetch
    Column("datasets", JSON),  # api: null = every history dataset of the source
    Column("partial", JSON, nullable=False),  # months where some datasets failed ("YYYY-MM")
    Column("retry", JSON, nullable=False),  # months being fetched again
    Column("retried", Boolean, nullable=False),
    Column("failures", Integer, nullable=False),  # failed attempts in a row
    Column("wait_until", UTCDateTime),
    Column("last_error", Text),
    Column("file", String),  # archive: the zip
    Column("done", Integer, nullable=False),  # months (api) or files (archive) done
    Column("total", Integer),
    Column("message", Text),
    Column("busy_s", Float, nullable=False),  # time spent working, for the ETA
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
)

# Versioned user-authored coaching context and policy, scoped for future athlete profiles.
coach_head = Table(
    "coach_head",
    metadata,
    Column("subject_id", Integer, primary_key=True),
    Column("kind", String, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("active_version", Integer),
)
coach_revision = Table(
    "coach_revision",
    metadata,
    Column("subject_id", Integer, primary_key=True),
    Column("kind", String, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("schema_version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("effective", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("actor", String, nullable=False),
    Column("reason", Text, nullable=False),
)
coach_event = Table(
    "coach_event",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subject_id", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("action", String, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("actor", String, nullable=False),
    Column("reason", Text, nullable=False),
)

publication_ledger = Table(
    "publication_ledger",
    metadata,
    Column("source", String, primary_key=True),
    Column("source_id", String, primary_key=True),
    Column("assessment_id", String, nullable=False),
    Column("preview_version", String(64), nullable=False),
    Column("owned_block", Text, nullable=False),
    Column("base_description", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("state", String, nullable=False),  # pending | verified | uncertain
    Column("updated_at", UTCDateTime, nullable=False),
)
publication_mirror = Table(
    "publication_mirror",
    metadata,
    Column("group_id", String, primary_key=True),
    Column("strava_id", String, nullable=False),
    Column("garmin_id", String),
    Column("language", String, nullable=False),
    Column("state", String, nullable=False),  # active | expired | removed | conflict
    Column("first_published_at", UTCDateTime, nullable=False),
    Column("watch_until", UTCDateTime, nullable=False),
    Column("next_check_at", UTCDateTime, nullable=False),
    Column("last_checked_at", UTCDateTime),
    Column("last_result", JSON),
    Column("last_error", Text),
    Column("garmin_original", Text),  # Garmin's description before the first mirror write
    Column("garmin_original_title", Text),  # and its title before the first title write
    Column("updated_at", UTCDateTime, nullable=False),
)
publication_enrichment = Table(
    "publication_enrichment",
    metadata,
    Column("identity_id", String, primary_key=True),
    Column("plugin_id", String, primary_key=True),
    Column("version", String, nullable=False),
    Column("section", Text, nullable=False),
    Column("provenance", JSON, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)
# Owned Garmin → CalDAV mirrors. Personal events are never rows here. Titles and
# calendar bodies are not stored; fingerprint is a hash of the date and summary.
calendar_mirror = Table(
    "calendar_mirror",
    metadata,
    Column("origin_id", String, nullable=False),
    Column("calendar_url", String, nullable=False),
    Column("ledger_id", String, nullable=False),
    Column("intent_id", String, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("account_id", String, nullable=False),
    Column("uid", String, nullable=False),
    Column("href", String, nullable=False),
    Column("etag", String),
    Column("sequence", Integer, nullable=False),
    Column("fingerprint", String),
    Column("pending_write", JSON),
    Column("event_date", Date),
    Column("state", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("last_action", String),
    Column("last_error", String),
    Column("updated_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("origin_id", "calendar_url"),
    Index("uq_calendar_mirror_uid", "calendar_url", "uid", unique=True),
)

# Strength and pilates sessions VibeSynergy delivered to Garmin with exercise steps, by
# intervals.icu event. fingerprint is the session as built; replaced lists the scheduled ids
# of intervals.icu's empty copies VibeSynergy took off the calendar.
garmin_strength_delivery = Table(
    "garmin_strength_delivery",
    metadata,
    Column("icu_event_id", String, primary_key=True),
    Column("event_date", Date),
    Column("fingerprint", String),
    Column("workout_id", Integer),
    Column("scheduled_id", Integer),
    Column("state", String),
    Column("replaced", JSON),
    Column("last_error", Text),
    Column("updated_at", UTCDateTime, nullable=False),
)

coach_decision_log = Table(
    "coach_decision_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("subject_id", Integer, nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("kind", String, nullable=False),
    Column("author", String, nullable=False),
    Column("entry_type", String, nullable=False, server_default="note"),
    Column("idempotency_key", String),
    Column("for_date", Date),
    Column("valid_until", UTCDateTime),
    Column("plan_version_id", String),
    Column("session_id", String),
    Column("text", Text, nullable=False),
    Column("meta", JSON, nullable=False),
    Index("ix_coach_decision_log_subject_at", "subject_id", "at"),
    Index(
        "uq_coach_decision_log_idempotency_key",
        "idempotency_key",
        unique=True,
        sqlite_where=text("idempotency_key IS NOT NULL"),
    ),
)

# One stored fit of a personal model. ``diff`` is the parameter change against the
# chronologically previous run of the same model_id, computed when the row is written.
# Inserting an older run does not rewrite diffs already stored on later runs.
model_run = Table(
    "model_run",
    metadata,
    Column("id", String, primary_key=True),
    Column("model_id", String, nullable=False),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime, nullable=False),
    Column("params", JSON, nullable=False),
    Column("report", Text, nullable=False),
    Column("diff", JSON),
    Column("created_by", String, nullable=False),
    Index("ix_model_run_model_id_started_at", "model_id", "started_at"),
)

# A forecast attached to one coach entry. ``for_date`` is the day the outcome belongs to
# (the entry day plus ``horizon_days``). actual/error/scored_at stay empty until that
# day's daily_unified value exists. error = actual - predicted, filled once.
coach_prediction = Table(
    "coach_prediction",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entry_id", Integer, ForeignKey("coach_decision_log.id"), nullable=False),
    Column("for_date", Date, nullable=False),
    Column("horizon_days", Integer, nullable=False),
    Column("metric", String, nullable=False),
    Column("predicted", Float, nullable=False),
    Column("ci_low", Float),
    Column("ci_high", Float),
    Column("model_id", String),
    Column("actual", Float),
    Column("error", Float),
    Column("scored_at", UTCDateTime),
    Index("ix_coach_prediction_entry_id", "entry_id"),
    Index("ix_coach_prediction_metric_for_date", "metric", "for_date"),
)

# Coaching knowledge: the current article, rule or source, plus one row per version.
# The full-text index (kb_fts) is derived from these tables and is not part of this metadata.
kb_article = Table(
    "kb_article",
    metadata,
    Column("id", String, primary_key=True),
    Column("slug", String, nullable=False),
    Column("topic", String, nullable=False),
    Column("title", String, nullable=False),
    Column("body_md", Text, nullable=False),
    Column("evidence_grade", String, nullable=False),
    Column("status", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("author", String, nullable=False),
    UniqueConstraint("slug", name="uq_kb_article_slug"),
    Index("ix_kb_article_topic", "topic"),
)
kb_article_revision = Table(
    "kb_article_revision",
    metadata,
    Column("article_id", String, ForeignKey("kb_article.id"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("slug", String, nullable=False),
    Column("topic", String, nullable=False),
    Column("title", String, nullable=False),
    Column("body_md", Text, nullable=False),
    Column("evidence_grade", String, nullable=False),
    Column("status", String, nullable=False),
    Column("author", String, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
)
kb_rule = Table(
    "kb_rule",
    metadata,
    Column("id", String, primary_key=True),
    Column("article_id", String, ForeignKey("kb_article.id"), nullable=False),
    Column("text", Text, nullable=False),
    Column("grade", String, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Index("ix_kb_rule_article_id", "article_id"),
)
kb_rule_revision = Table(
    "kb_rule_revision",
    metadata,
    Column("rule_id", String, ForeignKey("kb_rule.id"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("article_id", String, ForeignKey("kb_article.id"), nullable=False),
    Column("text", Text, nullable=False),
    Column("grade", String, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("author", String, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
)
kb_source = Table(
    "kb_source",
    metadata,
    Column("id", String, primary_key=True),
    Column("article_id", String, ForeignKey("kb_article.id"), nullable=False),
    Column("citation", Text, nullable=False),
    Column("doi", String),
    Column("url", String),
    Column("verified_at", UTCDateTime),
    Column("version", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Index("ix_kb_source_article_id", "article_id"),
)
kb_source_revision = Table(
    "kb_source_revision",
    metadata,
    Column("source_id", String, ForeignKey("kb_source.id"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("article_id", String, ForeignKey("kb_article.id"), nullable=False),
    Column("citation", Text, nullable=False),
    Column("doi", String),
    Column("url", String),
    Column("verified_at", UTCDateTime),
    Column("author", String, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
)
kb_proposal = Table(
    "kb_proposal",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("target", String, nullable=False),
    Column("base_version", Integer, nullable=False),
    Column("changes", JSON, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("author", String, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("decided_at", UTCDateTime),
    Column("decided_by", String),
    Index("ix_kb_proposal_status", "status"),
)

# intervals.icu calendar cache. ``id`` is the intervals.icu event id. ``source`` is
# ``plan`` (the event has an external_id) or ``other`` (the athlete's own, or one
# intervals.icu created). ``session_key`` ties a brick's ``:fiets`` and ``:loop`` parts
# together; ``external_id`` keeps the full session key ``<date>:<sport>`` plus any suffix.
icu_event = Table(
    "icu_event",
    metadata,
    Column("id", String, primary_key=True),
    Column("external_id", String),
    Column("session_key", String),
    Column("brick_part", String),
    Column("source", String, nullable=False),
    Column("category", String, nullable=False),
    Column("type", String),
    Column("date", Date, nullable=False, index=True),
    Column("name", String),
    Column("description", Text),
    Column("workout_doc", JSON),
    Column("tags", JSON, nullable=False),
    Column("for_week", Boolean, nullable=False),
    Column("targets", JSON, nullable=False),
    Column("updated", UTCDateTime),
    Column("raw", JSON, nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False),
    *_provenance(),
    Index("ix_icu_event_external_id", "external_id"),
    Index("ix_icu_event_session_key", "session_key"),
)

# RPE (1-10) and Feel (1 Strong … 5 Weak) entered in VibeSynergy for an activity that has no
# value on Garmin or intervals.icu. Keyed by a member ``source:source_id`` so it survives a
# rebuild of activity_unified. icu_* is the copy to intervals.icu: icu_error is "pending",
# "not_found" (not there yet), "conflict:<fields>" (a different value there, kept) or an
# error; icu_sent holds the intervals.icu fields VibeSynergy last wrote.
activity_effort = Table(
    "activity_effort",
    metadata,
    Column("source_ref", String, primary_key=True),
    Column("subject_id", Integer, nullable=False),
    Column("rpe", Integer),
    Column("feel", Integer),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("icu_activity_id", String),
    Column("icu_pushed_at", UTCDateTime),
    Column("icu_error", Text),
    Column("icu_sent", JSON),
)

# One row per check-in date pushed to intervals.icu wellness. A newer check-in, or a
# last_error, is retried on the next intervals.icu sync. sent is only the check-in fields.
icu_checkin_push = Table(
    "icu_checkin_push",
    metadata,
    Column("date", Date, primary_key=True),
    Column("checkin_updated_at", UTCDateTime, nullable=False),
    Column("pushed_at", UTCDateTime),
    Column("last_error", Text),
    Column("sent", JSON),
)

# Agent events: small versioned notices for outside coach agents, e.g. activity.ready.
# agent_event_watch debounces new activities until their Garmin and Strava copies merged;
# refs are every member ``source:id`` seen, so a later merge never emits the activity twice.
agent_event_watch = Table(
    "agent_event_watch",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("group_id", String, nullable=False),
    Column("refs", JSON, nullable=False),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("due_at", UTCDateTime, nullable=False),
    Column("event_id", String),
    Index("ix_coach_event_watch_first_seen_at", "first_seen_at"),
)

# One emitted event; data holds ids and non-sensitive facts only (docs/EVENTS.md).
agent_event = Table(
    "agent_event",
    metadata,
    Column("id", String, primary_key=True),
    Column("type", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("occurred_at", UTCDateTime, nullable=False),
    Column("data", JSON, nullable=False),
    Index("ix_coach_event_occurred_at", "occurred_at"),
)

# The outbox: one row per event and webhook subscription, so a restart never sends twice.
# state: pending, retry, sending, sent, failed, gave_up, skipped, expired, unknown.
agent_event_delivery = Table(
    "agent_event_delivery",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String, nullable=False),
    Column("subscription_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("next_at", UTCDateTime, nullable=False),
    Column("attempted_at", UTCDateTime),
    Column("sent_at", UTCDateTime),
    Column("http_status", Integer),
    Column("error", Text),
    Column("link", String),
    UniqueConstraint(
        "event_id", "subscription_id", name="uq_coach_event_delivery_event_id_subscription_id"
    ),
    Index("ix_coach_event_delivery_state_next_at", "state", "next_at"),
)

# API coach runner (docs/COACH_RUNNER.md): one row per job run. (job, run_key) is unique, so
# a scheduled job runs once per day and an insight once per event; attempts allows one retry.
# Usage and cost are written after every model turn, so a crash still counts its spend.
coach_run = Table(
    "coach_run",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job", String, nullable=False),
    Column("run_key", String, nullable=False),
    Column("for_date", Date, nullable=False),
    Column("trigger", String, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("model", String, nullable=False),
    Column("started_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("turns", Integer, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_write_tokens", Integer, nullable=False),
    Column("cache_read_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("stop_reason", String),
    Column("error", Text),
    Column("entry_id", Integer),
    Column("mailed_at", UTCDateTime),
    Column("mail_error", Text),
    # Context blocks preloaded and output-contract issues, for the run log.
    Column("harness", JSON),
    UniqueConstraint("job", "run_key", name="uq_coach_run_job_run_key"),
    Index("ix_coach_run_started_at", "started_at"),
)

# The athlete's replies to a coach entry (Coach page). The next run reads the unread ones
# and marks them read with its run id.
coach_reply = Table(
    "coach_reply",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entry_id", Integer, nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("author", String, nullable=False),
    Column("text", Text, nullable=False),
    Column("read_at", UTCDateTime),
    Column("read_run_id", Integer),
    Index("ix_coach_reply_entry_id", "entry_id"),
    Index("ix_coach_reply_read_at", "read_at"),
)

# The training plan as VibeSynergy's own model (docs/design/plan-model.md). Step 1 fills
# it from intervals.icu in shadow (read-only towards intervals.icu); readers still use
# icu_event until the switch. Every write is a plan_version; rows keep the version that
# last changed them.
plan = Table(
    "plan",
    metadata,
    Column("id", String, primary_key=True),
    Column("subject_id", Integer, nullable=False),
    Column("name", String, nullable=False),
    Column("method", String),
    Column("start", Date),
    Column("end", Date),
    Column("status", String, nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

plan_version = Table(
    "plan_version",
    metadata,
    Column("plan_id", String, nullable=False),
    Column("n", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("author", String, nullable=False),
    Column("reason", Text, nullable=False),
    Column("summary", JSON, nullable=False),
    PrimaryKeyConstraint("plan_id", "n"),
)

plan_week = Table(
    "plan_week",
    metadata,
    Column("plan_id", String, nullable=False),
    Column("n", Integer, nullable=False),
    Column("monday", Date, nullable=False),
    Column("phase", String),
    Column("kind", String),
    Column("hours_label", String),
    Column("note", Text),
    Column("version", Integer, nullable=False),
    PrimaryKeyConstraint("plan_id", "n"),
)

plan_target = Table(
    "plan_target",
    metadata,
    Column("plan_id", String, nullable=False),
    Column("week", Integer, nullable=False),
    Column("sport", String, nullable=False),
    Column("time_s", Float),
    Column("distance_m", Float),
    Column("load", Float),
    Column("version", Integer, nullable=False),
    PrimaryKeyConstraint("plan_id", "week", "sport"),
)

plan_session = Table(
    "plan_session",
    metadata,
    Column("id", String, primary_key=True),
    Column("plan_id", String, nullable=False),
    Column("date", Date, nullable=False),
    Column("slot", String),
    Column("sport", String),
    Column("name", String),
    Column("duration_s", Float),
    Column("distance_m", Float),
    Column("workout", JSON, nullable=False),
    Column("description", Text),
    Column("status", String, nullable=False),
    Column("version", Integer, nullable=False),
    Index("ix_plan_session_plan_date", "plan_id", "date"),
)

# Where a session lives outside VibeSynergy (intervals.icu event, Garmin workout) and its sync
# state: the idempotency record for the mirror and Garmin delivery.
plan_link = Table(
    "plan_link",
    metadata,
    Column("session_id", String, nullable=False),
    Column("target", String, nullable=False),
    Column("remote_id", String),
    Column("remote_version", String),
    Column("fingerprint", String),
    Column("synced_at", UTCDateTime),
    Column("last_error", Text),
    PrimaryKeyConstraint("session_id", "target"),
)

# Own settings per gear item, keyed by the gear ref ("strava:g123"): status
# (active, spare, retired), the lifetime limit in km and a note. Totals stay the source's.
gear_setting = Table(
    "gear_setting",
    metadata,
    Column("ref", String, primary_key=True),
    Column("status", String),
    Column("life_km", Float),
    Column("note", Text),
    Column("updated_at", UTCDateTime, nullable=False),
)

# Maintenance per gear item: what (chain, tyres, service…), every N km and/or days,
# and when and at how many km it was last done. "Due" is derived when gear is read.
gear_service = Table(
    "gear_service",
    metadata,
    Column("id", String, primary_key=True),
    Column("ref", String, nullable=False, index=True),
    Column("what", String, nullable=False),
    Column("every_km", Float),
    Column("every_days", Integer),
    Column("last_done", Date),
    Column("last_km", Float),
    Column("updated_at", UTCDateTime, nullable=False),
)

# Gear per activity, set by the athlete, keyed by each source copy ("strava:123") so
# it survives regrouping. ref NULL: no gear. It counts in the gear totals at once.
activity_gear = Table(
    "activity_gear",
    metadata,
    Column("source_ref", String, primary_key=True),
    Column("ref", String),
    Column("updated_at", UTCDateTime, nullable=False),
)

# Devices that get alerts as push notifications: one row per browser subscription.
push_subscription = Table(
    "push_subscription",
    metadata,
    Column("endpoint", String, primary_key=True),
    Column("p256dh", String, nullable=False),
    Column("auth", String, nullable=False),
    Column("agent", String),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_ok", UTCDateTime),
    Column("failures", Integer, nullable=False),
)

# Default gear per sport: activities of that sport from ``since`` on that have no gear
# from any source count on it.
gear_default = Table(
    "gear_default",
    metadata,
    Column("sport", String, primary_key=True),
    Column("ref", String, nullable=False),
    Column("since", Date, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

# Portal login (query/auth.py): one account per installation. Secrets never in plain text:
# the password as an scrypt hash, tokens and session ids as SHA-256 hashes, the TOTP secret
# in the encrypted secret store.
auth_account = Table(
    "auth_account",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String, nullable=False, unique=True),
    Column("password_hash", String),  # None until the first login sets it
    Column("password_set_at", UTCDateTime),
    Column("mfa", String, nullable=False),  # none | mail | totp
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

auth_session = Table(
    "auth_session",
    metadata,
    Column("id", String, primary_key=True),  # sha256 of the cookie value
    Column("account_id", Integer, ForeignKey("auth_account.id"), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("mfa_ok", Boolean, nullable=False),  # False: waiting for the second factor
    Column("client", String, nullable=False),
    Column("agent", String, nullable=False),
)

auth_token = Table(
    "auth_token",
    metadata,
    Column("hash", String, primary_key=True),  # sha256 of the link token or mailed code
    Column("account_id", Integer, ForeignKey("auth_account.id"), nullable=False),
    Column("purpose", String, nullable=False),  # login | reset | setup | mfa_mail
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("used_at", UTCDateTime),
)

auth_attempt = Table(
    "auth_attempt",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String, nullable=False),  # email:<address> or client:<address>
    Column("at", UTCDateTime, nullable=False),
    Column("ok", Boolean, nullable=False),
    Index("ix_auth_attempt_key_at", "key", "at"),
)
