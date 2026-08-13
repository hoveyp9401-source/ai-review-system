from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pytest

from app.agent2.weekly_plan_history_pipeline import (
    HistorySuggestionRefreshRequest,
    InMemoryHistorySuggestionGateway,
    LLMHistoryFollowUpReviewer,
    TrustedHistoryWindow,
    WeeklyPlanHistorySuggestionService,
)
from app.agent2.weekly_plan_history_suggestions import (
    BoundedFollowUpAssessment,
    ConfirmedRecordState,
    FollowUpVerdict,
    StableTomorrowPlanItem,
    TrustedPersonalRecord,
)
from app.agent2.weekly_plan_suggestions import TrustedSourceKind

UTC = timezone.utc
TARGET_WEEK = date(2026, 8, 17)
AS_OF = datetime(2026, 8, 14, 9, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trusted_window() -> TrustedHistoryWindow:
    source_text = '{"tomorrow_plan":["完善合同审查规则"]}'
    later_text = '{"today_work":["整理项目资料"]}'
    return TrustedHistoryWindow(
        source_items=(
            StableTomorrowPlanItem(
                owner_user_id="user-a",
                field_name="tomorrow_plan",
                item_ref="daily-item-a",
                source_ref="daily-report:report-a",
                source_version="v1",
                record_state=ConfirmedRecordState.SUBMITTED,
                recorded_at=datetime(2026, 8, 11, tzinfo=UTC),
                source_text=source_text,
                source_sha256=_sha256(source_text),
                item_exact_text="完善合同审查规则",
            ),
        ),
        later_records=(
            TrustedPersonalRecord(
                owner_user_id="user-a",
                source_kind=TrustedSourceKind.CONFIRMED_RECORD,
                source_ref="daily-report:report-b",
                source_version="v1",
                recorded_at=datetime(2026, 8, 12, tzinfo=UTC),
                evidence_text=later_text,
                evidence_sha256=_sha256(later_text),
            ),
        ),
    )


class _ExplicitNoLaterReviewer:
    async def assess(self, review_inputs):
        return tuple(
            BoundedFollowUpAssessment(
                review_scope=item.review_scope,
                verdict=FollowUpVerdict.NO_LATER_RECORD,
            )
            for item in review_inputs
        )


class _ScriptedReviewer:
    def __init__(self, build):
        self._build = build

    async def assess(self, review_inputs):
        return self._build(review_inputs)


@pytest.mark.asyncio
async def test_refresh_persists_only_an_optional_confirmed_record_suggestion() -> None:
    gateway = InMemoryHistorySuggestionGateway(
        history=_trusted_window(),
        plan_exists=True,
    )
    service = WeeklyPlanHistorySuggestionService(
        history_source=gateway,
        suggestion_store=gateway,
        reviewer=_ExplicitNoLaterReviewer(),
    )

    result = await service.refresh(
        HistorySuggestionRefreshRequest(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
        )
    )

    assert len(result.persisted_suggestions) == 1
    suggestion = result.persisted_suggestions[0]
    assert suggestion.source_kind is TrustedSourceKind.CONFIRMED_RECORD
    assert suggestion.matter_excerpt == "完善合同审查规则"
    assert gateway.available_suggestions == (suggestion,)
    assert not hasattr(result, "plan")
    assert not hasattr(result, "command")


@pytest.mark.asyncio
async def test_missing_unknown_or_duplicate_model_answers_fail_closed() -> None:
    for build in (
        lambda inputs: (),
        lambda inputs: (
            BoundedFollowUpAssessment(
                review_scope="invented-scope",
                verdict=FollowUpVerdict.NO_LATER_RECORD,
            ),
        ),
        lambda inputs: (
            BoundedFollowUpAssessment(
                review_scope=inputs[0].review_scope,
                verdict=FollowUpVerdict.NO_LATER_RECORD,
            ),
            BoundedFollowUpAssessment(
                review_scope=inputs[0].review_scope,
                verdict=FollowUpVerdict.FOLLOWED_UP,
            ),
        ),
    ):
        gateway = InMemoryHistorySuggestionGateway(
            history=_trusted_window(), plan_exists=True
        )
        result = await WeeklyPlanHistorySuggestionService(
            history_source=gateway,
            suggestion_store=gateway,
            reviewer=_ScriptedReviewer(build),
        ).refresh(
            HistorySuggestionRefreshRequest(
                tenant_id="tenant-a",
                owner_user_id="user-a",
                target_week_start=TARGET_WEEK,
                as_of=AS_OF,
            )
        )

        assert result.persisted_suggestions == ()
        assert gateway.available_suggestions == ()


class _ReviewClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.mark.asyncio
async def test_llm_reviewer_is_bounded_to_exact_scopes_and_returns_no_commands() -> None:
    source = _trusted_window().source_items[0]
    later = _trusted_window().later_records
    from app.agent2.weekly_plan_history_pipeline import HistoryFollowUpReviewInput
    from app.agent2.weekly_plan_history_suggestions import build_follow_up_review_scope

    scope = build_follow_up_review_scope(
        owner_user_id="user-a",
        source_item=source,
        later_records=later,
        as_of=AS_OF,
    )
    client = _ReviewClient(
        '{"assessments":[{"review_scope":"'
        + scope
        + '","verdict":"no_later_record"}]}'
    )

    results = await LLMHistoryFollowUpReviewer(client).assess(
        (
            HistoryFollowUpReviewInput(
                review_scope=scope,
                source_item=source,
                later_records=later,
            ),
        )
    )

    assert results == (
        BoundedFollowUpAssessment(
            review_scope=scope,
            verdict=FollowUpVerdict.NO_LATER_RECORD,
        ),
    )
    prompt = client.calls[0]["system_prompt"]
    assert "不得生成计划项" in prompt
    assert "uncertain" in prompt
    assert "完善合同审查规则" in client.calls[0]["user_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        "not-json",
        '{"assessments":[{"review_scope":"x","verdict":"completed"}]}',
        '{"assessments":[],"extra":true}',
    ),
)
async def test_llm_reviewer_malformed_or_out_of_contract_output_fails_closed(response) -> None:
    source = _trusted_window().source_items[0]
    later = _trusted_window().later_records
    from app.agent2.weekly_plan_history_pipeline import HistoryFollowUpReviewInput
    from app.agent2.weekly_plan_history_suggestions import build_follow_up_review_scope

    scope = build_follow_up_review_scope(
        owner_user_id="user-a",
        source_item=source,
        later_records=later,
        as_of=AS_OF,
    )
    review_input = HistoryFollowUpReviewInput(
        review_scope=scope,
        source_item=source,
        later_records=later,
    )
    assert await LLMHistoryFollowUpReviewer(_ReviewClient(response)).assess(
        (review_input,)
    ) == ()


@pytest.mark.asyncio
async def test_llm_reviewer_unknown_scope_is_discarded_by_the_service() -> None:
    gateway = InMemoryHistorySuggestionGateway(
        history=_trusted_window(),
        plan_exists=True,
    )
    client = _ReviewClient(
        '{"assessments":[{"review_scope":"invented-scope",'
        '"verdict":"no_later_record"}]}'
    )

    result = await WeeklyPlanHistorySuggestionService(
        history_source=gateway,
        suggestion_store=gateway,
        reviewer=LLMHistoryFollowUpReviewer(client),
    ).refresh(
        HistorySuggestionRefreshRequest(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            target_week_start=TARGET_WEEK,
            as_of=AS_OF,
        )
    )

    assert result.persisted_suggestions == ()
    assert gateway.available_suggestions == ()
