from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.admission_hashes import admission_claim_hashes_match
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
    DailySnapshotReference,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
TENANT_ID = "sandbox-agent2-phase2-20260711"
ACTOR_ID = str(uuid5(NAMESPACE_URL, "report-domain-admission:actor"))
CURRENT_REPORT_ID = str(uuid5(NAMESPACE_URL, "report-domain-admission:daily:current"))
PREVIOUS_REPORT_ID = str(uuid5(NAMESPACE_URL, "report-domain-admission:daily:previous"))
PERIODIC_REPORT_ID = str(uuid5(NAMESPACE_URL, "report-domain-admission:weekly"))


class _StaticInterpreter:
    def __init__(self, interpretation: SemanticInterpretation) -> None:
        self._interpretation = interpretation

    async def interpret(self, turn, state):
        return self._interpretation


def _daily_snapshot(
    *,
    report_id: str = CURRENT_REPORT_ID,
    report_date: str = "2026-07-14",
    version: int = 4,
    status: str = "collecting",
    previous: bool = False,
) -> dict[str, Any]:
    if previous:
        items = [
            {"item_id": "previous-work-1", "field": "today_work", "text": "完成旧案复盘"},
            {"item_id": "previous-risk-1", "field": "problems", "text": "等待法院反馈"},
            {"item_id": "previous-plan-1", "field": "tomorrow_plan", "text": "提交执行申请"},
        ]
    else:
        items = [
            {"item_id": "today-work-1", "field": "today_work", "text": "今天完成合同审核"},
            {"item_id": "today-work-2", "field": "today_work", "text": "整理材料"},
            {"item_id": "today-risk-1", "field": "problems", "text": "暂无"},
            {"item_id": "today-plan-1", "field": "tomorrow_plan", "text": "联系法院"},
        ]
    return {
        "report_id": report_id,
        "report_date": report_date,
        "version": version,
        "status": status,
        "items": items,
    }


def _resources(
    *,
    current_status: str = "collecting",
    periodic_status: str = "collecting",
    current_version: int = 4,
) -> dict[str, Any]:
    current = _daily_snapshot(version=current_version, status=current_status)
    previous = _daily_snapshot(
        report_id=PREVIOUS_REPORT_ID,
        report_date="2026-07-13",
        version=2,
        previous=True,
    )
    return {
        "daily_draft": {key: value for key, value in current.items() if key != "report_date"},
        "daily_reports": [current, previous],
        "daily_policy": {
            "current_report_date": "2026-07-14",
            "historical_mutation_allowed": False,
        },
        "active_tasks": [
            {
                "workflow": "daily_report",
                "task_id": CURRENT_REPORT_ID,
                "status": current_status,
                "metadata": {"report_date": "2026-07-14"},
            }
        ],
        "periodic_report": {
            "report_id": PERIODIC_REPORT_ID,
            "owner_user_id": ACTOR_ID,
            "report_type": "weekly",
            "period_key": "2026-W29",
            "version": 3,
            "status": periodic_status,
            "sections": {
                "accomplishments": ["完成初稿"],
                "risks": ["等待反馈"],
                "next_plan": ["提交终稿"],
                "metrics": [],
            },
            "item_ids": {
                "accomplishments": ["weekly-item-1"],
                "risks": ["weekly-risk-1"],
                "next_plan": ["weekly-plan-1"],
                "metrics": [],
            },
        },
        "timezone": "Asia/Shanghai",
        "verified_confirmation_ids": ["confirmed-clear-current"],
    }


def _admit(
    *,
    action_type: str,
    text: str,
    entity_type: str | None = None,
    entity_value: str = "",
    attributes: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
):
    entity_ids = ["report-entity"] if entity_type is not None else []
    payload = {
        "intents": ["report"],
        "segments": [
            {
                "segment_id": "report-segment",
                "text": text,
                "intents": ["report"],
                "entity_ids": entity_ids,
                "action_ids": ["report-action"],
            }
        ],
        "entities": (
            [
                {
                    "entity_id": "report-entity",
                    "entity_type": entity_type,
                    "value": entity_value,
                    "confidence": 1.0,
                    "attributes": attributes or {},
                }
            ]
            if entity_type is not None
            else []
        ),
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "report-action",
                "action_type": action_type,
                "intent": "report",
                "entity_ids": entity_ids,
                "parameters": parameters or {},
            }
        ],
        "clarification_need": None,
        "context_update": {},
    }
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="report-domain-admission",
        message_id=f"message-{action_type}",
        text=text,
        occurred_at=NOW,
        resources=resources or _resources(),
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )
    return DomainAdmissionEngine().admit(
        turn,
        state,
        SemanticInterpretation.from_payload(payload),
    )


