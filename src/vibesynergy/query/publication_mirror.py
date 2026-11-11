"""Strava is the source of truth, Garmin the mirror (the athlete 3 Oct 2026).

After a verified Strava publication the activity is watched for 7 days, counted from the
first Strava publication or the activity start, whichever is later. Each check reads the
Strava activity once. When the user changed the Strava text, the VibeSynergy block is
regenerated and kept below it (no duplicate). Garmin then gets exactly Strava's user text
plus the same block, and Strava's title. Garmin's own description from before the first
mirror write is kept in ``publication_mirror.garmin_original`` (and its title in
``garmin_original_title``).

Cadence: the scheduler looks for due watches every 5 minutes; a watch is checked every 30
minutes during its first 48 hours and hourly after that, so a few published activities stay
well inside Strava's read budget (shared with the normal sync). Strava's rate limit stops a
run and the watch is retried on the next one. A watch stops at ``watch_until`` (expired),
when the Strava block is removed (removed) or when a section needs review (conflict).
Activities without a Strava link are never watched: Garmin publishing stays Garmin-only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.secrets import SecretStore
from vibesynergy.query import evidence, publication, publication_format, views
from vibesynergy.store.schema import activity_unified, publication_mirror

log = logging.getLogger(__name__)

JOB = "publication_mirror"
WATCH = timedelta(days=7)
FAST = timedelta(minutes=30)
FAST_FOR = timedelta(hours=48)
SLOW = timedelta(hours=1)
MAX_PER_RUN = 25


class Edited(publication.Conflict):
    """The Strava block was edited or duplicated by hand: stop and leave it for review."""


def enabled(rt) -> bool:
    """When false, Strava publication stays Strava-only (no Garmin mirror or watch)."""
    from vibesynergy.core.config import load_settings

    pub = load_settings(rt.settings.config_path).publication
    return bool(pub.get("garmin_mirror_enabled", True))


def lock(rt, group: str):
    """Serializes a watch's checks with publish/remove of the same activity (taken before,
    never inside, the per-destination publication lock)."""
    return SecretStore(rt.settings.secrets_dir, "publication").lock(f"mirror-{group}")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _ids(rt, activity_id: str) -> tuple[str, str | None, str, datetime]:
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(activity_unified.c.sources, activity_unified.c.start_utc).where(
                    activity_unified.c.group_id == group
                )
            )
            .mappings()
            .one()
        )
    strava = [r.split(":", 1)[1] for r in row["sources"] if r.startswith("strava:")]
    garmin = [r.split(":", 1)[1] for r in row["sources"] if r.startswith("garmin:")]
    if len(strava) != 1:
        raise publication.Conflict("One matching strava activity is required")
    return strava[0], garmin[0] if len(garmin) == 1 else None, group, row["start_utc"]


def watch(rt, group: str) -> dict | None:
    with rt.engine.connect() as conn:
        row = (
            conn.execute(select(publication_mirror).where(publication_mirror.c.group_id == group))
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _update(rt, group: str, **values) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            publication_mirror.update()
            .where(publication_mirror.c.group_id == group)
            .values(**values, updated_at=datetime.now(UTC))
        )


def start(rt, activity_id: str, language: str, *, now: datetime | None = None) -> dict:
    """Begin (or keep) the 7-day watch after a verified Strava publication."""
    now = _now(now)
    strava_id, garmin_id, group, start_utc = _ids(rt, activity_id)
    old = watch(rt, group)
    first = old["first_published_at"] if old and old["state"] == "active" else now
    values = {
        "strava_id": strava_id,
        "garmin_id": garmin_id,
        "language": language,
        "state": "active",
        "first_published_at": first,
        "watch_until": max(first, start_utc) + WATCH,
        "next_check_at": now,
        "last_error": None,
        "updated_at": now,
    }
    with rt.engine.begin() as conn:
        if old:
            conn.execute(
                publication_mirror.update()
                .where(publication_mirror.c.group_id == group)
                .values(**values)
            )
        else:
            conn.execute(publication_mirror.insert().values(group_id=group, **values))
    return watch(rt, group)


class _OneRead:
    """Serve one already-fetched Strava detail to the next read, then read live again."""

    def __init__(self, client, detail: dict):
        self.client, self._detail = client, detail

    def read(self, source_id: str) -> dict:
        detail, self._detail = self._detail, None
        return detail if detail is not None else self.client.read(source_id)

    def write(self, source_id: str, description: str) -> None:
        self._detail = None
        self.client.write(source_id, description)


def _remove_after_manual_strava_edit(
    rt, activity_id: str, language: str, *, group: str, strava_id: str, garmin=None
) -> dict:
    """The user removed the VibeSynergy block on Strava; drop the Garmin copy and ledger.

    Called from ``sync`` while the mirror lock is already held.
    """
    _update(rt, group, state="removed")
    publication._delete_checkpoint(rt, "strava", strava_id)
    mirror = after_unpublish(rt, activity_id, language, garmin=garmin)
    publication._remove_icu(rt, activity_id)
    return {
        "strava": "removed",
        "garmin": mirror.get("garmin", "not_watched"),
        "title": "unchanged",
        "state": "removed",
    }


def sync(
    rt, activity_id: str, *, language: str, strava=None, garmin=None, removal_only: bool = False
) -> dict:
    """One check: Strava block below Strava's text, then Garmin = Strava text + block + title."""
    strava_id, garmin_id, group, _ = _ids(rt, activity_id)
    strava = strava or publication.provider(rt, "strava")
    result = {"strava": "unchanged", "garmin": "unchanged", "title": "unchanged"}
    detail = strava.read(strava_id)
    previous = publication._ledger(rt, "strava", strava_id)
    text = detail.get("description") or ""
    if not isinstance(text, str):
        text = ""
    blocks = publication_format.find_blocks(text)
    if previous and previous["state"] == "verified" and len(blocks) == 0:
        return _remove_after_manual_strava_edit(
            rt,
            activity_id,
            language,
            group=group,
            strava_id=strava_id,
            garmin=garmin,
        )
    if previous and previous["state"] == "verified":
        if len(blocks) != 1:
            raise Edited("The Strava section changed by hand; review the description")
        if publication_format.block_text(text, blocks[0]) != previous["owned_block"]:
            raise Edited("The Strava section changed by hand; review the description")
    if removal_only:
        # Mirroring is off: do not refresh the block, but a hand-edited section still stops.
        if previous and previous["state"] == "verified" and publication_format.orphan_markers(text):
            raise Edited("The Strava section changed by hand; review the description")
        return {"strava": "unchanged", "garmin": "mirror_off", "title": "unchanged"}
    item = publication.preview(
        rt, activity_id, "strava", language=language, client=_OneRead(strava, detail)
    )
    if item["state"] != "ready":
        raise publication.Conflict("A current assessment is required before publication")
    if not item["already_published"]:
        publication.publish(
            rt,
            activity_id,
            "strava",
            item["preview_version"],
            language=language,
            client=strava,
            mirror=False,
        )
        detail = strava.read(strava_id)
        result["strava"] = "updated"
        text = detail.get("description") or ""
        if not isinstance(text, str):
            text = ""
    ledger = publication._ledger(rt, "strava", strava_id)
    if not ledger or ledger["state"] != "verified":
        raise publication.Conflict("The Strava section is not verified")
    owned = ledger["owned_block"]
    blocks = publication_format.find_blocks(text)
    if len(blocks) != 1:
        raise Edited("The Strava section changed by hand; review the description")
    if publication_format.block_text(text, blocks[0]) != owned:
        raise Edited("The Strava section changed by hand; review the description")
    user_text = publication._without_owned(text, owned)
    if garmin_id is None:
        result["garmin"] = "not_linked"
        return result
    garmin = garmin or publication.provider(rt, "garmin")
    current_detail = garmin.read(garmin_id)
    current = current_detail.get("description") or ""
    if not isinstance(current, str):
        raise publication.Conflict("Garmin description is unreadable")
    desired = f"{user_text}\n\n{owned}" if user_text else owned
    garmin_ledger = publication._ledger(rt, "garmin", garmin_id)
    if (
        publication_format.same_text(current, desired)
        and garmin_ledger
        and garmin_ledger["state"] == "pending"
        and publication_format.same_text(garmin_ledger["description"], desired)
    ):
        # An interrupted mirror write that did land: verify it instead of writing again.
        publication._checkpoint(
            rt,
            {
                "destination": "garmin",
                "source_id": garmin_id,
                "assessment_id": garmin_ledger["assessment_id"],
                "preview_version": garmin_ledger["preview_version"],
                "owned_block": garmin_ledger["owned_block"],
                "current_description": garmin_ledger["base_description"],
                "description": desired,
            },
            "verified",
        )
    if not publication_format.same_text(current, desired):
        row = watch(rt, group)
        if row and row["garmin_original"] is None:
            _update(rt, group, garmin_original=current)
        item = {
            "destination": "garmin",
            "source_id": garmin_id,
            "assessment_id": ledger["assessment_id"],
            "preview_version": evidence.digest(
                {
                    "source": "garmin",
                    "id": garmin_id,
                    "base": current,
                    "description": desired,
                    "assessment": ledger["assessment_id"],
                }
            ),
            "owned_block": owned,
            "current_description": current,
            "description": desired,
        }
        publication._checkpoint(rt, item, "pending")
        garmin.write(garmin_id, desired)
        after = garmin.read(garmin_id)
        returned = after.get("description")
        if str(after.get("activityId")) != garmin_id or not publication_format.same_text(
            returned if isinstance(returned, str) else "", desired
        ):
            raise publication.Conflict("Garmin did not return the mirrored description")
        publication._checkpoint(rt, item, "verified")
        current_detail = after
        result["garmin"] = "updated"
    title = detail.get("name")
    if isinstance(title, str) and title.strip() and current_detail.get("activityName") != title:
        row = watch(rt, group)
        if row and row["garmin_original_title"] is None:
            _update(rt, group, garmin_original_title=str(current_detail.get("activityName") or ""))
        garmin.write_title(garmin_id, title)
        after = garmin.read(garmin_id)
        if after.get("activityName") != title:
            raise publication.Conflict("Garmin did not return the mirrored title")
        result["title"] = "updated"
    return result


