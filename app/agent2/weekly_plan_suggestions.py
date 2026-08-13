"""Pure domain rules for optional weekly-plan suggestions.

This module deliberately performs no database writes and does not interpret natural
language.  Agent2 supplies an exact excerpt from a trusted, user-owned source; the
rules here only validate provenance, preserve evidence, and manage deterministic
candidate states.  An available suggestion is never eligible for the formal plan
until the user explicitly accepts it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum


class TrustedSourceKind(str, Enum):
    """Sources allowed to support a suggestion.

    Both kinds must belong to the same user as the candidate.  Assistant-generated
    summaries are intentionally absent from this allow-list.
    """

    USER_ORIGINAL_MESSAGE = "user_original_message"
    CONFIRMED_RECORD = "confirmed_record"


class SuggestionStatus(str, Enum):
    AVAILABLE = "available"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class TrustedSuggestionEvidence:
    owner_user_id: str
    source_kind: TrustedSourceKind
    source_ref: str
    source_version: str
    evidence_text: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.owner_user_id, "证据本人标识")
        if not isinstance(self.source_kind, TrustedSourceKind):
            raise ValueError("建议证据不是允许的可信来源")  # noqa: TRY004
        _require_text(self.source_ref, "证据来源引用")
        _require_text(self.source_version, "证据来源版本")
        _require_text(self.evidence_text, "证据原文")
        expected_hash = _sha256_text(self.evidence_text)
        if self.evidence_sha256 != expected_hash:
            raise ValueError("证据原文哈希与原文不一致")


@dataclass(frozen=True)
class WeeklyPlanSuggestion:
    suggestion_id: str
    owner_user_id: str
    target_week_start: date
    evidence: TrustedSuggestionEvidence
    matter_excerpt: str
    created_at: datetime
    expires_at: datetime
    status: SuggestionStatus = SuggestionStatus.AVAILABLE
    decision_ref: str | None = None
    decided_at: datetime | None = None
    superseded_by_id: str | None = None
    accepted_item_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.suggestion_id, "建议标识")
        _require_text(self.owner_user_id, "建议本人标识")
        if self.owner_user_id != self.evidence.owner_user_id:
            raise ValueError("建议只能使用本人可信证据")
        _validate_target_week(self.target_week_start)
        _require_aware_datetime(self.created_at, "建议创建时间")
        _require_aware_datetime(self.expires_at, "建议过期时间")
        if self.expires_at <= self.created_at:
            raise ValueError("建议过期时间必须晚于创建时间")
        excerpt = _require_text(self.matter_excerpt, "建议事项原文片段")
        if excerpt not in self.evidence.evidence_text:
            raise ValueError("建议事项必须是证据中的原文片段，不能使用机器人总结")
        if not isinstance(self.status, SuggestionStatus):
            raise ValueError("建议状态无效")  # noqa: TRY004
        self._validate_status_fields()

    @property
    def source_kind(self) -> TrustedSourceKind:
        return self.evidence.source_kind

    @property
    def source_ref(self) -> str:
        return self.evidence.source_ref

    @property
    def source_version(self) -> str:
        return self.evidence.source_version

    @property
    def evidence_text(self) -> str:
        return self.evidence.evidence_text

    @property
    def evidence_sha256(self) -> str:
        return self.evidence.evidence_sha256

    def _validate_status_fields(self) -> None:
        if (
            self.accepted_item_id is not None
            and self.status is not SuggestionStatus.ACCEPTED
        ):
            raise ValueError("only an accepted suggestion may reference a plan item")
        if self.status is SuggestionStatus.AVAILABLE:
            if any(
                value is not None
                for value in (
                    self.decision_ref,
                    self.decided_at,
                    self.superseded_by_id,
                    self.accepted_item_id,
                )
            ):
                raise ValueError("available 建议不能带有处理结果")
            return

        if self.decided_at is None:
            raise ValueError("已处理建议必须记录处理时间")
        _require_aware_datetime(self.decided_at, "建议处理时间")
        if self.decided_at < self.created_at:
            raise ValueError("建议处理时间不能早于创建时间")

        if self.status is SuggestionStatus.ACCEPTED:
            _require_text(self.decision_ref, "用户决定来源引用")
            _require_text(self.accepted_item_id, "接受后生成的正式计划项标识")
            if self.superseded_by_id is not None:
                raise ValueError("用户决定不能同时标记为被替代")
            return

        if self.status is SuggestionStatus.REJECTED:
            _require_text(self.decision_ref, "用户决定来源引用")
            if self.superseded_by_id is not None:
                raise ValueError("用户决定不能同时标记为被替代")
            return

        if self.decision_ref is not None:
            raise ValueError("自动状态变化不能伪装成用户决定")
        if self.status is SuggestionStatus.EXPIRED:
            if self.superseded_by_id is not None:
                raise ValueError("过期建议不能同时标记为被替代")
            return
        if self.status is SuggestionStatus.SUPERSEDED:
            _require_text(self.superseded_by_id, "替代建议标识")


def build_trusted_evidence(
    *,
    owner_user_id: str,
    source_kind: TrustedSourceKind,
    source_ref: str,
    source_version: str,
    evidence_text: str,
) -> TrustedSuggestionEvidence:
    """Build evidence while hashing the exact preserved source text."""

    if not isinstance(source_kind, TrustedSourceKind):
        raise ValueError("建议证据不是允许的可信来源")  # noqa: TRY004
    return TrustedSuggestionEvidence(
        owner_user_id=owner_user_id,
        source_kind=source_kind,
        source_ref=source_ref,
        source_version=source_version,
        evidence_text=evidence_text,
        evidence_sha256=_sha256_text(evidence_text),
    )


def create_suggestion(
    *,
    owner_user_id: str,
    target_week_start: date,
    evidence: TrustedSuggestionEvidence,
    matter_excerpt: str,
    created_at: datetime,
    expires_at: datetime,
) -> WeeklyPlanSuggestion:
    """Create an idempotent available candidate from an exact evidence excerpt."""

    suggestion_id = _stable_suggestion_id(
        owner_user_id=owner_user_id,
        target_week_start=target_week_start,
        evidence=evidence,
        matter_excerpt=matter_excerpt,
    )
    return WeeklyPlanSuggestion(
        suggestion_id=suggestion_id,
        owner_user_id=owner_user_id,
        target_week_start=target_week_start,
        evidence=evidence,
        matter_excerpt=matter_excerpt,
        created_at=created_at,
        expires_at=expires_at,
    )


def render_suggestion_prompt(suggestion: WeeklyPlanSuggestion) -> str:
    """Render a personable prompt without asserting that work is unfinished."""

    _require_available(suggestion)
    return (
        f"你本周提到过“{suggestion.matter_excerpt}”，"
        "我暂时没找到后续记录。要不要把它放到下周某一天？"
        "如果已经处理完，也可以告诉我。"
    )


def accept_suggestion(
    suggestion: WeeklyPlanSuggestion,
    *,
    decision_ref: str,
    decided_at: datetime,
    accepted_item_id: str,
) -> WeeklyPlanSuggestion:
    """Link explicit acceptance to an already materialized formal plan item."""

    _validate_user_decision(suggestion, decision_ref, decided_at)
    _require_text(accepted_item_id, "接受后生成的正式计划项标识")
    return replace(
        suggestion,
        status=SuggestionStatus.ACCEPTED,
        decision_ref=decision_ref,
        decided_at=decided_at,
        accepted_item_id=accepted_item_id,
    )


def reject_suggestion(
    suggestion: WeeklyPlanSuggestion,
    *,
    decision_ref: str,
    decided_at: datetime,
) -> WeeklyPlanSuggestion:
    """Record an explicit user rejection without changing the source evidence."""

    _validate_user_decision(suggestion, decision_ref, decided_at)
    return replace(
        suggestion,
        status=SuggestionStatus.REJECTED,
        decision_ref=decision_ref,
        decided_at=decided_at,
    )


def expire_suggestions(
    suggestions: Iterable[WeeklyPlanSuggestion], *, as_of: datetime
) -> tuple[WeeklyPlanSuggestion, ...]:
    """Expire due available candidates, preserving order and terminal states."""

    _require_aware_datetime(as_of, "建议过期检查时间")
    result: list[WeeklyPlanSuggestion] = []
    for suggestion in suggestions:
        if (
            suggestion.status is SuggestionStatus.AVAILABLE
            and as_of >= suggestion.expires_at
        ):
            result.append(
                replace(
                    suggestion,
                    status=SuggestionStatus.EXPIRED,
                    decided_at=as_of,
                )
            )
        else:
            result.append(suggestion)
    return tuple(result)


def supersede_suggestion(
    suggestion: WeeklyPlanSuggestion,
    *,
    replacement: WeeklyPlanSuggestion,
    decided_at: datetime,
) -> WeeklyPlanSuggestion:
    """Link an available candidate to a newer candidate for the same source."""

    _require_available(suggestion)
    _require_available(replacement)
    _require_aware_datetime(decided_at, "建议替代时间")
    if decided_at < suggestion.created_at:
        raise ValueError("建议替代时间不能早于创建时间")
    if suggestion.suggestion_id == replacement.suggestion_id:
        raise ValueError("替代建议必须是不同版本")
    if (
        suggestion.owner_user_id != replacement.owner_user_id
        or suggestion.target_week_start != replacement.target_week_start
        or suggestion.source_kind is not replacement.source_kind
        or suggestion.source_ref != replacement.source_ref
    ):
        raise ValueError("只能由同一本人、目标周和可信来源的新版本替代")
    if suggestion.source_version == replacement.source_version:
        raise ValueError("替代建议必须记录新的来源版本")
    return replace(
        suggestion,
        status=SuggestionStatus.SUPERSEDED,
        decided_at=decided_at,
        superseded_by_id=replacement.suggestion_id,
    )


def is_eligible_for_formal_plan(suggestion: WeeklyPlanSuggestion) -> bool:
    """Only explicit acceptance permits later formal-plan materialization."""

    return suggestion.status is SuggestionStatus.ACCEPTED


def _validate_user_decision(
    suggestion: WeeklyPlanSuggestion, decision_ref: str, decided_at: datetime
) -> None:
    _require_available(suggestion)
    _require_text(decision_ref, "用户决定来源引用")
    _require_aware_datetime(decided_at, "用户决定时间")
    if decided_at < suggestion.created_at:
        raise ValueError("用户决定时间不能早于建议创建时间")
    if decided_at >= suggestion.expires_at:
        raise ValueError("建议已经到期，不能再接受或拒绝")


def _require_available(suggestion: WeeklyPlanSuggestion) -> None:
    if suggestion.status is not SuggestionStatus.AVAILABLE:
        raise ValueError("只有 available 建议可以进行该操作")


def _stable_suggestion_id(
    *,
    owner_user_id: str,
    target_week_start: date,
    evidence: TrustedSuggestionEvidence,
    matter_excerpt: str,
) -> str:
    payload = {
        "owner_user_id": owner_user_id,
        "target_week_start": target_week_start.isoformat(),
        "source_kind": evidence.source_kind.value,
        "source_ref": evidence.source_ref,
        "source_version": evidence.source_version,
        "evidence_sha256": evidence.evidence_sha256,
        "matter_excerpt": matter_excerpt,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"wps_{hashlib.sha256(encoded).hexdigest()}"


def _sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("证据原文必须是文本")  # noqa: TRY004
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}不能为空")
    return value


def _require_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name}必须包含时区")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name}必须包含时区")
    return value


def _validate_target_week(value: object) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError("目标周必须使用日期")  # noqa: TRY004
    if value.weekday() != 0:
        raise ValueError("目标周起始日必须是周一")
    return value


__all__ = [
    "SuggestionStatus",
    "TrustedSourceKind",
    "TrustedSuggestionEvidence",
    "WeeklyPlanSuggestion",
    "accept_suggestion",
    "build_trusted_evidence",
    "create_suggestion",
    "expire_suggestions",
    "is_eligible_for_formal_plan",
    "reject_suggestion",
    "render_suggestion_prompt",
    "supersede_suggestion",
]
