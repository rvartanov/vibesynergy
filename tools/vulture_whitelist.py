"""Names vulture cannot see being used (#202). CI runs ``vulture`` over ``src`` and this file
(settings in pyproject.toml, ``[tool.vulture]``); a name here counts as used.

Keep it honest: add a name only when something outside plain Python calls it (a framework,
an entry point, a signature that must match), or under "Known leftovers" with the issue
that removes it. Tests do not count as a caller: code only tests use is dead code.
Alembic migrations are excluded in pyproject.toml; route, MCP-tool and CLI functions are
ignored by their decorators there.
"""

# ruff: noqa: B018, F821
# fmt: off

# --- Frameworks call these --------------------------------------------------------------

# Pydantic reads model_config on every model.
_.model_config
# SQLAlchemy TypeDecorator (store/types.py): its hooks, class attributes and signature.
_.impl
_.cache_ok
_.process_bind_param
_.process_result_value
_.dialect
# The MCP SDK's OAuth provider protocol (api/oauth.py): the SDK calls these.
_.register_client
_.authorize
_.load_authorization_code
_.exchange_authorization_code
_.load_refresh_token
_.exchange_refresh_token
_.revoke_token
# uvicorn's Server (cli/server.py, cli/main.py): it reads should_exit and calls
# capture_signals.
_.should_exit
_.capture_signals
# Typer passes --version as a parameter; its eager callback does the work (cli/main.py).
_.show_version
# sqlite3's authorizer callback takes five arguments (query/sql.py).
_.arg2
# Source plugins, loaded through the entry points in pyproject.toml.
_.CalDAVSource
_.DemoSource
_.FilesSource
_.GarminExportSource
_.RunSynergySource

# --- Read by name: dataclass and pydantic fields, getattr -------------------------------

# Record fields (model.py), filled by the sources and stored with asdict().
_.tz_offset_s
_.sub_sport
_.calories
_.aerobic_te
_.anaerobic_te
_.deep_s
_.light_s
_.rem_s
_.awake_s
_.plan_type
_.time_created
_.manufacturer
_.product
_.serial_number
_.workout_doc
# Request and document fields that pydantic fills and dumps.
_.distance_km
_.instructions
_.tone
_.imported_digest
# Catalogue and threshold-test fields, serialised as a whole.
_.daily_agg
_.higher_is_better
_.ftp_factor
_.run_lthr_last_min
_.step_match
_.distance_match
_.hold_s
_.default_on
# intervals.icu client: _pace() reads the attribute by its name; the key is kept with it.
_._last_write
_._api_key

# --- Tables kept until the separate drop step (#202: no table is dropped here) ----------

_.coaching_override
_.coaching_rule
_.publication_mirror
_.icu_checkin_push

# --- Used outside src: tools/ scripts and the i18n guard --------------------------------

_.usual_window  # tools/sleep_check_replay.py
_.UI_STRINGS  # the i18n catalogue guard reads the OAuth page's strings

# --- Known leftovers: no caller in src, not part of #202 ---------------------------------
# Remove these in a follow-up (with the tests that only test them), then drop them here.

_.upsert_item  # query/equipment.py: #211
_.workout_prompt_context  # query/coach.py
_.portal_address  # portal/hosts.py: old alias of canonical_url
_.OPTIONAL  # coach_runner/model.py
_.AI_COACH_AUTHOR  # mcp/identity.py
_.SAFETY_RULES  # query/daily_coach.py
_.AUDIENCES  # releases.py
_.SHADOW  # query/plan_model.py
_.Purpose  # query/session_purpose.py
_.ACTIONS  # query/workout_media.py
_.TITLE  # query/workout_method.py
_.LINT_CHECK  # query/workout_method.py
_.PARTS  # sources/runsynergy/feed.py