def after_publish(rt, activity_id: str, language: str, *, strava=None, garmin=None) -> dict:
    """Start the watch and mirror right away; a mirror failure never undoes the publish."""
    if not enabled(rt):
        return {"state": "disabled"}
    group = _ids(rt, activity_id)[2]
    with lock(rt, group):
        row = start(rt, activity_id, language)
        try:
            result = sync(rt, activity_id, language=language, strava=strava, garmin=garmin)
        except Exception as exc:  # recorded on the watch; the next run retries
            log.warning("publication mirror for %s failed: %s", activity_id, exc)
            _update(rt, row["group_id"], last_error=str(exc)[:500])
            return {"state": "watching", "error": str(exc)[:500]}
        _update(rt, row["group_id"], last_result=result, last_checked_at=datetime.now(UTC))
        return {"state": "watching", **result}


def stopping(rt, activity_id: str):
    """Context for removing the Strava block: the watch is marked removed first (under the
    watch lock) so no check can re-add the block; a failed removal reactivates it."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        try:
            group = _ids(rt, activity_id)[2]
        except (publication.Conflict, ValueError):
            yield
            return
        with lock(rt, group):
            row = watch(rt, group)
            active = row is not None and row["state"] == "active"
            if active:
                _update(rt, group, state="removed")
            try:
                yield
            except BaseException:
                strava_row = publication._ledger(rt, "strava", row["strava_id"]) if row else None
                # An uncertain removal (checkpoint "removing") stays stopped: never re-add.
                if active and not (strava_row and strava_row["state"] == "removing"):
                    _update(rt, group, state="active")
                raise

    return _ctx()


def after_unpublish(rt, activity_id: str, language: str, *, garmin=None) -> dict:
    """Stop the watch and remove the mirrored Garmin block, leaving the text as it is."""
    try:
        _, garmin_id, group, _ = _ids(rt, activity_id)
    except publication.Conflict:
        return {"state": "not_watched"}
    if watch(rt, group) is None:
        return {"state": "not_watched"}
    _update(rt, group, state="removed")
    if garmin_id is None or not publication._ledger(rt, "garmin", garmin_id):
        return {"state": "removed"}
    garmin = garmin or publication.provider(rt, "garmin")
    item = publication.preview(rt, activity_id, "garmin", language=language, client=garmin)
    if item["state"] == "ready" and item.get("already_published"):
        publication.unpublish(
            rt, activity_id, "garmin", item["preview_version"], language=language, client=garmin
        )
        return {"state": "removed", "garmin": "removed"}
    return {"state": "removed", "garmin": "needs_review"}


def due(rt, *, now: datetime | None = None) -> bool:
    """True when a watch is due.

    Switching the mirror off stops new copies. An earlier watch still becomes due so a
    block the athlete removed on Strava can be cleared from Garmin.
    """
    now = _now(now)
    with rt.engine.connect() as conn:
        return (
            conn.execute(
                select(publication_mirror.c.group_id)
                .where(
                    publication_mirror.c.state == "active",
                    publication_mirror.c.next_check_at <= now,
                )
                .limit(1)
            ).first()
            is not None
        )


def run(rt, *, now: datetime | None = None, strava=None, garmin=None) -> dict:
    """The scheduled job: check every due watch once, oldest first.

    With mirroring switched off, a check only removes a Garmin copy after the athlete
    deleted the block on Strava. It does not refresh descriptions or titles.
    """
    removal_only = not enabled(rt)
    now = _now(now)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(publication_mirror)
                .where(
                    publication_mirror.c.state == "active",
                    publication_mirror.c.next_check_at <= now,
                )
                .order_by(publication_mirror.c.next_check_at)
                .limit(MAX_PER_RUN)
            )
            .mappings()
            .all()
        )
    summary = {"checked": 0, "expired": 0, "conflict": 0, "failed": 0, "rate_limited": False}
    for row in rows:
        group = row["group_id"]
        if now >= row["watch_until"]:
            _update(rt, group, state="expired")
            summary["expired"] += 1
            continue
        step = FAST if now - row["first_published_at"] < FAST_FOR else SLOW
        try:
            with lock(rt, group):
                current = watch(rt, group)
                if not current or current["state"] != "active":
                    continue  # removed (or changed) since this run started
                result = sync(
                    rt,
                    f"strava:{row['strava_id']}",
                    language=row["language"],
                    strava=strava,
                    garmin=garmin,
                    removal_only=removal_only,
                )
        except RateLimited as exc:
            summary["rate_limited"] = True
            retry = getattr(exc, "retry_at", None)
            if isinstance(retry, datetime) and retry > now:
                _update(rt, group, next_check_at=min(retry, row["watch_until"]))
            break  # the rest waits for the next run
        except Edited as exc:
            _update(rt, group, state="conflict", last_error=str(exc)[:500], last_checked_at=now)
            summary["conflict"] += 1
            continue
        except Exception as exc:  # network or provider trouble: try again later
            log.warning("publication mirror check for %s failed: %s", group, exc)
            _update(
                rt,
                group,
                last_error=str(exc)[:500],
                last_checked_at=now,
                next_check_at=min(now + step, row["watch_until"]),
            )
            summary["failed"] += 1
            continue
        _update(
            rt,
            group,
            last_result=result,
            last_error=None,
            last_checked_at=now,
            next_check_at=min(now + step, row["watch_until"]),
        )
        summary["checked"] += 1
    return summary
