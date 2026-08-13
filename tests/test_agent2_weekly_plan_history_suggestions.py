from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pytest

from app.agent2.weekly_plan_history_suggestions import (
    BoundedFollowUpAssessment,
    ConfirmedRecordState,
    FollowUpVerdict,
    HistorySuggestionRequest,
    StableTomorrowPlanItem,
    TrustedPersonalRecord,
    build_follow_up_review_scope,
    generate_history_suggestions,
)
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    TrustedSourceKind,
    build_trusted_evidence,
    create_suggestion,
    render_suggestion_prompt,
)

UTC = timezone.utc
TARGET_WEEK = date(2026, 8, 17)
AS_OF = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_item(
    *,
    version: str = "3",
    item_ref: str = "daily-plan-item:alpha",
    recorded_at: datetime = datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
) -> StableTomorrowPlanItem:
    source_text = "明日计划：继续完善示例合同审查规则，并补齐风险提示。"
    return StableTomorrowPlanItem(
        owner_user_id="user-example",
        field_name="tomorrow_plan",
        item_ref=item_ref,
        source_ref="daily-report:2026-08-11",
        source_version=version,
        record_state=ConfirmedRecordState.SUBMITTED,
        recorded_at=recorded_at,
        source_text=source_text,
        source_sha256=_sha256(source_text),
        item_exact_text="继续完善示例合同审查规则",
    )


def _later_record(
    *,
    source_ref: str = "daily-report:2026-08-12",
    recorded_at: datetime = datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
) -> TrustedPersonalRecord:
    text = "今日工作：整理示例项目材料。"
    return TrustedPersonalRecord(
        owner_user_id="user-example",
        source_kind=TrustedSourceKind.CONFIRMED_RECORD,
        source_ref=source_ref,
        source_version="1",
        recorded_at=recorded_at,
        evidence_text=text,
        evidence_sha256=_sha256(text),
    )


def test_grounded_no_later_record_creates_only_a_confirmed_record_suggestion() -> None:
    item = _source_item()
    later = _later_record()
    scope = build_follow_up_review_scope(
        owner_user_id="user-example",
        source_item=item,
        later_records=(later,),
        as_of=AS_OF,
    )
    request = HistorySuggestionRequest(
        owner_user_id="user-example",
        target_week_start=TARGET_WEEK,
        as_of=AS_OF,
        source_items=(item,),
        later_records=(later,),
        assessments=(
            BoundedFollowUpAssessment(
                review_scope=scope,
                verdict=FollowUpVerdict.NO_LATER_RECORD,
            ),
        ),
    )

    result = generate_history_suggestions(request)

    assert len(result.new_suggestions) == 1
    suggestion = result.new_suggestions[0]
    assert suggestion.source_kind is TrustedSourceKind.CONFIRMED_RECORD
    assert suggestion.owner_user_id == "user-example"
    assert suggestion.target_week_start == TARGET_WEEK
    assert suggestion.matter_excerpt == "继续完善示例合同审查规则"
    assert suggestion.evidence_text == item.source_text
    assert suggestion.evidence_sha256 == item.source_sha256
    assert not hasattr(result, "command")
    assert not hasattr(result, "plan")
    assert "我暂时没找到后续记录" in render_suggestion_prompt(suggestion)


def test_followed_up_uncertain_missing_review_or_no_later_evidence_never_suggests() -> None:
    item = _source_item()
    later = _later_record()
    scope = build_follow_up_review_scope(
        owner_user_id="user-example",
        source_item=item,
        later_records=(later,),
        as_of=AS_OF,
    )

    for verdict in (FollowUpVerdict.FOLLOWED_UP, FollowUpVerdict.UNCERTAIN):
        result = generate_history_suggestions(
            HistorySuggestionRequest(
                owner_user_id="user-example",
                target_week_start=TARGET_WEEK,
                as_of=AS_OF,
                source_items=(item,),
                later_records=(later,),
                assessments=(
                    BoundedFollowUpAssessment(
                        review_scope=scope,
                        verdict=verdict,
                    ),
                ),
            )
        )
        assert result.new_suggestions == ()

    missing_review = generate_history_suggestions(
        HistorySuggestionRequest(
            owner_user_id="user-example",
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
            source_items=(item,),
            later_records=(later,),
            assessments=(),
        )
    )
    assert missing_review.new_suggestions == ()

    no_later_evidence_scope = build_follow_up_review_scope(
        owner_user_id="user-example",
        source_item=item,
        later_records=(),
        as_of=AS_OF,
    )
    no_later_evidence = generate_history_suggestions(
        HistorySuggestionRequest(
            owner_user_id="user-example",
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
            source_items=(item,),
            later_records=(),
            assessments=(
                BoundedFollowUpAssessment(
                    review_scope=no_later_evidence_scope,
                    verdict=FollowUpVerdict.NO_LATER_RECORD,
                ),
            ),
        )
    )
    assert no_later_evidence.new_suggestions == ()


