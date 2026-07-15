from __future__ import annotations

import pytest
from types import SimpleNamespace
from pathlib import Path
from datetime import date
from datetime import datetime, timezone

from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeFactGuard,
    OutcomeObjectRef,
    OutcomeReplyComposer,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.daily_execution import Agent2DailyExecutionResult
from app.agent2.outcome_adapters import daily_execution_outcomes
from app.agent2.outcome_adapters import business_composition_outcomes
from app.agent2.outcome_adapters import periodic_execution_outcomes
from app.agent2.outcome_adapters import notification_outcome
from app.agent2.outcome_adapters import text_outcome
from app.agent2.business.composition import BusinessActionResult, BusinessCompositionResult
from app.agent2.business.contracts import BusinessReceipt
from app.agent2.report_domain import (
    PeriodicReportSnapshot,
    TypedPeriodicReportCommand,
    execute_periodic_report_command,
)
from app.agent2.report_sql_executor import PersistedPeriodicReportExecution
from uuid import NAMESPACE_URL, uuid5


def test_failed_case_progress_outcome_cannot_be_expressed_as_a_successful_write():
    outcome = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef(
            object_type="case_progress",
            stable_id="",
            label="保定锦珑府案",
        ),
        business_status="failed",
        message_status="not_applicable",
        changed_fields=(),
        user_visible_snapshot={
            "case_name": "保定锦珑府案",
            "content": "法院表示下周重新查控",
        },
        blocking_reason="database_commit_failed",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("planned", "failed"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "保定锦珑府案" in reply
    assert "暂时没能记录" in reply
    assert all(word not in reply for word in ("已记录", "已修改", "已删除", "已提交"))


def test_missing_travel_date_reply_preserves_recognized_destination() -> None:
    outcome = OperationOutcome(
        domain="travel",
        operation="register",
        object_ref=OutcomeObjectRef("travel_event", "", "常州出差"),
        business_status="blocked",
        message_status="not_applicable",
        changed_fields=(),
        user_visible_snapshot={"destination": "常州市"},
        blocking_reason="travel_time_needs_clarification",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("planned", "blocked"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert reply == (
        "去常州市的出差我已经识别到了，还差出发日期。"
        "哪天去？本次还没有登记。"
    )


def test_blocked_travel_composition_keeps_destination_for_reply() -> None:
    composition = BusinessCompositionResult(
        source_message_id="travel-changzhou-no-date",
        actions=(
            BusinessActionResult(
                semantic_command_id="semantic-travel",
                semantic_command_type="record_travel_candidate",
                block=SimpleNamespace(
                    reason_code="travel_time_needs_clarification",
                    metadata={},
                ),
                outcome_context={"destination": "常州市"},
            ),
        ),
    )

    reply = OutcomeReplyComposer().compose(
        business_composition_outcomes(composition)
    )

    assert reply == (
        "去常州市的出差我已经识别到了，还差出发日期。"
        "哪天去？本次还没有登记。"
    )


def test_unknown_case_composition_names_the_unmatched_reference() -> None:
    composition = BusinessCompositionResult(
        source_message_id="unknown-case-message",
        actions=(
            BusinessActionResult(
                semantic_command_id="semantic-case",
                semantic_command_type="record_case_progress_candidate",
                block=SimpleNamespace(
                    reason_code="case_target_not_found",
                    metadata={},
                ),
                outcome_context={"case_name": "SSGL-2605-0024"},
            ),
        ),
    )

    reply = OutcomeReplyComposer().compose(
        business_composition_outcomes(composition)
    )

    assert reply == (
        "我没在你当前分配的案件中找到“SSGL-2605-0024”。"
        "请核对案件编号或名称后再发一次；本次没有登记。"
    )


def test_daily_change_reply_shows_the_complete_snapshot_without_internal_fields():
    outcome = OperationOutcome(
        domain="report",
        operation="update",
        object_ref=OutcomeObjectRef("daily_report", "report-internal-id", "7月13日日报", 8),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("today_work",),
        user_visible_snapshot={
            "report_type": "daily",
            "period_label": "2026-07-13",
            "today_work": ["完成合同审核"],
            "problems": ["暂无"],
            "tomorrow_plan": ["跟进案件材料"],
        },
        blocking_reason="",
        receipt_refs=(
            OutcomeReceiptRef("receipt-internal-id", "database", "executed", True),
        ),
        state_transition=OutcomeStateTransition("collecting", "collecting"),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "完成合同审核" in reply
    assert "暂无" in reply
    assert "跟进案件材料" in reply
    assert all(secret not in reply for secret in ("report-internal-id", "receipt-internal-id", "v8"))


def test_case_progress_reply_repeats_the_persisted_case_and_body_verbatim():
    body = "今天联系法院推进了该案件，法院表示下周重新查控。"
    outcome = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef("case_progress", "progress-id", "保定锦珑府案", 1),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("summary",),
        user_visible_snapshot={"case_name": "保定锦珑府案", "content": body},
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-id", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "recorded"),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "保定锦珑府案" in reply
    assert body in reply
    assert "下周重新查控并反馈" not in reply


def test_provider_acceptance_is_not_expressed_as_delivery_or_user_agreement():
    outcome = OperationOutcome(
        domain="travel",
        operation="notify",
        object_ref=OutcomeObjectRef("travel_intent", "travel-id", "上海出差", 2),
        business_status="accepted_by_provider",
        message_status="accepted_by_provider",
        changed_fields=("notification_status",),
        user_visible_snapshot={
            "destination": "上海",
            "date_label": "7月15日",
            "purpose": "开庭",
            "counterparty": "刘聪",
        },
        blocking_reason="",
        receipt_refs=(
            OutcomeReceiptRef(
                "message-receipt", "external_message", "accepted_by_provider", False,
                external_message_id="provider-message-id",
            ),
        ),
        state_transition=OutcomeStateTransition("sending", "accepted_by_provider"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert all(value in reply for value in ("上海", "7月15日", "开庭"))
    assert "平台已经受理" in reply
    assert all(claim not in reply for claim in ("对方已收到", "已经送达", "刘聪已同意"))


def test_provider_acceptance_requires_an_external_message_receipt():
    with pytest.raises(ValueError, match="external message evidence"):
        OperationOutcome(
            domain="travel",
            operation="notify",
            object_ref=OutcomeObjectRef("travel_intent", "travel-id", "上海出差"),
            business_status="accepted_by_provider",
            message_status="accepted_by_provider",
            changed_fields=(),
            user_visible_snapshot={"destination": "上海"},
            blocking_reason="",
            receipt_refs=(),
            state_transition=OutcomeStateTransition("sending", "accepted_by_provider"),
            actual_write=False,
        )


def test_travel_waiting_for_reply_cannot_claim_sent_without_external_message_evidence():
    outcome = OperationOutcome(
        domain="travel",
        operation="notify",
        object_ref=OutcomeObjectRef("travel_notification", "notification-1", "南京出差"),
        business_status="waiting_for_reply",
        message_status="waiting_for_reply",
        changed_fields=("message_status",),
        user_visible_snapshot={
            "destination": "南京",
            "date_label": "7月15日",
            "purpose": "开庭",
        },
        blocking_reason="",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("sending", "waiting_for_reply"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "通知已发出" not in reply
    assert "回复暂时无法生成" in reply


def test_followup_waiting_for_reply_cannot_treat_database_write_as_send_evidence():
    outcome = OperationOutcome(
        domain="followup",
        operation="create_task",
        object_ref=OutcomeObjectRef(
            "case_followup_task", "followup-1", "南京工程款案追问"
        ),
        business_status="succeeded",
        message_status="waiting_for_reply",
        changed_fields=("task_status", "message_status"),
        user_visible_snapshot={"case_name": "南京工程款案"},
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-1", "database", "executed", True),),
        state_transition=OutcomeStateTransition("sending", "waiting_for_reply"),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "消息已发出" not in reply
    assert "回复暂时无法生成" in reply


def test_waiting_for_reply_allows_send_claim_with_provider_message_evidence():
    outcome = OperationOutcome(
        domain="travel",
        operation="notify",
        object_ref=OutcomeObjectRef("travel_notification", "notification-1", "南京出差"),
        business_status="waiting_for_reply",
        message_status="waiting_for_reply",
        changed_fields=("message_status",),
        user_visible_snapshot={"destination": "南京"},
        blocking_reason="",
        receipt_refs=(
            OutcomeReceiptRef(
                "message-receipt",
                "transport",
                "accepted_by_provider",
                False,
                external_message_id="provider-message-1",
            ),
        ),
        state_transition=OutcomeStateTransition("sending", "waiting_for_reply"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "通知已发出" in reply
    assert "回复暂时无法生成" not in reply


def test_waiting_for_reply_rejects_external_id_from_a_queued_receipt():
    outcome = OperationOutcome(
        domain="travel",
        operation="notify",
        object_ref=OutcomeObjectRef("travel_notification", "notification-1", "南京出差"),
        business_status="waiting_for_reply",
        message_status="waiting_for_reply",
        changed_fields=("message_status",),
        user_visible_snapshot={"destination": "南京"},
        blocking_reason="",
        receipt_refs=(
            OutcomeReceiptRef(
                "message-receipt",
                "transport",
                "queued",
                False,
                external_message_id="premature-message-id",
            ),
        ),
        state_transition=OutcomeStateTransition("queued", "waiting_for_reply"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "通知已发出" not in reply
    assert "回复暂时无法生成" in reply


def test_message_send_guard_allows_an_explicit_denial_without_external_evidence():
    outcome = OperationOutcome(
        domain="followup",
        operation="notify",
        object_ref=OutcomeObjectRef("case_followup_notification", "notification-1", "案件追问"),
        business_status="waiting_for_reply",
        message_status="waiting_for_reply",
        changed_fields=(),
        user_visible_snapshot={},
        blocking_reason="",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("sending", "waiting_for_reply"),
        actual_write=False,
    )

    violations = OutcomeFactGuard().violations(
        (outcome,), "目前不能说消息已发出，因为没有外部消息证据。"
    )

    assert "message_sent_claim_without_external_evidence" not in violations


def test_fact_guard_rejects_travel_agreement_claim_without_committed_response_receipt():
    outcome = OperationOutcome(
        domain="travel",
        operation="query",
        object_ref=OutcomeObjectRef(
            "travel_collaboration", "candidate-1", "南京出差协同"
        ),
        business_status="matched",
        message_status="not_applicable",
        changed_fields=(),
        user_visible_snapshot={"destination": "南京"},
        blocking_reason="",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("matched", "matched"),
        actual_write=False,
    )

    violations = OutcomeFactGuard().violations(
        (outcome,), "双方都已明确接受这次出差协同。"
    )

    assert "agreement_claim_without_committed_response_receipt" in violations


def test_fact_guard_allows_explicit_denial_of_unproven_travel_agreement():
    outcome = OperationOutcome(
        domain="travel",
        operation="query",
        object_ref=OutcomeObjectRef(
            "travel_collaboration", "candidate-1", "南京出差协同"
        ),
        business_status="matched",
        message_status="not_applicable",
        changed_fields=(),
        user_visible_snapshot={"destination": "南京"},
        blocking_reason="",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("matched", "matched"),
        actual_write=False,
    )

    violations = OutcomeFactGuard().violations(
        (outcome,), "目前不能说双方已接受，因为没有对应的协同回复 receipt。"
    )

    assert "agreement_claim_without_committed_response_receipt" not in violations


def test_mutation_success_requires_a_committed_write_receipt():
    with pytest.raises(ValueError, match="committed write receipt"):
        OperationOutcome(
            domain="case_progress",
            operation="create",
            object_ref=OutcomeObjectRef("case_progress", "progress-id", "保定案"),
            business_status="succeeded",
            message_status="not_applicable",
            changed_fields=("summary",),
            user_visible_snapshot={"case_name": "保定案", "content": "已开庭"},
            blocking_reason="",
            receipt_refs=(OutcomeReceiptRef("receipt", "database", "failed", False),),
            state_transition=OutcomeStateTransition("absent", "recorded"),
            actual_write=False,
        )


def test_daily_executor_result_is_adapted_to_one_receipt_backed_outcome():
    result = Agent2DailyExecutionResult(
        report_id="report-id",
        report_date=date(2026, 7, 13),
        status="collecting",
        message="executor prose must be ignored",
        report_saved=True,
        read_only=False,
        today_work=["完成合同审核"],
        problems=[],
        tomorrow_plan=["跟进案件材料"],
        command_results=[{
            "receipt_id": "daily-receipt",
            "typed_command": {"command_type": "append_item"},
            "status": "executed",
            "actual_write": True,
            "changed": True,
            "audit": {"before_version": 1, "after_version": 2},
        }],
    )

    outcomes = daily_execution_outcomes(result, source_turn_id="turn-1")

    assert len(outcomes) == 1
    assert outcomes[0].operation == "create"
    assert outcomes[0].receipt_refs[0].receipt_id == "daily-receipt"
    assert outcomes[0].user_visible_snapshot["today_work"] == ["完成合同审核"]
    assert "executor prose" not in OutcomeReplyComposer().compose(outcomes)


def test_case_business_receipt_is_the_only_source_for_case_reply_facts():
    receipt = BusinessReceipt(
        receipt_id="receipt-case",
        command_id="command-case",
        command_type="create_case_progress",
        tenant_id="tenant-a",
        actor_user_id="user-a",
        source_message_id="turn-case",
        idempotency_key="idempotency-case",
        status="executed",
        resource_type="case_progress",
        resource_id="progress-id",
        before={},
        after={"case_id": "case-id", "summary": "法院表示下周重新查控。", "version": 1},
        error_code=None,
        failed_stage=None,
        actual_write=True,
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    composition = BusinessCompositionResult(
        source_message_id="turn-case",
        actions=(BusinessActionResult(
            semantic_command_id="semantic-case",
            semantic_command_type="record_case_progress_candidate",
            compiled_command_type="create_case_progress",
            receipt=receipt,
            outcome_context={"case_name": "保定锦珑府案", "content": receipt.after["summary"]},
        ),),
    )

    outcomes = business_composition_outcomes(composition)
    reply = OutcomeReplyComposer().compose(outcomes)

    assert outcomes[0].receipt_refs[0].receipt_id == "receipt-case"
    assert "保定锦珑府案" in reply
    assert receipt.after["summary"] in reply


def test_case_reply_never_falls_back_to_internal_case_id():
    internal_case_id = "ca46f25e-0859-58bd-adf7-3cf750a724bd"
    receipt = BusinessReceipt(
        receipt_id="receipt-case-without-label",
        command_id="command-case-without-label",
        command_type="update_case_progress",
        tenant_id="tenant-a",
        actor_user_id="user-a",
        source_message_id="turn-case-without-label",
        idempotency_key="idempotency-case-without-label",
        status="executed",
        resource_type="case_progress",
        resource_id="progress-id",
        before={"case_id": internal_case_id, "summary": "旧进展"},
        after={"case_id": internal_case_id, "summary": "新进展", "version": 2},
        error_code=None,
        failed_stage=None,
        actual_write=True,
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    composition = BusinessCompositionResult(
        source_message_id="turn-case-without-label",
        actions=(
            BusinessActionResult(
                semantic_command_id="semantic-case-without-label",
                semantic_command_type="update_case_progress_candidate",
                compiled_command_type="update_case_progress",
                receipt=receipt,
                outcome_context={},
            ),
        ),
    )

    reply = OutcomeReplyComposer().compose(business_composition_outcomes(composition))

    assert internal_case_id not in reply
    assert "该案件" in reply
    assert "新进展" in reply


def test_periodic_report_execution_adapts_to_the_same_report_outcome_contract():
    owner = uuid5(NAMESPACE_URL, "outcome-owner")
    report_id = uuid5(NAMESPACE_URL, "weekly-outcome-report")
    snapshot = PeriodicReportSnapshot(
        report_id=report_id,
        owner_user_id=owner,
        report_type="weekly",
        period_key="2026-W29",
        version=0,
        status="collecting",
    )
    command = TypedPeriodicReportCommand(
        command_id=uuid5(NAMESPACE_URL, "weekly-outcome-command"),
        decision_id=uuid5(NAMESPACE_URL, "weekly-outcome-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "weekly-outcome-sub"),
        command_type="append_item",
        report_type="weekly",
        period_key="2026-W29",
        report_id=report_id,
        report_version=0,
        target_item_ids=(),
        patch={"field": "accomplishments", "value": "完成案件核验"},
        idempotency_key="weekly-outcome-key",
    )
    execution = execute_periodic_report_command(
        command, snapshot=snapshot, actor_user_id=owner
    )
    persisted = PersistedPeriodicReportExecution(
        execution=execution,
        receipt_id="weekly-receipt",
        status="authorized",
        actual_write=True,
    )

    outcomes = periodic_execution_outcomes((persisted,), source_turn_id="turn-weekly")
    reply = OutcomeReplyComposer().compose(outcomes)

    assert outcomes[0].receipt_refs[0].receipt_id == "weekly-receipt"
    assert "完成案件核验" in reply
    assert "v1" not in reply


def test_queued_notification_never_claims_sent_or_delivered():
    event = SimpleNamespace(
        notification_id="notification-1",
        status="pending",
        external_message_id="",
        error_message="",
    )

    outcome = notification_outcome(
        event,
        travel_snapshot={
            "destination": "南京",
            "date_label": "7月14日",
            "purpose": "开庭",
        },
        source_turn_id="turn-travel",
    )
    reply = OutcomeReplyComposer().compose((outcome,))

    assert outcome.business_status == "queued"
    assert "排队" in reply
    assert "已送达" not in reply
    assert "对方已收到" not in reply


def test_provider_acceptance_is_not_delivery_and_requires_external_message_id():
    event = SimpleNamespace(
        notification_id="notification-2",
        status="sent",
        external_message_id="provider-message-2",
        error_message="",
    )

    outcome = notification_outcome(
        event,
        travel_snapshot={"destination": "南京", "date_label": "7月14日", "purpose": "开庭"},
        source_turn_id="turn-travel",
    )
    reply = OutcomeReplyComposer().compose((outcome,))

    assert outcome.message_status == "accepted_by_provider"
    assert "平台已经受理" in reply
    assert "对方已收到" not in reply
    assert "已同意" not in reply


def test_multi_intent_partial_success_and_failure_are_both_expressed_from_outcomes():
    success = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef("case_progress", "progress-1", "南京合同案", 1),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("summary",),
        user_visible_snapshot={"case_name": "南京合同案", "content": "法院下周重新查控。"},
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-ok", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "recorded"),
        actual_write=True,
    )
    failed = OperationOutcome(
        domain="travel",
        operation="register",
        object_ref=OutcomeObjectRef("travel_intent", "", "上海"),
        business_status="failed",
        message_status="not_requested",
        changed_fields=(),
        user_visible_snapshot={"destination": "上海", "date_label": "7月15日", "purpose": "开庭"},
        blocking_reason="database_failed",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("planned", "failed"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((success, failed))

    assert "南京合同案" in reply
    assert "法院下周重新查控。" in reply
    assert "上海" in reply
    assert "没有改动" in reply


def test_partial_success_fact_guard_is_scoped_to_each_outcome():
    success = OperationOutcome(
        domain="report",
        operation="create",
        object_ref=OutcomeObjectRef("daily_report", "report-1", "2026-07-14 日报", 2),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("today_work",),
        user_visible_snapshot={
            "report_type": "daily",
            "today_work": ["完成合同审核"],
            "problems": [],
            "tomorrow_plan": [],
        },
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-ok", "database", "executed", True),),
        state_transition=OutcomeStateTransition("collecting", "collecting"),
        actual_write=True,
    )
    failed = OperationOutcome(
        domain="travel",
        operation="register",
        object_ref=OutcomeObjectRef("travel_intent", "", "南京"),
        business_status="failed",
        message_status="not_requested",
        changed_fields=(),
        user_visible_snapshot={"destination": "南京", "date_label": "明天"},
        blocking_reason="database_failed",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("planned", "failed"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((success, failed))

    assert "完成合同审核" in reply
    assert "南京" in reply
    assert "回复暂时无法生成" not in reply


def test_read_only_chat_outcome_cannot_claim_a_business_write():
    reply = OutcomeReplyComposer().compose(
        (text_outcome("已经记录到日报。", source_turn_id="turn-chat-unsafe"),)
    )

    assert "已经记录到日报" not in reply
    assert "不会改变或补充执行事实" in reply


def test_read_only_chat_outcome_cannot_claim_saved_to_report():
    reply = OutcomeReplyComposer().compose(
        (text_outcome("已经保存到日报。", source_turn_id="turn-chat-save-unsafe"),)
    )

    assert "已经保存到日报" not in reply
    assert "不会改变或补充执行事实" in reply


def test_read_only_chat_outcome_cannot_claim_notification_sent():
    reply = OutcomeReplyComposer().compose(
        (text_outcome("通知已经发出。", source_turn_id="turn-chat-send-unsafe"),)
    )

    assert "通知已经发出" not in reply
    assert "不会改变或补充执行事实" in reply


def test_read_only_chat_outcome_cannot_claim_item_added_to_report():
    reply = OutcomeReplyComposer().compose(
        (
            text_outcome(
                "我已经帮你把这条加入日报了。",
                source_turn_id="turn-chat-add-unsafe",
            ),
        )
    )

    assert "已经帮你把这条加入日报" not in reply
    assert "不会改变或补充执行事实" in reply


def test_read_only_chat_outcome_preserves_explicit_denial_of_business_success():
    text = (
        "目前不能说已经保存到日报，也不能说通知已经发出，"
        "更不能说对方已经收到或已经同意。"
    )

    reply = OutcomeReplyComposer().compose(
        (text_outcome(text, source_turn_id="turn-chat-explicit-denial"),)
    )

    assert reply == text


def test_denial_clause_cannot_mask_a_later_positive_business_claim():
    text = "之前不能说已经保存到日报，但是现在已经保存到日报了。"

    reply = OutcomeReplyComposer().compose(
        (text_outcome(text, source_turn_id="turn-chat-contrast-unsafe"),)
    )

    assert text not in reply
    assert "不会改变或补充执行事实" in reply


@pytest.mark.parametrize(
    "text",
    (
        "对方已经收到通知。",
        "对方已经同意了。",
    ),
)
def test_read_only_text_outcome_cannot_claim_delivery_or_user_agreement(text):
    reply = OutcomeReplyComposer().compose(
        (text_outcome(text, source_turn_id="turn-chat-external-fact-unsafe"),)
    )

    assert text not in reply
    assert "不会改变或补充执行事实" in reply


def test_read_only_knowledge_outcome_cannot_claim_business_success():
    text = "通知已经发出，对方已经同意了。"

    reply = OutcomeReplyComposer().compose(
        (
            text_outcome(
                text,
                source_turn_id="turn-knowledge-external-fact-unsafe",
                domain="knowledge",
            ),
        )
    )

    assert text not in reply
    assert "不会改变或补充执行事实" in reply


def test_case_progress_reply_repeats_receipt_backed_next_actions():
    outcome = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef("case_progress", "progress-2", "南京合同案", 1),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("summary", "next_actions"),
        user_visible_snapshot={
            "case_name": "南京合同案",
            "content": "今天联系法院确认了查控进度。",
            "next_actions": ["下周一提交补充财产线索"],
        },
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-case", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "recorded"),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "南京合同案" in reply
    assert "今天联系法院确认了查控进度。" in reply
    assert "下周一提交补充财产线索" in reply


def test_all_mutation_operations_require_a_committed_write_receipt():
    for operation in ("respond", "create_task", "snooze"):
        with pytest.raises(ValueError, match="committed write receipt"):
            OperationOutcome(
                domain="followup",
                operation=operation,
                object_ref=OutcomeObjectRef("object", "object-1", "测试对象"),
                business_status="succeeded",
                message_status="not_applicable",
                changed_fields=("status",),
                user_visible_snapshot={},
                blocking_reason="",
                receipt_refs=(),
                state_transition=OutcomeStateTransition("before", "after"),
                actual_write=False,
            )


def test_duplicate_case_receipt_is_expressed_as_no_repeat_write():
    outcome = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef("case_progress", "progress-1", "南京合同案", 1),
        business_status="duplicate",
        message_status="not_applicable",
        changed_fields=(),
        user_visible_snapshot={"case_name": "南京合同案", "content": "法院下周重新查控。"},
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-duplicate", "database", "duplicate", False),),
        state_transition=OutcomeStateTransition("recorded", "recorded"),
        actual_write=False,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "没有重复写入" in reply
    assert "南京合同案" in reply
    assert "法院下周重新查控。" in reply


def test_production_entrypoints_do_not_use_executor_authored_business_reply():
    for path in (Path("app/api/webhook.py"), Path("app/stream_runner.py")):
        source = path.read_text(encoding="utf-8")
        assert "business_composition_reply_text" not in source
        assert "OutcomeReplyComposer" in source


def test_existing_party_case_query_is_rendered_from_receipt_without_internal_fields():
    receipt = BusinessReceipt(
        receipt_id="receipt-party",
        command_id="command-party",
        command_type="query_party_cases",
        tenant_id="tenant-a",
        actor_user_id="user-a",
        source_message_id="turn-party",
        idempotency_key="party-key",
        status="executed",
        resource_type="party_case_query",
        resource_id="party-id",
        before={},
        after={
            "party": {"canonical_name": "南京华东建设有限公司"},
            "case_count": 1,
            "cases": [{
                "case_number": "（2026）苏01民初101号",
                "case_name": "建设工程合同纠纷案",
                "role_type": "defendant",
                "status": "open",
            }],
        },
        error_code=None,
        failed_stage=None,
        actual_write=False,
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    composition = BusinessCompositionResult(
        source_message_id="turn-party",
        actions=(BusinessActionResult(
            semantic_command_id="semantic-party",
            semantic_command_type="query_case_risk",
            compiled_command_type="query_party_cases",
            receipt=receipt,
        ),),
    )

    reply = OutcomeReplyComposer().compose(business_composition_outcomes(composition))

    assert "南京华东建设有限公司" in reply
    assert "（2026）苏01民初101号" in reply
    assert "建设工程合同纠纷案" in reply
    assert "party-id" not in reply
    assert "receipt-party" not in reply


def test_existing_chat_text_is_wrapped_as_an_outcome_before_reply_composition():
    outcome = text_outcome("我会继续按你刚才的周报上下文处理。", source_turn_id="turn-chat")

    reply = OutcomeReplyComposer().compose((outcome,))

    assert reply == "我会继续按你刚才的周报上下文处理。"
    assert outcome.actual_write is False


def test_read_only_chat_cannot_say_it_remembered_a_case_fact():
    outcome = text_outcome(
        "收到，恒大翡翠华庭后天开庭，我记下了。需要我帮你准备材料吗？",
        source_turn_id="turn-false-case-success",
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "记下了" not in reply
    assert "没有写入" in reply
    assert outcome.actual_write is False


def test_read_only_chat_cannot_promise_to_record_a_case_fact_later():
    outcome = text_outcome(
        "收到，恒大翡翠华庭后天开庭，我会记下这个时间点。您还有其他需要提醒的事项吗？",
        source_turn_id="turn-false-future-case-write",
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "我会记下" not in reply
    assert "没有写入" in reply
    assert outcome.actual_write is False


def test_operation_outcome_serializes_the_full_production_identity_and_audit_envelope():
    created_at = datetime(2026, 7, 13, 9, tzinfo=timezone.utc)
    outcome = OperationOutcome(
        domain="followup",
        operation="create_task",
        object_ref=OutcomeObjectRef("case_followup_task", "followup-1", "南京工程款案追问"),
        business_status="succeeded",
        message_status="scheduled",
        changed_fields=("task_status",),
        user_visible_snapshot={"case_name": "南京工程款案"},
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-1", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "scheduled"),
        actual_write=True,
        source_turn_id="turn-1",
        outcome_id="outcome-1",
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        would_write=True,
        audit_refs=("audit-1",),
        idempotency_key="followup:create:1",
        created_at=created_at,
    )

    payload = outcome.as_dict()

    assert payload["outcome_id"] == "outcome-1"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["user_id"] == "user-1"
    assert payload["conversation_id"] == "conversation-1"
    assert payload["would_write"] is True
    assert payload["audit_refs"] == ["audit-1"]
    assert payload["idempotency_key"] == "followup:create:1"
    assert payload["created_at"] == created_at.isoformat()


def test_followup_reply_is_natural_and_does_not_upgrade_scheduled_to_sent():
    outcome = OperationOutcome(
        domain="followup",
        operation="create_task",
        object_ref=OutcomeObjectRef("case_followup_task", "followup-1", "南京工程款案追问"),
        business_status="succeeded",
        message_status="scheduled",
        changed_fields=("task_status",),
        user_visible_snapshot={
            "case_name": "南京工程款案",
            "due_at_label": "今天 15:00",
            "question_summary": "了解开庭准备情况",
        },
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-1", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "scheduled"),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "南京工程款案" in reply
    assert "今天 15:00" in reply
    assert "还没进入发送队列" in reply
    assert "已发送" not in reply
    assert "已送达" not in reply


def test_report_confirmation_pending_reply_states_case_is_kept_and_asks_before_write():
    outcome = OperationOutcome(
        domain="report",
        operation="confirm_projection",
        object_ref=OutcomeObjectRef("report_projection", "pending-1", "今天的日报"),
        business_status="waiting_for_reply",
        message_status="waiting_for_reply",
        changed_fields=("pending_status",),
        user_visible_snapshot={
            "report_type": "daily",
            "case_name": "南京工程款案",
            "content": "今天向法院提交了补充材料。",
        },
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-1", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "awaiting_input"),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "南京工程款案" in reply
    assert "今天向法院提交了补充材料" in reply
    assert "案件进展已经保留" in reply
    assert "是否也加入今天的日报" in reply
