"""Explicit, checkpointed activity-description publication.

Only exact source IDs from the unified activity may be targets. Provider reads and writes
are isolated in adapters so offline tests can supply synthetic providers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select

from vibesynergy.core.config import load_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import (
    annotations,
    evidence,
    publication_format,
    publication_insight,
    publication_plugins,
    views,
)
from vibesynergy.sources import oauth
from vibesynergy.store.schema import activity_unified, audit_log, publication_ledger


class Conflict(ValueError):
    pass


class Provider(Protocol):
    def read(self, source_id: str) -> dict: ...
    def write(self, source_id: str, description: str) -> None: ...


class StravaProvider:
    def __init__(self, rt, store: SecretStore | None = None):
        from vibesynergy.sources.http_timeout import http_timeout_seconds
        from vibesynergy.sources.strava.client import StravaClient

        self.store = store or SecretStore(rt.settings.secrets_dir, "strava")
        self.client = StravaClient(self.store, timeout=http_timeout_seconds(rt.settings, "strava"))

    def read(self, source_id: str) -> dict:
        try:
            detail = self.client.get(f"/activities/{source_id}")
        except oauth.AuthError as exc:
            raise Conflict(str(exc)) from exc
        owner = (self.store.get("athlete") or {}).get("id")
        if owner is None or str((detail.get("athlete") or {}).get("id")) != str(owner):
            raise Conflict("Strava activity owner could not be verified")
        return detail

    def write(self, source_id: str, description: str) -> None:
        try:
            self.client.update_description(source_id, description)
        except oauth.AuthError as exc:
            raise Conflict(str(exc)) from exc


class GarminProvider:
    def __init__(self, rt):
        self.store = SecretStore(rt.settings.secrets_dir, "garmin")

    def _call(self, method: str, *args):
        from vibesynergy.sources.garmin.client import GarminAuthError, connect, save_tokens

        try:
            with self.store.lock("tokens"):
                client = connect(self.store)
                result = getattr(client, method)(*args)
                save_tokens(self.store, client)
                return result, getattr(client, "profile_id", None)
        except GarminAuthError as exc:
            raise Conflict(str(exc)) from exc

    def read(self, source_id: str) -> dict:
        detail, profile_id = self._call("get_activity", source_id)
        if str(detail.get("activityId")) != source_id:
            raise Conflict("Garmin activity identity could not be verified")
        owner = (self.store.get("account") or {}).get("id")
        if (
            type(profile_id) is not int
            or detail.get("userProfileId") != profile_id
            or (owner is not None and owner != profile_id)
        ):
            raise Conflict("Garmin activity owner could not be verified")
        return detail

    def write(self, source_id: str, description: str) -> None:
        self._call("set_activity_description", source_id, description)

    def write_title(self, source_id: str, title: str) -> None:
        self._call("set_activity_name", source_id, title)


def provider(rt, destination: str) -> Provider:
    if destination == "strava":
        store = SecretStore(rt.settings.secrets_dir, destination)
        scopes = oauth.scope_set((store.get("tokens") or {}).get("scope"))
        if "activity:write" not in scopes:
            raise Conflict("Connect Strava with activity write access in Settings → Sources")
        return StravaProvider(rt, store)
    if destination == "garmin":
        return GarminProvider(rt)
    raise ValueError("Only Garmin and Strava descriptions can be published")


def _source_id(rt, activity_id: str, destination: str) -> str:
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == group)
        )
    matches = [ref.split(":", 1)[1] for ref in sources or [] if ref.startswith(destination + ":")]
    if len(matches) != 1:
        raise Conflict(f"One matching {destination} activity is required")
    return matches[0]


def _ledger(rt, destination: str, source_id: str) -> dict | None:
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(publication_ledger).where(
                    publication_ledger.c.source == destination,
                    publication_ledger.c.source_id == source_id,
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _located_block(text: str, previous: dict) -> tuple[int, int]:
    blocks = publication_format.find_blocks(text)
    if len(blocks) != 1 or publication_format.orphan_markers(text):
        raise Conflict("The published section changed; review the provider description")
    span = blocks[0]
    if publication_format.block_text(text, span) != previous["owned_block"]:
        raise Conflict("The published section changed; review the provider description")
    return span


def _compose(current: str, owned: str, previous: dict | None) -> str:
    if previous:
        _located_block(current, previous)
        old = previous["owned_block"]
        if current.endswith(old):
            return current[: -len(old)] + owned
        base = _without_owned(current)
        return f"{base}\n\n{owned}" if base else owned
    if publication_format.find_blocks(current) or publication_format.orphan_markers(current):
        raise Conflict("An existing VibeSynergy section needs review before publication")
    return f"{current}\n\n{owned}" if current else owned


def preview(rt, activity_id: str, destination: str, *, language: str = "en", client=None) -> dict:
    """Read the target and return the exact description a publish would write."""
    source_id = _source_id(rt, activity_id, destination)
    client = client or provider(rt, destination)
    detail = client.read(source_id)
    if str(detail.get("id" if destination == "strava" else "activityId")) != source_id:
        raise Conflict("Provider activity identity changed")
    current = detail.get("description") or ""
    if not isinstance(current, str):
        raise Conflict("Provider description is unreadable")
    previous = _ledger(rt, destination, source_id)
    pub = load_settings(rt.settings.config_path).publication
    labels = publication_format.section_labels(pub, language)
    if previous and previous["state"] == "removing":
        if not publication_format.same_text(
            current, previous["base_description"]
        ) and not publication_format.same_text(current, previous["description"]):
            raise Conflict("Removal outcome is uncertain; review the provider description")
        return {
            "state": "undo_pending",
            "destination": destination,
            "source_id": source_id,
            "preview_version": previous["preview_version"],
            "description": current,
            "undo_description": previous["description"],
            "plugins": {},
        }
    block = annotations.preview(rt, activity_id, language=language)
    block_id = block.get("assessment_id") or evidence.digest(
        {"activity": activity_id, "language": language, "stats": block.get("stats")}
    )
    if block["state"] != "preview":
        if previous and previous["state"] == "verified":
            _compose(current, previous["owned_block"], previous)
            return {
                "state": "ready",
                "destination": destination,
                "source_id": source_id,
                "assessment_id": previous["assessment_id"],
                "preview_version": evidence.digest(
                    {
                        "source": destination,
                        "id": source_id,
                        "base": current,
                        "description": current,
                        "assessment": previous["assessment_id"],
                    }
                ),
                "description": current,
                "current_description": current,
                "owned_block": previous["owned_block"],
                "plugins": {},
                "already_published": True,
                "undo_description": _without_owned(current),
            }
        return {
            "state": "blocked",
            "reason": block["reason"],
            "assessment_state": block.get("assessment_state"),
            "destination": destination,
        }
    if (
        previous
        and previous["state"] == "pending"
        and not publication_format.same_text(current, previous["description"])
        and not publication_format.same_text(current, previous["base_description"])
    ):
        raise Conflict("Previous write outcome is uncertain; review the provider description")
    sections, plugin_states = publication_plugins.collect(rt, activity_id, language=language)
    # Only the sentence the coach marked public; never other coach, diary or health text.
    insight = publication_insight.for_activity(rt.engine, activity_id)
    if insight:
        block_id = evidence.digest({"block": block_id, "insight": insight["entry_id"]})
    body = publication_format.compose(
        insight=insight["text"] if insight else None,
        stats=block["stats"],
        weather=sections.get("weather"),
        quote=sections.get("quote"),
        language=language,
        labels=labels,
    )
    owned = publication_format.wrap_owned(body or "", pub)
    if previous and previous["state"] == "pending":
        if owned != previous["owned_block"]:
            raise Conflict("The description changed during a pending write; review before retrying")
        # A provider that only rewrote newlines still holds the intended text.
        description = (
            current
            if publication_format.same_text(current, previous["description"])
            else previous["description"]
        )
    else:
        description = _compose(current, owned, previous)
    if publication_format.same_text(description, current):
        description = current
    version = evidence.digest(
        {
            "source": destination,
            "id": source_id,
            "base": current,
            "description": description,
            "block": block_id,
        }
    )
    if previous and previous["state"] == "pending":
        version = previous["preview_version"]
    return {
        "state": "ready",
        "destination": destination,
        "source_id": source_id,
        "assessment_id": block_id,
        "preview_version": version,
        "description": description,
        "current_description": current,
        "owned_block": owned,
        "plugins": plugin_states,
        "insight": (
            {"state": "present", "entry_id": insight["entry_id"]}
            if insight
            else {"state": "missing"}
        ),
        "already_published": description == current,
        "undo_description": _without_owned(current) if description == current else None,
    }


def _newline_span(text: str, index: int, *, forward: bool) -> int:
    """Bytes in one newline at ``index`` (``\\r\\n``, ``\\n`` or ``\\r``), or 0."""
    if forward:
        if text.startswith("\r\n", index):
            return 2
        if index < len(text) and text[index] in "\r\n":
            return 1
        return 0
    if text.endswith("\r\n", 0, index):
        return 2
    if index > 0 and text[index - 1] in "\r\n":
        return 1
    return 0


def _newline_count(text: str, *, leading: bool) -> int:
    index = 0 if leading else len(text)
    count = 0
    while True:
        size = _newline_span(text, index, forward=leading)
        if not size:
            return count
        index = index + size if leading else index - size
        count += 1


def _drop_newlines(text: str, count: int, *, leading: bool) -> str:
    index = 0 if leading else len(text)
    for _ in range(count):
        size = _newline_span(text, index, forward=leading)
        if not size:
            break
        index = index + size if leading else index - size
    return text[index:] if leading else text[:index]


def _join_without_block_separator(prefix: str, suffix: str) -> str:
    """Drop the one blank line (or single newline) that joined the owned block."""
    if _newline_count(prefix, leading=False) >= 2:
        prefix = _drop_newlines(prefix, 2, leading=False)
    elif _newline_count(suffix, leading=True) >= 2:
        suffix = _drop_newlines(suffix, 2, leading=True)
    elif _newline_count(prefix, leading=False) >= 1:
        prefix = _drop_newlines(prefix, 1, leading=False)
    elif _newline_count(suffix, leading=True) >= 1:
        suffix = _drop_newlines(suffix, 1, leading=True)
    return prefix + suffix


def _without_owned(current: str, owned: str | None = None) -> str:
    """The description without the owned block and one joining separator."""
    blocks = publication_format.find_blocks(current)
    if len(blocks) == 1:
        block = publication_format.block_text(current, blocks[0])
        wrapped = f"\n\n{block}\n\n"
        if wrapped in current:
            return current.replace(wrapped, "\n\n", 1)
        start, end = blocks[0]
        return _join_without_block_separator(current[:start], current[end:])
    if current == owned:
        return ""
    if owned and current.endswith("\n\n" + owned):
        return current[: -(len(owned) + 2)]
    if owned and ("\n\n" + owned + "\n\n") in current:
        return current.replace("\n\n" + owned + "\n\n", "\n\n", 1)
    if owned and current.startswith(owned + "\n\n"):
        return current[len(owned) + 2 :]
    if owned:
        return current.replace(owned, "", 1)
    return current


def _checkpoint(rt, item: dict, state: str) -> None:
    values = {
        "source": item["destination"],
        "source_id": item["source_id"],
        "assessment_id": item["assessment_id"],
        "preview_version": item["preview_version"],
        "owned_block": item["owned_block"],
        "base_description": item["current_description"],
        "description": item["description"],
        "state": state,
        "updated_at": datetime.now(UTC),
    }
    with rt.engine.begin() as conn:
        conn.execute(
            publication_ledger.delete().where(
                publication_ledger.c.source == values["source"],
                publication_ledger.c.source_id == values["source_id"],
            )
        )
        conn.execute(publication_ledger.insert().values(**values))


def _delete_checkpoint(rt, destination: str, source_id: str) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            publication_ledger.delete().where(
                publication_ledger.c.source == destination,
                publication_ledger.c.source_id == source_id,
            )
        )


def publish(
    rt,
    activity_id: str,
    destination: str,
    expected_version: str,
    *,
    language: str = "en",
    client=None,
    mirror: bool = True,
) -> dict:
    """User-triggered only. A retry verifies an interrupted write before writing again.

    A verified Strava publication also goes to the intervals.icu description
    (publication_icu), mirrors to Garmin and starts the 7-day watch (publication_mirror);
    these run after the Strava lock is released.
    """
    result = _publish(
        rt, activity_id, destination, expected_version, language=language, client=client
    )
    if destination == "strava":
        # The same block in the intervals.icu description (publication_icu); never fails this.
        from vibesynergy.query import publication_icu

        result["icu"] = publication_icu.after_publish(rt, activity_id)
    if destination == "strava" and mirror:
        from vibesynergy.query import publication_mirror

        if publication_mirror.enabled(rt):
            result["mirror"] = publication_mirror.after_publish(
                rt, activity_id, language, strava=client
            )
        else:
            result["mirror"] = {"state": "disabled"}
    return result


def _publish(
    rt,
    activity_id: str,
    destination: str,
    expected_version: str,
    *,
    language: str = "en",
    client=None,
) -> dict:
    source_id = _source_id(rt, activity_id, destination)
    lock = SecretStore(rt.settings.secrets_dir, "publication")
    with lock.lock(f"{destination}-{source_id}"):
        client = client or provider(rt, destination)
        previous = _ledger(rt, destination, source_id)
        item = preview(rt, activity_id, destination, language=language, client=client)
        if item["state"] != "ready":
            raise Conflict("Garmin statistics are required before publication")
        if item["preview_version"] != expected_version:
            raise Conflict("The description changed; preview it again before publishing")
        if item["already_published"]:
            if previous and previous["state"] == "pending":
                _checkpoint(rt, item, "verified")
            return {"state": "verified", "destination": destination, "source_id": source_id}
        _checkpoint(rt, item, "pending")
        client.write(source_id, item["description"])
        after = client.read(source_id)
        id_key = "id" if destination == "strava" else "activityId"
        returned = after.get("description")
        if str(after.get(id_key)) != source_id or not publication_format.same_text(
            returned if isinstance(returned, str) else "", item["description"]
        ):
            raise Conflict(
                "The provider did not return the saved description; review before retrying"
            )
        if isinstance(returned, str) and returned != item["description"]:
            item = {**item, "description": returned}
        _checkpoint(rt, item, "verified")
        if previous and previous["owned_block"] != item["owned_block"]:
            _audit_update(rt, item, previous)
        return {"state": "verified", "destination": destination, "source_id": source_id}


def _audit_update(rt, item: dict, previous: dict) -> None:
    """An already published VibeSynergy block was replaced (new insight, figures or format)."""
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="vibesynergy",
                action="publication.update",
                target=f"{item['destination']}:{item['source_id']}",
                outcome="ok",
                detail={
                    "previous_version": previous["preview_version"],
                    "preview_version": item["preview_version"],
                    "insight": (item.get("insight") or {}).get("entry_id"),
                },
            )
        )


def unpublish(
    rt,
    activity_id: str,
    destination: str,
    expected_version: str,
    *,
    language: str = "en",
    client=None,
) -> dict:
    """Remove only the exact owned block, with the same guarded read-back and retry.

    Removing the Strava block stops the mirror watch and removes the mirrored Garmin block
    as well; both providers keep their text.
    """
    if destination != "strava":
        return _unpublish(
            rt, activity_id, destination, expected_version, language=language, client=client
        )
    from vibesynergy.query import publication_mirror

    with publication_mirror.stopping(rt, activity_id):
        result = _unpublish(
            rt, activity_id, destination, expected_version, language=language, client=client
        )
    if destination == "strava":
        try:
            result["mirror"] = publication_mirror.after_unpublish(rt, activity_id, language)
        except Exception as exc:  # the Strava removal itself is verified and stays
            result["mirror"] = {"state": "removed", "garmin": "failed", "error": str(exc)[:300]}
        result["icu"] = _remove_icu(rt, activity_id)
    return result


def _remove_icu(rt, activity_id: str) -> dict:
    from vibesynergy.query import publication_icu

    try:
        return publication_icu.remove(rt, activity_id)
    except Exception as exc:  # the Strava removal itself is verified and stays
        return {"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}


def _unpublish(
    rt,
    activity_id: str,
    destination: str,
    expected_version: str,
    *,
    language: str = "en",
    client=None,
) -> dict:
    source_id = _source_id(rt, activity_id, destination)
    lock = SecretStore(rt.settings.secrets_dir, "publication")
    with lock.lock(f"{destination}-{source_id}"):
        client = client or provider(rt, destination)
        previous = _ledger(rt, destination, source_id)
        if not previous:
            raise Conflict("No verified VibeSynergy section exists on this activity")
        if previous["state"] == "removing":
            if expected_version != previous["preview_version"]:
                raise Conflict("Preview the description again before removing it")
            current = client.read(source_id)
            id_key = "id" if destination == "strava" else "activityId"
            if str(current.get(id_key)) != source_id:
                raise Conflict("Provider activity identity changed")
            text = current.get("description") or ""
            if publication_format.same_text(text, previous["description"]):
                _delete_checkpoint(rt, destination, source_id)
                return {"state": "removed", "destination": destination, "source_id": source_id}
            if not publication_format.same_text(text, previous["base_description"]):
                raise Conflict("Description changed during removal; review it before retrying")
            target = previous["description"]
        else:
            item = preview(rt, activity_id, destination, language=language, client=client)
            if item["state"] != "ready" or not item["already_published"]:
                raise Conflict("Only a verified unchanged section can be removed")
            if item["preview_version"] != expected_version:
                raise Conflict("Preview the description again before removing it")
            target = item["undo_description"]
            _checkpoint(rt, {**item, "description": target}, "removing")
        client.write(source_id, target)
        after = client.read(source_id)
        id_key = "id" if destination == "strava" else "activityId"
        returned = after.get("description") or ""
        if str(after.get(id_key)) != source_id or not publication_format.same_text(
            returned if isinstance(returned, str) else "", target
        ):
            raise Conflict("The provider did not confirm removal; review before retrying")
        _delete_checkpoint(rt, destination, source_id)
        return {"state": "removed", "destination": destination, "source_id": source_id}