def test_source_must_be_a_stable_tomorrow_plan_item_from_the_source_week() -> None:
    invalid_field = _source_item()
    invalid_field = StableTomorrowPlanItem(
        **{
            **invalid_field.__dict__,
            "field_name": "today_work",  # type: ignore[arg-type]
        }
    )

    with pytest.raises(ValueError, match="tomorrow_plan"):
        generate_history_suggestions(
            HistorySuggestionRequest(
                owner_user_id="user-example",
                target_week_start=TARGET_WEEK,
                as_of=AS_OF,
                source_items=(invalid_field,),
                later_records=(_later_record(),),
                assessments=(),
            )
        )


def test_each_week_has_at_most_three_new_suggestions_in_stable_newest_first_order() -> None:
    items = tuple(
        _source_item(
            item_ref=f"daily-plan-item:{index}",
            recorded_at=datetime(2026, 8, 10 + index, 8, 0, tzinfo=UTC),
        )
        for index in range(1, 5)
    )
    later = _later_record(
        recorded_at=datetime(2026, 8, 14, 8, 30, tzinfo=UTC)
    )

    def _run(source_items: tuple[StableTomorrowPlanItem, ...]):
        assessments = tuple(
            BoundedFollowUpAssessment(
                review_scope=build_follow_up_review_scope(
                    owner_user_id="user-example",
                    source_item=item,
                    later_records=(later,),
                    as_of=AS_OF,
                ),
                verdict=FollowUpVerdict.NO_LATER_RECORD,
            )
            for item in source_items
        )
        return generate_history_suggestions(
            HistorySuggestionRequest(
                owner_user_id="user-example",
                target_week_start=TARGET_WEEK,
                as_of=AS_OF,
                source_items=source_items,
                later_records=(later,),
                assessments=assessments,
            )
        )

    forward = _run(items)
    reverse = _run(tuple(reversed(items)))

    assert len(forward.new_suggestions) == 3
    assert [item.suggestion_id for item in forward.new_suggestions] == [
        item.suggestion_id for item in reverse.new_suggestions
    ]
    assert [item.source_ref.rsplit(":", 1)[-1] for item in forward.new_suggestions] == [
        "4",
        "3",
        "2",
    ]


def test_changed_source_version_returns_a_new_candidate_and_superseded_old_state() -> None:
    old_item = _source_item(version="2")
    old_evidence = build_trusted_evidence(
        owner_user_id=old_item.owner_user_id,
        source_kind=TrustedSourceKind.CONFIRMED_RECORD,
        source_ref=f"{old_item.source_ref}#{old_item.item_ref}",
        source_version=old_item.source_version,
        evidence_text=old_item.source_text,
    )
    old_suggestion = create_suggestion(
        owner_user_id="user-example",
        target_week_start=TARGET_WEEK,
        evidence=old_evidence,
        matter_excerpt=old_item.item_exact_text,
        created_at=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 24, 0, 0, tzinfo=UTC),
    )
    changed_item = _source_item(version="3")
    later = _later_record()
    scope = build_follow_up_review_scope(
        owner_user_id="user-example",
        source_item=changed_item,
        later_records=(later,),
        as_of=AS_OF,
    )

    result = generate_history_suggestions(
        HistorySuggestionRequest(
            owner_user_id="user-example",
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
            source_items=(changed_item,),
            later_records=(later,),
            assessments=(
                BoundedFollowUpAssessment(
                    review_scope=scope,
                    verdict=FollowUpVerdict.NO_LATER_RECORD,
                ),
            ),
            existing_suggestions=(old_suggestion,),
        )
    )

    assert len(result.new_suggestions) == 1
    replacement = result.new_suggestions[0]
    assert replacement.source_version == "3"
    assert result.superseded_suggestions[0].suggestion_id == old_suggestion.suggestion_id
    assert result.superseded_suggestions[0].superseded_by_id == replacement.suggestion_id


