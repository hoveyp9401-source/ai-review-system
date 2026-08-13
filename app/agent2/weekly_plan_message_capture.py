"""Turn Agent2's grounded weekly-plan reading into a safe domain candidate.

Natural-language interpretation stays with Agent2.  This module only checks the
trusted original-message evidence, the resolved target-week boundary, and the
deterministic write shape.  It deliberately does not look for words such as
``下周`` or try to calculate a weekday from a sentence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from app.agent2.weekly_plan_models import WeeklyPlan, WeeklyPlanCommand
from app.agent2.weekly_plan_suggestions import (
    TrustedSourceKind,
    TrustedSuggestionEvidence,
    WeeklyPlanSuggestion,
    create_suggestion,
)


class WeeklyPlanCaptureKind(str, Enum):
    """The two meanings Agent2 may ground in a user's original message."""

    DATED_PLAN = "dated_plan"
    UNDATED_SUGGESTION = "undated_suggestion"


@dataclass(frozen=True)
class ModelWeeklyPlanCandidate:
    """A model-resolved meaning; all text fields must remain verbatim quotes."""

    kind: WeeklyPlanCaptureKind
    target_week_start: date
    matter_exact_quote: str
    temporal_exact_quote: str
    plan_date: date | None = None
    date_is_ambiguous: bool = False


@dataclass(frozen=True)
class WeeklyPlanCaptureOutcome:
    status: Literal["materialized", "rejected"]
    capture_id: str
    evidence: TrustedSuggestionEvidence
    command: WeeklyPlanCommand | None = None
    suggestion: WeeklyPlanSuggestion | None = None
    reason_code: str = ""


def materialize_weekly_plan_candidate(
    candidate: ModelWeeklyPlanCandidate,
    *,
    evidence: TrustedSuggestionEvidence,
    plan: WeeklyPlan,
    observed_at: datetime,
) -> WeeklyPlanCaptureOutcome:
    """Validate and materialize one grounded candidate without writing data.

    A dated candidate becomes the normal formal-draft command.  A candidate that
    says only which week it belongs to becomes an independent suggestion and
    therefore cannot enter a formal day until the user accepts it later.
    """

    capture_id = _capture_id(candidate, evidence)
    reason = _validate(candidate, evidence=evidence, plan=plan, observed_at=observed_at)
    if reason:
        return WeeklyPlanCaptureOutcome(
            status="rejected",
            capture_id=capture_id,
            evidence=evidence,
            reason_code=reason,
        )

    if candidate.kind is WeeklyPlanCaptureKind.DATED_PLAN:
        command = WeeklyPlanCommand(
            command_id=_stable_uuid("weekly-plan-message-command", capture_id),
            command_type="add_item",
            tenant_id=plan.tenant_id,
            actor_user_id=plan.owner_user_id,
            plan_id=plan.plan_id,
            expected_version=plan.version,
            idempotency_key=f"weekly-plan-message:{capture_id}",
            source_message_id=evidence.source_ref,
            patch={
                "plan_date": candidate.plan_date.isoformat(),  # type: ignore[union-attr]
                "original_text": candidate.matter_exact_quote,
                "source": "user_original_message:explicit_target_date",
                "source_ref": evidence.source_ref,
                "source_version": evidence.source_version,
                "source_exact_quote": evidence.evidence_text,
                "matter_exact_quote": candidate.matter_exact_quote,
                "temporal_exact_quote": candidate.temporal_exact_quote,
                "source_evidence_sha256": evidence.evidence_sha256,
            },
        )
        return WeeklyPlanCaptureOutcome(
            status="materialized",
            capture_id=capture_id,
            evidence=evidence,
            command=command,
        )

    expiry = datetime.combine(
        plan.target_week_start + timedelta(days=7),
        time.min,
        tzinfo=observed_at.tzinfo,
    )
    suggestion = create_suggestion(
        owner_user_id=plan.owner_user_id,
        target_week_start=plan.target_week_start,
        evidence=evidence,
        matter_excerpt=candidate.matter_exact_quote,
        created_at=observed_at,
        expires_at=expiry,
    )
    return WeeklyPlanCaptureOutcome(
        status="materialized",
        capture_id=capture_id,
        evidence=evidence,
        suggestion=suggestion,
    )


def _validate(
    candidate: ModelWeeklyPlanCandidate,
    *,
    evidence: TrustedSuggestionEvidence,
    plan: WeeklyPlan,
    observed_at: datetime,
) -> str:
    if not isinstance(candidate.kind, WeeklyPlanCaptureKind):
        return "unsupported_capture_kind"
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return "observed_at_timezone_required"
    if evidence.source_kind is not TrustedSourceKind.USER_ORIGINAL_MESSAGE:
        return "current_user_original_message_required"
    if evidence.owner_user_id != plan.owner_user_id:
        return "owner_mismatch"
    if candidate.target_week_start.weekday() != 0:
        return "target_week_not_monday"
    if candidate.target_week_start != plan.target_week_start:
        return "target_week_mismatch"
    if not candidate.matter_exact_quote.strip():
        return "matter_exact_quote_required"
    if candidate.matter_exact_quote not in evidence.evidence_text:
        return "matter_quote_not_in_source"
    if not candidate.temporal_exact_quote.strip():
        return "temporal_exact_quote_required"
    if candidate.temporal_exact_quote not in evidence.evidence_text:
        return "temporal_quote_not_in_source"
    if candidate.date_is_ambiguous:
        return "ambiguous_plan_date"

    if candidate.kind is WeeklyPlanCaptureKind.DATED_PLAN:
        if candidate.plan_date is None:
            return "exact_plan_date_required"
        if candidate.plan_date not in {day.plan_date for day in plan.days}:
            return "plan_date_outside_monday_to_saturday"
        return ""

    if candidate.plan_date is not None:
        return "undated_suggestion_cannot_have_plan_date"
    expiry = datetime.combine(
        plan.target_week_start + timedelta(days=7),
        time.min,
        tzinfo=observed_at.tzinfo,
    )
    if expiry <= observed_at:
        return "target_week_suggestion_expired"
    return ""


def _capture_id(
    candidate: ModelWeeklyPlanCandidate,
    evidence: TrustedSuggestionEvidence,
) -> str:
    payload = {
        "kind": (
            candidate.kind.value
            if isinstance(candidate.kind, WeeklyPlanCaptureKind)
            else str(candidate.kind)
        ),
        "target_week_start": candidate.target_week_start.isoformat(),
        "plan_date": candidate.plan_date.isoformat() if candidate.plan_date else None,
        "date_is_ambiguous": candidate.date_is_ambiguous,
        "matter_exact_quote": candidate.matter_exact_quote,
        "temporal_exact_quote": candidate.temporal_exact_quote,
        "owner_user_id": evidence.owner_user_id,
        "source_ref": evidence.source_ref,
        "source_version": evidence.source_version,
        "evidence_sha256": evidence.evidence_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"wpc_{hashlib.sha256(encoded).hexdigest()}"


def _stable_uuid(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{kind}:{value}"))


__all__ = [
    "ModelWeeklyPlanCandidate",
    "WeeklyPlanCaptureKind",
    "WeeklyPlanCaptureOutcome",
    "materialize_weekly_plan_candidate",
]
