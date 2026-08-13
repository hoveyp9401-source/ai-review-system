from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib

from app.agent2.weekly_plan_domain import (
    create_weekly_plan,
    create_weekly_plan_batch,
    execute_weekly_plan_command,
)
from app.agent2.weekly_plan_message_capture import (
    ModelWeeklyPlanCandidate,
    WeeklyPlanCaptureKind,
    materialize_weekly_plan_candidate,
)
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_suggestions import (
    TrustedSourceKind,
    build_trusted_evidence,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
TARGET_WEEK = date(2026, 8, 17)


def _plan():
    batch = create_weekly_plan_batch(
        tenant_id="tenant-demo",
        target_week_start=TARGET_WEEK,
        roster=(WeeklyPlanRosterMember("user-demo", "示例用户"),),
        created_at=NOW,
    )
    return create_weekly_plan(
        batch=batch,
        owner_user_id="user-demo",
        created_at=NOW,
    )


def _evidence(text: str):
    return build_trusted_evidence(
        owner_user_id="user-demo",
        source_kind=TrustedSourceKind.USER_ORIGINAL_MESSAGE,
        source_ref="daily-message-demo-42",
        source_version="1",
        evidence_text=text,
    )


def test_explicit_next_week_day_becomes_a_formal_draft_command_with_verbatim_evidence() -> None:
    text = "今天完成了规则校对。下周三整理技能比赛材料。"
    evidence = _evidence(text)
    candidate = ModelWeeklyPlanCandidate(
        kind=WeeklyPlanCaptureKind.DATED_PLAN,
        target_week_start=TARGET_WEEK,
        plan_date=date(2026, 8, 19),
        temporal_exact_quote="下周三",
        matter_exact_quote="整理技能比赛材料",
    )

    outcome = materialize_weekly_plan_candidate(
        candidate,
        evidence=evidence,
        plan=_plan(),
        observed_at=NOW,
    )

    assert outcome.status == "materialized"
    assert outcome.command is not None
    assert outcome.suggestion is None
    assert outcome.command.command_type == "add_item"
    assert outcome.command.source_message_id == "daily-message-demo-42"
    assert outcome.command.patch == {
        "plan_date": "2026-08-19",
        "original_text": "整理技能比赛材料",
        "source": "user_original_message:explicit_target_date",
        "source_ref": "daily-message-demo-42",
        "source_version": "1",
        "source_exact_quote": text,
        "matter_exact_quote": "整理技能比赛材料",
        "temporal_exact_quote": "下周三",
        "source_evidence_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    execution = execute_weekly_plan_command(
        outcome.command,
        plan=_plan(),
        executed_at=NOW,
    )
    assert execution.receipt.status == "executed"
    assert execution.after.days[2].items[0].original_text == "整理技能比赛材料"
    assert execution.after.days[2].items[0].source_ref == "daily-message-demo-42"


def test_next_week_without_a_day_stays_in_the_independent_suggestion_area() -> None:
    text = "下周继续优化合同评审规则。"
    evidence = _evidence(text)
    candidate = ModelWeeklyPlanCandidate(
        kind=WeeklyPlanCaptureKind.UNDATED_SUGGESTION,
        target_week_start=TARGET_WEEK,
        plan_date=None,
        temporal_exact_quote="下周",
        matter_exact_quote="继续优化合同评审规则",
    )

    outcome = materialize_weekly_plan_candidate(
        candidate,
        evidence=evidence,
        plan=_plan(),
        observed_at=NOW,
    )

    assert outcome.status == "materialized"
    assert outcome.command is None
    assert outcome.suggestion is not None
    assert outcome.suggestion.matter_excerpt == "继续优化合同评审规则"
    assert outcome.suggestion.source_ref == "daily-message-demo-42"
    assert outcome.suggestion.source_version == "1"
    assert outcome.suggestion.evidence_text == text
    assert outcome.suggestion.evidence_sha256 == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_replaying_the_same_grounded_message_candidate_is_idempotent() -> None:
    text = "下周五向项目组汇报。"
    evidence = _evidence(text)
    candidate = ModelWeeklyPlanCandidate(
        kind=WeeklyPlanCaptureKind.DATED_PLAN,
        target_week_start=TARGET_WEEK,
        plan_date=date(2026, 8, 21),
        temporal_exact_quote="下周五",
        matter_exact_quote="向项目组汇报",
    )

    first = materialize_weekly_plan_candidate(
        candidate, evidence=evidence, plan=_plan(), observed_at=NOW
    )
    replay = materialize_weekly_plan_candidate(
        candidate, evidence=evidence, plan=_plan(), observed_at=NOW + timedelta(hours=1)
    )

    assert replay.capture_id == first.capture_id
    assert replay.command is not None
    assert first.command is not None
    assert replay.command.command_id == first.command.command_id
    assert replay.command.idempotency_key == first.command.idempotency_key


def test_ambiguous_or_out_of_scope_date_is_rejected_instead_of_guessed() -> None:
    text = "下周找一天整理材料，周末也可以。"
    evidence = _evidence(text)
    ambiguous = ModelWeeklyPlanCandidate(
        kind=WeeklyPlanCaptureKind.DATED_PLAN,
        target_week_start=TARGET_WEEK,
        plan_date=date(2026, 8, 22),
        temporal_exact_quote="下周找一天",
        matter_exact_quote="整理材料",
        date_is_ambiguous=True,
    )
    sunday = ModelWeeklyPlanCandidate(
        kind=WeeklyPlanCaptureKind.DATED_PLAN,
        target_week_start=TARGET_WEEK,
        plan_date=date(2026, 8, 23),
        temporal_exact_quote="周末",
        matter_exact_quote="整理材料",
    )

    ambiguous_outcome = materialize_weekly_plan_candidate(
        ambiguous, evidence=evidence, plan=_plan(), observed_at=NOW
    )
    sunday_outcome = materialize_weekly_plan_candidate(
        sunday, evidence=evidence, plan=_plan(), observed_at=NOW
    )

    assert ambiguous_outcome.status == "rejected"
    assert ambiguous_outcome.reason_code == "ambiguous_plan_date"
    assert ambiguous_outcome.command is None
    assert ambiguous_outcome.suggestion is None
    assert sunday_outcome.status == "rejected"
    assert sunday_outcome.reason_code == "plan_date_outside_monday_to_saturday"


def test_candidate_cannot_be_written_to_a_different_target_week() -> None:
    text = "下周二准备项目材料。"
    outcome = materialize_weekly_plan_candidate(
        ModelWeeklyPlanCandidate(
            kind=WeeklyPlanCaptureKind.DATED_PLAN,
            target_week_start=date(2026, 8, 24),
            plan_date=date(2026, 8, 25),
            temporal_exact_quote="下周二",
            matter_exact_quote="准备项目材料",
        ),
        evidence=_evidence(text),
        plan=_plan(),
        observed_at=NOW,
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "target_week_mismatch"
    assert outcome.command is None
    assert outcome.suggestion is None