def test_existing_available_suggestions_count_toward_the_weekly_limit() -> None:
    def _existing(index: int):
        text = f"明日计划：处理既有事项{index}。"
        evidence = build_trusted_evidence(
            owner_user_id="user-example",
            source_kind=TrustedSourceKind.CONFIRMED_RECORD,
            source_ref=f"daily-report:existing#{index}",
            source_version="1",
            evidence_text=text,
        )
        return create_suggestion(
            owner_user_id="user-example",
            target_week_start=TARGET_WEEK,
            evidence=evidence,
            matter_excerpt=f"处理既有事项{index}",
            created_at=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
            expires_at=datetime(2026, 8, 24, 0, 0, tzinfo=UTC),
        )

    items = tuple(
        _source_item(
            item_ref=f"daily-plan-item:new-{index}",
            recorded_at=datetime(2026, 8, 11 + index, 8, 0, tzinfo=UTC),
        )
        for index in range(2)
    )
    later = _later_record(recorded_at=datetime(2026, 8, 14, 8, 30, tzinfo=UTC))
    assessments = tuple(
        BoundedFollowUpAssessment(
            review_scope=build_follow_up_review_scope(
                owner_user_id="user-example",
                source_item=item,
                later_records=(later,),
                as_of=AS_OF,
            ),
            verdict=FollowUpVerdict.NO_LATER_RECORD,
        )
        for item in items
    )

    result = generate_history_suggestions(
        HistorySuggestionRequest(
            owner_user_id="user-example",
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
            source_items=items,
            later_records=(later,),
            assessments=assessments,
            existing_suggestions=(_existing(1), _existing(2)),
        )
    )

    assert len(result.new_suggestions) == 1


@pytest.mark.parametrize(
    "terminal_status",
    (SuggestionStatus.ACCEPTED, SuggestionStatus.REJECTED),
)
def test_explicit_owner_decision_is_never_revived_by_history_refresh(
    terminal_status,
) -> None:
    from dataclasses import replace

    item = _source_item(version="2")
    later = _later_record()
    evidence = build_trusted_evidence(
        owner_user_id=item.owner_user_id,
        source_kind=TrustedSourceKind.CONFIRMED_RECORD,
        source_ref=f"{item.source_ref}#{item.item_ref}",
        source_version=item.source_version,
        evidence_text=item.source_text,
    )
    existing = create_suggestion(
        owner_user_id=item.owner_user_id,
        target_week_start=TARGET_WEEK,
        evidence=evidence,
        matter_excerpt=item.item_exact_text,
        created_at=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 24, 0, 0, tzinfo=UTC),
    )
    existing = replace(
        existing,
        status=terminal_status,
        decision_ref="owner-message-1",
        decided_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        accepted_item_id=(
            "10000000-0000-4000-8000-000000000001"
            if terminal_status is SuggestionStatus.ACCEPTED
            else None
        ),
    )
    scope = build_follow_up_review_scope(
        owner_user_id=item.owner_user_id,
        source_item=item,
        later_records=(later,),
        as_of=AS_OF,
    )

    result = generate_history_suggestions(
        HistorySuggestionRequest(
            owner_user_id=item.owner_user_id,
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
            source_items=(item,),
            later_records=(later,),
            assessments=(
                BoundedFollowUpAssessment(
                    review_scope=scope,
                    verdict=FollowUpVerdict.NO_LATER_RECORD,
                ),
            ),
            existing_suggestions=(existing,),
        )
    )

    assert result.new_suggestions == ()
    assert result.superseded_suggestions == ()