@pytest.mark.parametrize(
    (
        "action_type",
        "text",
        "entity_type",
        "entity_value",
        "attributes",
        "parameters",
        "command_type",
        "targets",
        "patch",
        "changed_fields",
    ),
    [
        (
            "submit_daily_report",
            "提交今天的日报",
            None,
            "",
            {},
            {},
            "submit_report",
            [],
            {},
            ["status"],
        ),
        (
            "edit_daily_item",
            "把第一条改成完成合同终稿审核",
            "daily_item_target",
            "第一条",
            {"target_item_ids": ["today-work-1"], "replacement": "完成合同终稿审核"},
            {},
            "edit_item",
            ["today-work-1"],
            {"replacement": "完成合同终稿审核"},
            ["items"],
        ),
        (
            "delete_daily_item",
            "删除今天工作第二条",
            "daily_item_target",
            "今天工作第二条",
            {"target_item_ids": ["today-work-2"]},
            {},
            "delete_item",
            ["today-work-2"],
            {},
            ["items"],
        ),
        (
            "merge_daily_items",
            "合并今天工作第一条和第二条",
            "daily_item_target",
            "今天工作第一条和第二条",
            {"target_item_ids": ["today-work-1", "today-work-2"]},
            {},
            "merge_items",
            ["today-work-1", "today-work-2"],
            {},
            ["items"],
        ),
        (
            "clear_daily_section",
            "清空今天日报的问题与风险",
            "daily_report",
            "今天日报的问题与风险",
            {
                "report_id": CURRENT_REPORT_ID,
                "version": 4,
                "report_date": "2026-07-14",
                "field": "problems",
            },
            {},
            "clear_report",
            [],
            {"field": "problems"},
            ["section", "items"],
        ),
        (
            "clear_daily_report",
            "确认清空今天日报",
            "daily_report",
            "今天日报",
            {"report_id": CURRENT_REPORT_ID, "version": 4, "report_date": "2026-07-14"},
            {"confirmed_pending_id": "confirmed-clear-current"},
            "clear_report",
            [],
            {"field": "all"},
            ["sections", "items"],
        ),
        (
            "copy_previous_daily_report",
            "把昨天日报复制到今天",
            "daily_report",
            "昨天日报",
            {"report_id": PREVIOUS_REPORT_ID, "version": 2, "report_date": "2026-07-13"},
            {},
            "copy_report",
            [],
            {
                "sections": {
                    "today_work": ["完成旧案复盘"],
                    "problems": ["等待法院反馈"],
                    "tomorrow_plan": ["提交执行申请"],
                },
                "source_report_date": "2026-07-13",
                "source_report_id": PREVIOUS_REPORT_ID,
            },
            ["sections", "items"],
        ),
        (
            "copy_current_work_to_tomorrow",
            "把今天工作转成明日计划",
            "daily_report",
            "今天日报",
            {"report_id": CURRENT_REPORT_ID, "version": 4, "report_date": "2026-07-14"},
            {},
            "copy_report",
            [],
            {
                "sections": {
                    "tomorrow_plan": ["继续推进合同审核", "继续整理材料"],
                },
                "source_report_date": "2026-07-14",
                "source_report_id": CURRENT_REPORT_ID,
            },
            ["section", "items"],
        ),
        (
            "complete_previous_daily_plan",
            "把昨天计划转成今天工作",
            "daily_report",
            "昨天日报",
            {"report_id": PREVIOUS_REPORT_ID, "version": 2, "report_date": "2026-07-13"},
            {},
            "copy_report",
            [],
            {
                "sections": {"today_work": ["提交执行申请"]},
                "source_report_date": "2026-07-13",
                "source_report_id": PREVIOUS_REPORT_ID,
            },
            ["section", "items"],
        ),
    ],
)
def test_daily_mutation_ticket_binds_exact_trusted_snapshot_and_command(
    action_type: str,
    text: str,
    entity_type: str | None,
    entity_value: str,
    attributes: dict[str, Any],
    parameters: dict[str, Any],
    command_type: str,
    targets: list[str],
    patch: dict[str, Any],
    changed_fields: list[str],
) -> None:
    result = _admit(
        action_type=action_type,
        text=text,
        entity_type=entity_type,
        entity_value=entity_value,
        attributes=attributes,
        parameters=parameters,
    )

    assert result.decisions[0].status == "admitted"
    assert len(result.tickets) == 1
    ticket = result.tickets[0].as_dict()
    assert ticket["operation"] == action_type
    assert ticket["object_ref"] == {
        "object_type": "daily_report",
        "stable_id": CURRENT_REPORT_ID,
        "version": 4,
    }
    assert ticket["authority_scope"] == {
        "report_type": "daily",
        "report_id": CURRENT_REPORT_ID,
        "report_version": 4,
        "command_type": command_type,
        "target_item_ids": targets,
        "patch": patch,
    }
    assert ticket["allowed_changed_fields"] == changed_fields
    assert admission_claim_hashes_match(ticket) is True


