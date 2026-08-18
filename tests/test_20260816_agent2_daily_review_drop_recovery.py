"""Regression for an independent review dropping a complete Daily write.

The test enters through Agent2's public canary ingress.  Only the model network
and database seams are in memory; no production handler or message sender is
reachable.
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

import test_20260816_original_content_shapes_regression as shape_harness
import test_agent2_tri_domain_ingress_atomicity as ingress_harness


def _clarification(reply: str) -> dict:
    return {
        "role": "assistant",
        "content": json.dumps(
            {"decision": "clarification", "reply": reply},
            ensure_ascii=False,
        ),
    }


async def _run_public_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_text: str | None = None,
    draft_calls: tuple[dict, ...] | None = None,
    first_review: dict,
    adjudication: dict,
    tail: tuple[dict, ...] | None = None,
):
    case = shape_harness.COMPLETE_FIVE_NONE_FOUR
    if draft_calls is None:
        draft_calls = (
            shape_harness._daily_call(case, call_id="main-complete-daily"),
        )
    if tail is None:
        tail = (ingress_harness._write_terminal(),)
    scripted = [
        ingress_harness._assistant_tools(*draft_calls),
        first_review,
        adjudication,
        *tail,
    ]
    original_client = ingress_harness._ScriptedHttpClient
    monkeypatch.setattr(
        ingress_harness,
        "_ScriptedHttpClient",
        lambda _ignored: original_client(scripted),
    )
    monkeypatch.setattr(
        ingress_harness,
        "_ObservableDailyExecutor",
        shape_harness._StructuredDailyExecutor,
    )
    return await ingress_harness._run_ingress(
        monkeypatch,
        user_text=user_text or case.user_text,
        first_calls=draft_calls,
        reviewed_calls=draft_calls,
    )


@pytest.mark.asyncio
async def test_complete_daily_write_survives_one_review_drop_only_after_fresh_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = shape_harness.COMPLETE_FIVE_NONE_FOUR
    adjudicated = shape_harness._daily_call(
        case,
        call_id="fresh-independent-adjudication",
    )

    outcome, session, http = await _run_public_recovery(
        monkeypatch,
        first_review=_clarification("请确认这些内容是否都要写入今天的日报？"),
        adjudication=ingress_harness._assistant_tools(adjudicated),
    )

    assert outcome.actual_write is True
    assert outcome.tool_success_count == 1
    assert session.attempted_calls == ["add_daily_items"]
    assert session.committed["daily_shape_batches"] == [
        shape_harness._expected_batch(case)
    ]
    assert session.outer_commit_count == 1
    assert session.outer_rollback_count == 0
    assert len(http.calls) == 4
    assert [
        item["function"]["name"] for item in http.calls[2]["tools"]
    ] == ["add_daily_items"]
    adjudication_payload = json.loads(
        http.calls[2]["messages"][1]["content"]
    )
    assert set(adjudication_payload) == {
        "ordered_current_user_messages",
        "trusted_context",
    }
    assert adjudication_payload["ordered_current_user_messages"] == [
        {"sequence": 1, "content": case.user_text}
    ]


@pytest.mark.asyncio
async def test_daily_write_keep_original_review_enters_fresh_adjudication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = shape_harness.COMPLETE_FIVE_NONE_FOUR
    adjudicated = shape_harness._daily_call(
        case,
        call_id="fresh-keep-original-adjudication",
    )

    outcome, session, http = await _run_public_recovery(
        monkeypatch,
        first_review={
            "role": "assistant",
            "content": json.dumps({"decision": "keep_original"}),
        },
        adjudication=ingress_harness._assistant_tools(adjudicated),
    )

    assert outcome.actual_write is True
    assert session.attempted_calls == ["add_daily_items"]
    assert session.outer_commit_count == 1
    assert len(http.calls) == 4


@pytest.mark.asyncio
async def test_fresh_adjudication_clarification_keeps_the_whole_batch_unwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_reply = "请确认这些内容是否都要写入今天的日报？"
    final_reply = "这些内容是要记入今天的日报，还是只需要我帮你整理文字？"

    outcome, session, http = await _run_public_recovery(
        monkeypatch,
        first_review=_clarification(first_reply),
        adjudication=_clarification(final_reply),
        tail=(ingress_harness._zero_tool_keep(final_reply),),
    )

    assert outcome.actual_write is False
    assert outcome.message == final_reply
    assert session.attempted_calls == []
    assert session.committed["daily_reports"] == []
    assert session.committed["receipts"] == []
    assert session.outer_rollback_count == 0
    assert len(http.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "difference",
    (
        "missing_item",
        "changed_field",
        "changed_acknowledgement",
        "changed_quote",
        "changed_source_index",
        "changed_submit_intent",
        "changed_target_and_version",
    ),
)
async def test_changed_or_incomplete_fresh_daily_decision_cannot_restore_the_draft(
    monkeypatch: pytest.MonkeyPatch,
    difference: str,
) -> None:
    case = shape_harness.COMPLETE_FIVE_NONE_FOUR
    first_reply = "请确认这些内容是否都要写入今天的日报？"
    changed = shape_harness._daily_call(case, call_id="changed-adjudication")
    changed_arguments = json.loads(changed["function"]["arguments"])
    if difference == "missing_item":
        changed_arguments["items"] = changed_arguments["items"][:-1]
    elif difference == "changed_field":
        changed_arguments["items"][0]["field"] = "tomorrow_plan"
    elif difference == "changed_acknowledgement":
        changed_arguments["acknowledged_empty_fields"] = []
        changed_arguments["empty_field_evidence"] = []
    elif difference == "changed_quote":
        changed_arguments["items"][0]["source_evidence"]["exact_quote"] = (
            changed_arguments["items"][1]["source_evidence"]["exact_quote"]
        )
    elif difference == "changed_source_index":
        changed_arguments["items"][0]["source_evidence"][
            "source_message_index"
        ] = 2
    elif difference == "changed_submit_intent":
        changed_arguments["submit_after_write"] = True
    elif difference == "changed_target_and_version":
        changed_arguments.update(
            {
                "date_selection": "trusted_report",
                "report_id": "20000000-0000-4000-8000-000000000099",
                "expected_version": 9,
            }
        )
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(difference)
    changed["function"]["arguments"] = json.dumps(
        changed_arguments,
        ensure_ascii=False,
    )

    outcome, session, _ = await _run_public_recovery(
        monkeypatch,
        first_review=_clarification(first_reply),
        adjudication=ingress_harness._assistant_tools(changed),
        tail=(ingress_harness._zero_tool_keep(first_reply),),
    )

    assert outcome.actual_write is False
    assert outcome.message == first_reply
    assert session.attempted_calls == []
    assert session.committed["daily_reports"] == []
    assert session.committed["receipts"] == []


@pytest.mark.asyncio
async def test_mixed_review_restores_only_daily_and_keeps_reviewed_weekly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = shape_harness.COMPLETE_FIVE_NONE_FOUR
    original_daily = shape_harness._daily_call(case, call_id="original-daily")
    original_weekly = ingress_harness._weekly_plan_call("original-weekly")
    reviewed_weekly = deepcopy(original_weekly)
    reviewed_weekly["id"] = "reviewed-weekly"
    adjudicated_daily = shape_harness._daily_call(
        case,
        call_id="adjudicated-daily",
    )

    outcome, session, _ = await _run_public_recovery(
        monkeypatch,
        user_text=(
            case.user_text
            + "下周一整理案件材料。"
        ),
        draft_calls=(original_daily, original_weekly),
        first_review=ingress_harness._assistant_tools(reviewed_weekly),
        adjudication=ingress_harness._assistant_tools(adjudicated_daily),
    )

    assert outcome.actual_write is True
    assert session.attempted_calls == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert [item["tool_call_id"] for item in session.committed["receipts"]] == [
        "adjudicated-daily",
        "reviewed-weekly",
    ]
    assert session.committed["daily_shape_batches"] == [
        shape_harness._expected_batch(case)
    ]
    assert len(session.committed["weekly_plans"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_path",
    (
        "ordinary_reordered_review",
        "dropped_review_skips_multi_add_adjudication",
    ),
)
async def test_two_different_daily_targets_fail_closed_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    review_path: str,
) -> None:
    case = shape_harness.COMPLETE_FIVE_NONE_FOUR
    user_text = "昨天补录如下；" + case.user_text
    first_group = shape_harness.ShapeCase(
        case_id="today-group",
        user_text=user_text,
        expected_items=case.expected_items[:5],
    )
    second_group = shape_harness.ShapeCase(
        case_id="yesterday-group",
        user_text=user_text,
        expected_items=case.expected_items[5:],
        expected_empty_fields=case.expected_empty_fields,
    )
    today_call = shape_harness._daily_call(
        first_group,
        call_id="original-today",
    )
    yesterday_call = shape_harness._daily_call(
        second_group,
        call_id="original-yesterday",
    )
    yesterday_arguments = json.loads(yesterday_call["function"]["arguments"])
    yesterday_arguments.update(
        {
            "date_selection": "user_explicit",
            "date_expression": "昨天",
            "proposed_date": "2026-08-13",
            "date_evidence": {
                "source_message_index": 1,
                "exact_quote": "昨天",
            },
        }
    )
    yesterday_call["function"]["arguments"] = json.dumps(
        yesterday_arguments,
        ensure_ascii=False,
    )
    reviewed_today = deepcopy(today_call)
    reviewed_today["id"] = "reviewed-today"
    reviewed_yesterday = deepcopy(yesterday_call)
    reviewed_yesterday["id"] = "reviewed-yesterday"
    clarification = "请分别确认昨天和今天要补充的日报内容。"
    scripted = [ingress_harness._assistant_tools(today_call, yesterday_call)]
    if review_path == "ordinary_reordered_review":
        scripted.append(
            ingress_harness._assistant_tools(
                reviewed_yesterday,
                reviewed_today,
            )
        )
    else:
        scripted.extend(
            (
                _clarification(clarification),
                ingress_harness._zero_tool_keep(clarification),
            )
        )
    original_client = ingress_harness._ScriptedHttpClient
    monkeypatch.setattr(
        ingress_harness,
        "_ScriptedHttpClient",
        lambda _ignored: original_client(scripted),
    )
    monkeypatch.setattr(
        ingress_harness,
        "_ObservableDailyExecutor",
        shape_harness._StructuredDailyExecutor,
    )

    outcome, session, http = await ingress_harness._run_ingress(
        monkeypatch,
        user_text=user_text,
        first_calls=(today_call, yesterday_call),
        reviewed_calls=(reviewed_yesterday, reviewed_today),
    )

    assert outcome.actual_write is False
    if review_path == "ordinary_reordered_review":
        assert outcome.owner == "blocked"
        assert outcome.reason == "tool_call_canary_execution_failed"
    else:
        assert outcome.message == clarification
        assert len(http.calls) == 3
        assert not http.calls[2].get("tools")
        assert "Daily Report adjudicator" not in str(
            http.calls[2]["messages"][0].get("content") or ""
        )
    assert session.attempted_calls == []
    assert session.committed["daily_reports"] == []
    assert session.committed["receipts"] == []
    assert len(http.calls) == len(scripted)
