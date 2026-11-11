"""Evidence required before a future external calendar or workout writer may act.

These value objects do not adopt imported objects or grant write access by themselves.
The future ledger must persist and verify the claim against the provider before use.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from vibesynergy.query import coach, evidence


class ExternalObjectRef(coach.StrictModel):
    provider: str = Field(min_length=1)
    account: str = Field(min_length=1)
    calendar: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    etag: str | None = None
    version_token: str | None = None


class OwnershipClaim(coach.StrictModel):
    target: ExternalObjectRef
    origin: Literal["vibesynergy", "garmin_coach", "personal", "unknown"]
    origin_id: str | None = None
    ledger_id: str | None = None
    verified: bool = False

    @model_validator(mode="after")
    def require_origin_record(self) -> OwnershipClaim:
        if (
            self.verified
            and self.origin == "vibesynergy"
            and not (self.origin_id and self.ledger_id)
        ):
            raise ValueError("verified VibeSynergy ownership requires origin and ledger IDs")
        return self


class CreateIntent(coach.StrictModel):
    """Identity of one proposed create before the provider assigns an object ID."""

    provider: str = Field(min_length=1)
    account: str = Field(min_length=1)
    calendar: str = Field(min_length=1)
    origin_id: str = Field(min_length=1)
    ledger_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)


def create_idempotency_key(intent: CreateIntent) -> str:
    """The ledger must reserve this key before sending the first create request."""
    return evidence.digest({"contract": 1, "action": "create", **intent.model_dump()})


def can_write(claim: OwnershipClaim) -> bool:
    """Only a verified VibeSynergy-origin claim is eligible for a later authorization check."""
    return bool(
        claim.verified
        and claim.origin == "vibesynergy"
        and claim.origin_id
        and claim.ledger_id
        and (claim.target.etag or claim.target.version_token)
    )


def idempotency_key(claim: OwnershipClaim, action: str, intent_id: str) -> str:
    """Stable operation identity; titles, dates and sport matches are deliberately absent."""
    if not can_write(claim) or not action or not intent_id:
        raise ValueError("verified owner evidence, action and intent are required")
    return evidence.digest(
        {
            "contract": 1,
            "target": claim.target.model_dump(exclude={"etag", "version_token"}),
            "origin_id": claim.origin_id,
            "ledger_id": claim.ledger_id,
            "action": action,
            "intent_id": intent_id,
        }
    )
