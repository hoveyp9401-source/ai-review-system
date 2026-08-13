from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import hashlib

import pytest

from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    TrustedSourceKind,
    accept_suggestion,
    build_trusted_evidence,
    create_suggestion,
    expire_suggestions,
    is_eligible_for_formal_plan,
    reject_suggestion,
    render_suggestion_prompt,
    supersede_suggestion,
)


UTC = timezone.utc
CREATED_AT = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
TARGET_WEEK = date(2026, 8, 17)


def _evidence(*, version: str = "7"):
    return build_trusted_evidence(
        owner_user_id="user-user-a",
        source_kind=TrustedSourceKind.USER_ORIGINAL_MESSAGE,
        source_ref="daily-message:2026-08-13:42",
        source_version=version,
        evidence_text="合同评审规则还要继续优化，先把风险提示补齐。",
    )


def _suggestion(**overrides):
    values = {
        "owner_user_id": "user-user-a",
        "target_week_start": TARGET_WEEK,
        "evidence": _evidence(),
        "matter_excerpt": "合同评审规则还要继续优化",
        "created_at": CREATED_AT,
        "expires_at": EXPIRES_AT,
    }
    values.update(overrides)
    return create_suggestion(**values)


def test_candidate_keeps_exact_auditable_source_and_has_stable_identity() -> None:
    evidence = _evidence()

    first = _suggestion(evidence=evidence)
    replay = _suggestion(evidence=evidence)

    assert first.suggestion_id == replay.suggestion_id
    assert first.status is SuggestionStatus.AVAILABLE
    assert first.source_kind is TrustedSourceKind.USER_ORIGINAL_MESSAGE
    assert first.source_ref == "daily-message:2026-08-13:42"
    assert first.source_version == "7"
    assert first.evidence_text == "合同评审规则还要继续优化，先把风险提示补齐。"
    assert first.evidence_sha256 == hashlib.sha256(
        first.evidence_text.encode("utf-8")
    ).hexdigest()
    with pytest.raises(FrozenInstanceError):
        first.status = SuggestionStatus.ACCEPTED  # type: ignore[misc]


def test_only_the_same_users_trusted_original_or_confirmed_record_is_allowed() -> None:
    evidence = _evidence()
    with pytest.raises(ValueError, match="本人"):
        _suggestion(owner_user_id="another-user", evidence=evidence)

    with pytest.raises(ValueError, match="可信来源"):
        build_trusted_evidence(
            owner_user_id="user-user-a",
            source_kind="assistant_summary",  # type: ignore[arg-type]
            source_ref="assistant:summary:1",
            source_version="1",
            evidence_text="机器人认为该事项尚未完成",
        )


def test_display_matter_must_be_a_verbatim_excerpt_not_an_ai_summary() -> None:
    with pytest.raises(ValueError, match="原文片段"):
        _suggestion(matter_excerpt="完成合同评审规则优化")


def test_prompt_is_personable_but_does_not_claim_the_work_is_unfinished() -> None:
    text = render_suggestion_prompt(_suggestion())

    assert "你本周提到过“合同评审规则还要继续优化”" in text
    assert "我暂时没找到后续记录" in text
    assert "要不要把它放到下周某一天" in text
    assert "如果已经处理完，也可以告诉我" in text
    assert "未完成" not in text
    assert "没有完成" not in text
    assert "还没做" not in text


def test_suggestion_never_enters_formal_plan_before_explicit_acceptance() -> None:
    available = _suggestion()
    rejected = reject_suggestion(
        available,
        decision_ref="user-message:reject:1",
        decided_at=CREATED_AT + timedelta(minutes=2),
    )
    accepted = accept_suggestion(
        available,
        decision_ref="user-message:accept:1",
        decided_at=CREATED_AT + timedelta(minutes=1),
        accepted_item_id="weekly-plan-item:2026-08-17:1",
    )

    assert not is_eligible_for_formal_plan(available)
    assert not is_eligible_for_formal_plan(rejected)
    assert is_eligible_for_formal_plan(accepted)
    assert accepted.decision_ref == "user-message:accept:1"
    assert accepted.accepted_item_id == "weekly-plan-item:2026-08-17:1"
    assert accepted.evidence_sha256 == available.evidence_sha256