def test_reopen_daily_ticket_targets_current_completed_snapshot() -> None:
    result = _admit(
        action_type="reopen_daily_report",
        text="重新打开今天的日报",
        entity_type="daily_report",
        entity_value="今天日报",
        attributes={
            "report_id": CURRENT_REPORT_ID,
            "version": 4,
            "report_date": "2026-07-14",
        },
        resources=_resources(current_status="completed"),
    )

    assert result.decisions[0].status == "admitted"
    assert result.tickets[0].as_dict()["authority_scope"] == {
        "report_type": "daily",
        "report_id": CURRENT_REPORT_ID,
        "report_version": 4,
        "command_type": "reopen_report",
        "target_item_ids": [],
        "patch": {"report_date": "2026-07-14"},
    }
    assert result.tickets[0].allowed_changed_fields == ("status",)


@pytest.mark.parametrize(
    ("action_type", "text", "entity_type", "entity_value", "attributes", "command_type", "targets", "patch", "changed_fields"),
    [
        (
            "capture_report_event",
            "周报记一条：完成合同审核",
            "report_event",
            "完成合同审核",
            {"report_type": "weekly", "field": "accomplishments"},
            "append_item",
            [],
            {"field": "accomplishments", "value": "完成合同审核"},
            ["section", "items"],
        ),
        (
            "submit_periodic_report",
            "提交本周周报",
            "periodic_report",
            "本周周报",
            {
                "report_type": "weekly",
                "report_id": PERIODIC_REPORT_ID,
                "version": 3,
                "period_key": "2026-W29",
            },
            "submit_report",
            [],
            {},
            ["status"],
        ),
        (
            "edit_periodic_report_item",
            "把周报第一条改成完成终稿",
            "report_item_target",
            "周报第一条",
            {
                "report_type": "weekly",
                "target_item_ids": ["weekly-item-1"],
                "replacement": "完成终稿",
            },
            "edit_item",
            ["weekly-item-1"],
            {"replacement": "完成终稿"},
            ["items"],
        ),
        (
            "delete_periodic_report_item",
            "删除周报风险第一条",
            "report_item_target",
            "周报风险第一条",
            {"report_type": "weekly", "target_item_ids": ["weekly-risk-1"]},
            "delete_item",
            ["weekly-risk-1"],
            {},
            ["items"],
        ),
    ],
)
def test_periodic_mutation_ticket_binds_exact_trusted_snapshot_and_command(
    action_type: str,
    text: str,
    entity_type: str,
    entity_value: str,
    attributes: dict[str, Any],
    command_type: str,
    targets: list[str],
    patch: dict[str, Any],
    changed_fields: list[str],
) -> None:
    result = _admit(
        action_type=action_type,
        text=text,
        entity_type=entity_type,
        entity_value=entity_value,
        attributes=attributes,
    )

    assert result.decisions[0].status == "admitted"
    assert len(result.tickets) == 1
    ticket = result.tickets[0].as_dict()
    assert ticket["object_ref"] == {
        "object_type": "periodic_report",
        "stable_id": PERIODIC_REPORT_ID,
        "version": 3,
    }
    assert ticket["authority_scope"] == {
        "report_type": "weekly",
        "period_key": "2026-W29",
        "report_id": PERIODIC_REPORT_ID,
        "report_version": 3,
        "command_type": command_type,
        "target_item_ids": targets,
        "patch": patch,
    }
    assert ticket["allowed_changed_fields"] == changed_fields
    assert admission_claim_hashes_match(ticket) is True