def test_expiration_is_deterministic_and_only_changes_available_candidates() -> None:
    available = _suggestion()
    before, = expire_suggestions(
        (available,), as_of=EXPIRES_AT - timedelta(microseconds=1)
    )
    expired, = expire_suggestions((available,), as_of=EXPIRES_AT)

    assert before is available
    assert expired.status is SuggestionStatus.EXPIRED
    assert expired.decided_at == EXPIRES_AT
    assert not is_eligible_for_formal_plan(expired)

    accepted = accept_suggestion(
        available,
        decision_ref="user-message:accept:1",
        decided_at=CREATED_AT + timedelta(minutes=1),
        accepted_item_id="weekly-plan-item:2026-08-17:1",
    )
    still_accepted, = expire_suggestions((accepted,), as_of=EXPIRES_AT)
    assert still_accepted is accepted


def test_new_source_version_can_explicitly_supersede_an_available_candidate() -> None:
    old = _suggestion()
    new = _suggestion(evidence=_evidence(version="8"))

    superseded = supersede_suggestion(
        old,
        replacement=new,
        decided_at=CREATED_AT + timedelta(hours=1),
    )

    assert superseded.status is SuggestionStatus.SUPERSEDED
    assert superseded.superseded_by_id == new.suggestion_id
    assert new.status is SuggestionStatus.AVAILABLE
    assert not is_eligible_for_formal_plan(superseded)


@pytest.mark.parametrize(
    "target_week_start,created_at,expires_at,error",
    [
        (date(2026, 8, 18), CREATED_AT, EXPIRES_AT, "周一"),
        (
            TARGET_WEEK,
            datetime(2026, 8, 14, 8, 0),
            EXPIRES_AT,
            "时区",
        ),
        (
            TARGET_WEEK,
            CREATED_AT,
            datetime(2026, 8, 23, 12, 0),
            "时区",
        ),
        (TARGET_WEEK, CREATED_AT, CREATED_AT, "晚于"),
    ],
)
def test_candidate_rejects_ambiguous_week_or_time_boundaries(
    target_week_start: date,
    created_at: datetime,
    expires_at: datetime,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        _suggestion(
            target_week_start=target_week_start,
            created_at=created_at,
            expires_at=expires_at,
        )


def test_terminal_status_cannot_be_rewritten_by_another_user_decision() -> None:
    accepted = accept_suggestion(
        _suggestion(),
        decision_ref="user-message:accept:1",
        decided_at=CREATED_AT + timedelta(minutes=1),
        accepted_item_id="weekly-plan-item:2026-08-17:1",
    )

    with pytest.raises(ValueError, match="available"):
        reject_suggestion(
            accepted,
            decision_ref="user-message:reject:later",
            decided_at=CREATED_AT + timedelta(minutes=2),
        )


@pytest.mark.parametrize("accepted_item_id", ["", "   "])
def test_acceptance_requires_a_nonempty_materialized_plan_item(
    accepted_item_id: str,
) -> None:
    with pytest.raises(ValueError, match="正式计划项"):
        accept_suggestion(
            _suggestion(),
            decision_ref="user-message:accept:1",
            decided_at=CREATED_AT + timedelta(minutes=1),
            accepted_item_id=accepted_item_id,
        )


@pytest.mark.parametrize(
    "status,extra",
    [
        (SuggestionStatus.AVAILABLE, {}),
        (
            SuggestionStatus.REJECTED,
            {
                "decision_ref": "user-message:reject:1",
                "decided_at": CREATED_AT + timedelta(minutes=1),
            },
        ),
        (
            SuggestionStatus.EXPIRED,
            {"decided_at": EXPIRES_AT},
        ),
        (
            SuggestionStatus.SUPERSEDED,
            {
                "decided_at": CREATED_AT + timedelta(minutes=1),
                "superseded_by_id": "wps_replacement",
            },
        ),
    ],
)
def test_nonaccepted_statuses_cannot_reference_a_formal_plan_item(
    status: SuggestionStatus,
    extra: dict[str, object],
) -> None:
    available = _suggestion()

    with pytest.raises(ValueError, match="accepted"):
        type(available)(
            suggestion_id=available.suggestion_id,
            owner_user_id=available.owner_user_id,
            target_week_start=available.target_week_start,
            evidence=available.evidence,
            matter_excerpt=available.matter_excerpt,
            created_at=available.created_at,
            expires_at=available.expires_at,
            status=status,
            accepted_item_id="weekly-plan-item:2026-08-17:1",
            **extra,
        )