@pytest.mark.parametrize(
    ("action_type", "text", "entity_type", "entity_value", "attributes"),
    [
        (
            "query_daily_report",
            "查看昨天日报",
            "daily_report",
            "昨天日报",
            {"report_id": PREVIOUS_REPORT_ID, "version": 2, "report_date": "2026-07-13"},
        ),
        (
            "query_periodic_report",
            "查看本周周报",
            "periodic_report",
            "本周周报",
            {
                "report_type": "weekly",
                "report_id": PERIODIC_REPORT_ID,
                "version": 3,
                "period_key": "2026-W29",
            },
        ),
    ],
)
def test_report_query_is_admitted_without_a_write_ticket(
    action_type: str,
    text: str,
    entity_type: str,
    entity_value: str,
    attributes: dict[str, Any],
) -> None:
    result = _admit(
        action_type=action_type,
        text=text,
        entity_type=entity_type,
        entity_value=entity_value,
        attributes=attributes,
    )

    assert result.decisions[0].status == "admitted"
    assert not result.decisions[0].ticket_id
    assert [action.action_type for action in result.interpretation.required_actions] == [
        action_type
    ]
    assert result.tickets == ()


@pytest.mark.parametrize(
    ("action_type", "text", "entity_type", "entity_value", "attributes", "resources", "reason_code"),
    [
        (
            "reopen_daily_report",
            "重新打开昨天日报",
            "daily_report",
            "昨天日报",
            {"report_id": PREVIOUS_REPORT_ID, "version": 2, "report_date": "2026-07-13"},
            _resources(),
            "historical_daily_mutation_blocked",
        ),
        (
            "edit_daily_item",
            "把第一条改成完成终稿",
            "daily_item_target",
            "第一条",
            {"target_item_ids": ["unknown-item"], "replacement": "完成终稿"},
            _resources(),
            "daily_item_target_not_uniquely_authorized",
        ),
        (
            "submit_daily_report",
            "提交今天日报",
            None,
            "",
            {},
            _resources(current_status="completed"),
            "daily_report_not_writable",
        ),
        (
            "capture_report_event",
            "周报记一条：完成合同审核",
            "report_event",
            "完成合同审核",
            {"report_type": "weekly", "field": "accomplishments"},
            _resources(periodic_status="completed"),
            "periodic_report_not_writable",
        ),
    ],
)
def test_untrusted_or_unwritable_report_mutation_is_zero_ticket(
    action_type: str,
    text: str,
    entity_type: str | None,
    entity_value: str,
    attributes: dict[str, Any],
    resources: dict[str, Any],
    reason_code: str,
) -> None:
    result = _admit(
        action_type=action_type,
        text=text,
        entity_type=entity_type,
        entity_value=entity_value,
        attributes=attributes,
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == reason_code
    assert result.tickets == ()
    assert result.interpretation.required_actions == ()


def test_periodic_report_owner_mismatch_is_zero_ticket() -> None:
    resources = _resources()
    resources["periodic_report"] = {
        **resources["periodic_report"],
        "owner_user_id": str(uuid5(NAMESPACE_URL, "report-domain-admission:other-user")),
    }

    result = _admit(
        action_type="submit_periodic_report",
        text="提交本周周报",
        entity_type="periodic_report",
        entity_value="本周周报",
        attributes={
            "report_type": "weekly",
            "report_id": PERIODIC_REPORT_ID,
            "version": 3,
            "period_key": "2026-W29",
        },
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "periodic_report_snapshot_not_authorized"
    assert result.tickets == ()


def test_ambiguous_daily_snapshot_is_zero_ticket() -> None:
    resources = _resources()
    resources["daily_draft"] = None

    result = _admit(
        action_type="query_daily_report",
        text="查看日报",
        entity_type="daily_report",
        entity_value="日报",
        attributes={},
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == (
        "daily_report_snapshot_not_uniquely_authorized"
    )
    assert result.tickets == ()


def test_daily_merge_across_sections_is_zero_ticket() -> None:
    result = _admit(
        action_type="merge_daily_items",
        text="合并今天工作第一条和风险第一条",
        entity_type="daily_item_target",
        entity_value="今天工作第一条和风险第一条",
        attributes={"target_item_ids": ["today-work-1", "today-risk-1"]},
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == (
        "daily_item_target_not_uniquely_authorized"
    )
    assert result.tickets == ()


def test_schema_has_closed_read_only_allowlist_and_mutation_ticket_branch() -> None:
    schema = json.loads(
        Path("docs/schemas/agent2-domain-admission.schema.json").read_text(
            encoding="utf-8"
        )
    )
    branches = schema["$defs"]["admission_decision"]["allOf"]
    read_only_branch = next(
        branch
        for branch in branches
        if branch.get("if", {})
        .get("properties", {})
        .get("operation", {})
        .get("enum")
    )
    mutation_branch = next(
        branch
        for branch in branches
        if branch.get("if", {})
        .get("properties", {})
        .get("operation", {})
        .get("not")
    )
    expected = {
        "query_daily_report",
        "query_periodic_report",
        "answer_case_query",
        "query_case_progress",
        "query_operation_status",
        "search_enterprise_knowledge",
    }

    assert set(
        read_only_branch["if"]["properties"]["operation"]["enum"]
    ) == expected
    assert read_only_branch["then"]["properties"]["ticket_id"] == {
        "type": "null"
    }
    assert set(
        mutation_branch["if"]["properties"]["operation"]["not"]["enum"]
    ) == expected
    assert mutation_branch["then"]["properties"]["ticket_id"] == {
        "$ref": "#/$defs/uuid_v5"
    }


def test_two_daily_mutations_bind_sequential_ticket_and_command_versions() -> None:
    first = "今天完成合同审核"
    second = "今天整理诉讼材料"
    text = f"{first}；{second}"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["report"],
            "segments": [
                {
                    "segment_id": "first-segment",
                    "text": first,
                    "intents": ["report"],
                    "entity_ids": ["first-event"],
                    "action_ids": ["first-action"],
                },
                {
                    "segment_id": "second-segment",
                    "text": second,
                    "intents": ["report"],
                    "entity_ids": ["second-event"],
                    "action_ids": ["second-action"],
                },
            ],
            "entities": [
                {
                    "entity_id": "first-event",
                    "entity_type": "daily_event",
                    "value": first,
                    "confidence": 1.0,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "second-event",
                    "entity_type": "daily_event",
                    "value": second,
                    "confidence": 1.0,
                    "attributes": {"field": "today_work"},
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "first-action",
                    "action_type": "capture_daily_event",
                    "intent": "report",
                    "entity_ids": ["first-event"],
                },
                {
                    "action_id": "second-action",
                    "action_type": "capture_daily_event",
                    "intent": "report",
                    "entity_ids": ["second-event"],
                },
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="report-sequential-version",
        message_id="report-sequential-version-message",
        text=text,
        occurred_at=NOW,
        resources=_resources(),
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )
    decision = asyncio.run(
        CognitiveCoreV3(
            _StaticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(),
            admission_enforced=True,
        ).process(turn, state)
    ).decision

    snapshot = DailyReportMutationSnapshot(
        report_id=UUID(CURRENT_REPORT_ID),
        owner_user_id=UUID(ACTOR_ID),
        version=4,
        status="collecting",
        today_work=("今天完成合同审核", "整理材料"),
        problems=("暂无",),
        tomorrow_plan=("联系法院",),
        item_ids={
            "today_work": ("today-work-1", "today-work-2"),
            "problems": ("today-risk-1",),
            "tomorrow_plan": ("today-plan-1",),
        },
    )
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=UUID(ACTOR_ID),
            daily_snapshot=snapshot,
            daily_history=(DailySnapshotReference(date(2026, 7, 14), snapshot),),
            current_report_date=date(2026, 7, 14),
        ),
    )

    assert plan.blocked_actions == ()
    assert [ticket.object_ref["version"] for ticket in decision.admission_tickets] == [
        4,
        5,
    ]
    assert [command.report_version for command in plan.daily_commands] == [4, 5]
    assert [
        command.admission_ticket["object_ref"]["version"]
        for command in plan.daily_commands
    ] == [4, 5]
