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
            "move_daily_items",
            "今天完成合同审核，这是明天的计划",
            "daily_item_target",
            "今天完成合同审核",
            {
                "target_item_ids": ["today-work-1"],
                "source_field": "today_work",
                "target_field": "tomorrow_plan",
            },
            {},
            "move_items",
            ["today-work-1"],
            {"target_field": "tomorrow_plan"},
            ["section", "items"],
        ),
        (
            "replace_daily_section",
            "明日计划：准备评审材料",
            "daily_report",
            "明日计划：准备评审材料",
            {
                "report_id": CURRENT_REPORT_ID,
                "version": 4,
                "field": "tomorrow_plan",
                "items": ["准备评审材料"],
            },
            {},
            "replace_section",
            [],
            {"field": "tomorrow_plan", "items": ["准备评审材料"]},
            ["section", "items"],
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


@pytest.mark.parametrize(
    ("text", "field_name", "model_item"),
    [
        (
            "今日工作：1. 刘聪说完成合同审核\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：- 刘聪说完成合同审核\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：十、刘聪说完成合同审核\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：（1） 刘聪说完成合同审核\n"
            "问题与风险：（1） 来函界面被调整\n"
            "明日计划：（1） 准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：(1) 刘聪说完成合同审核\n"
            "问题与风险：(1) 来函界面被调整\n"
            "明日计划：(1) 准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：如果完成合同审核，再提交材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：没有完成合同审核\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：准备评审材料",
            "today_work",
            "完成合同审核",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：明天南京出差取消",
            "tomorrow_plan",
            "明天南京出差",
        ),
        (
            "今日工作：1. 不处理合同\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：准备评审材料",
            "today_work",
            "处理合同",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 不跟进客户",
            "tomorrow_plan",
            "跟进客户",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 不去",
            "tomorrow_plan",
            "去",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 取消",
            "tomorrow_plan",
            "取消",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 撤销南京行程",
            "tomorrow_plan",
            "南京行程",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访已取消",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访已经撤销",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访被暂停",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访暂时终止",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访已取消。",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访已经撤销.……）",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访已经撤销\u200b",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访已经撤销⚠️",
            "tomorrow_plan",
            "客户拜访",
        ),
        (
            "今日工作：整理评审材料\n"
            "问题与风险：来函界面被调整\n"
            "明日计划：1. 客户拜访暂时终止啦",
            "tomorrow_plan",
            "客户拜访",
        ),
    ],
)
def test_replace_section_cannot_strip_nonassertive_framing_before_admission(
    text: str,
    field_name: str,
    model_item: str,
) -> None:
    result = _admit(
        action_type="replace_daily_section",
        text=text,
        entity_type="daily_report",
        entity_value=model_item,
        attributes={
            "report_id": CURRENT_REPORT_ID,
            "version": 4,
            "field": field_name,
            "items": [model_item],
        },
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == (
        "daily_section_replacement_not_authorized"
    )
    assert result.tickets == ()


@pytest.mark.parametrize(
    "text",
    [
        "明日计划：1. 客户拜访已取消。",
        "明日计划：1. 客户拜访已经撤销！",
        "明日计划：1. 客户拜访被暂停？",
        "明日计划：1. 客户拜访暂时终止，",
        "明日计划：1. 客户拜访已经撤销.",
        "明日计划：1. 客户拜访暂时终止……",
        "明日计划：1. 客户拜访已经撤销）",
        "明日计划：1. 客户拜访被暂停.……）",
        "明日计划：1. 客户拜访已经撤销\u200b",
        "明日计划：1. 客户拜访已经撤销⚠️",
        "明日计划：1. 客户拜访暂时终止啦",
        "明日计划：1. 客户拜访已经撤\u200b销",
        "明日计划：1. 客户拜访已经撤 销",
        "明日计划：1. 客户拜访已经撤-销",
        "明日计划：1. 客户拜访已经撤⚠️销",
        "明日计划：1. 客户拜访已经撤销了吧",
        "明日计划：1. 客户拜访已经撤销掉了吧",
        "明日计划：1. 客户拜访暂时终止了呢",
        "明日计划：1. 客户拜访已经撤销了没",
        "明日计划：1. 客户拜访已经撤销了没有",
        "明日计划：1. 客户拜访已经撤销了吗",
        "明日计划：1. 客户拜访已经取消哈",
        "明日计划：1. 客户拜访已经取消呐",
        "明日计划：1. 客户拜访已经取消诶",
        "明日计划：1. 客户拜访已经取消耶",
        "明日计划：1. 跟进着的客户拜访取消",
        "明日计划：1. 审核完的合同撤销",
        "明日计划：1. 办理了的许可撤销",
        "明日计划：1. 跟进多日的客户拜访取消",
        "明日计划：1. 跟进了一段时间的客户拜访取消",
        "明日计划：1. 审核多次的合同撤销",
        "明日计划：1. 处理许久的案件撤回",
        "明日计划：1. 办理到一半的许可撤销",
        "明日计划：1. 审核完毕的那个合同撤销",
        "明日计划：1. 跟进已久的客户拜访取消",
        "明日计划：1. 跟进了三个月的客户拜访取消",
        "明日计划：1. 处理过程中的案件撤回",
        "明日计划：1. 办理期间的该项许可撤销",
        "明日计划：1. 跟进超过一年的客户拜访取消",
        "明日计划：1. 审核完成后的合同撤销",
        "明日计划：1. 跟进客户拜访被告知取消",
        "明日计划：1. 处理公司会议被告知取消",
        "明日计划：1. 评估阶段的项目暂停",
        "明日计划：1. 研究阶段的项目暂停",
        "明日计划：1. 分析阶段的项目终止",
        "明日计划：1. 审查期内的许可撤销",
        "明日计划：1. 复核期间内的决定撤销",
        "明日计划：1. 研究状态下的项目暂停",
        "明日计划：1. 核查范围内的事项取消",
    ],
)
def test_capture_daily_event_cannot_drop_termination_state(text: str) -> None:
    result = _admit(
        action_type="capture_daily_event",
        text=text,
        entity_type="daily_event",
        entity_value="客户拜访",
        attributes={"field": "tomorrow_plan", "statement_mode": "asserted"},
    )

    assert result.decisions[0].status == "blocked"
    assert result.tickets == ()


@pytest.mark.parametrize(
    "item",
    [
        "客户拜访已取消。",
        "客户拜访已经撤销！",
        "客户拜访被暂停？",
        "客户拜访暂时终止，",
        "客户拜访已经撤销.",
        "客户拜访暂时终止……",
        "客户拜访已经撤销）",
        "客户拜访被暂停.……）",
        "客户拜访已经撤销\u200b",
        "客户拜访已经撤销⚠️",
        "客户拜访暂时终止啦",
        "客户拜访已经撤\u200b销",
        "客户拜访已经撤 销",
        "客户拜访已经撤-销",
        "客户拜访已经撤⚠️销",
        "客户拜访已经撤销了吧",
        "客户拜访已经撤销掉了吧",
        "客户拜访暂时终止了呢",
        "客户拜访已经撤销了没",
        "客户拜访已经撤销了没有",
        "客户拜访已经撤销了吗",
        "客户拜访已经取消哈",
        "客户拜访已经取消呐",
        "客户拜访已经取消诶",
        "客户拜访已经取消耶",
        "跟进着的客户拜访取消",
        "审核完的合同撤销",
        "办理了的许可撤销",
        "跟进多日的客户拜访取消",
        "跟进了一段时间的客户拜访取消",
        "审核多次的合同撤销",
        "处理许久的案件撤回",
        "办理到一半的许可撤销",
        "审核完毕的那个合同撤销",
        "跟进已久的客户拜访取消",
        "跟进了三个月的客户拜访取消",
        "处理过程中的案件撤回",
        "办理期间的该项许可撤销",
        "跟进超过一年的客户拜访取消",
        "审核完成后的合同撤销",
        "跟进客户拜访被告知取消",
        "处理公司会议被告知取消",
        "评估阶段的项目暂停",
        "研究阶段的项目暂停",
        "分析阶段的项目终止",
        "审查期内的许可撤销",
        "复核期间内的决定撤销",
        "研究状态下的项目暂停",
        "核查范围内的事项取消",
    ],
)
def test_replace_section_rejects_exact_termination_state(item: str) -> None:
    result = _admit(
        action_type="replace_daily_section",
        text=f"明日计划：1. {item}",
        entity_type="daily_report",
        entity_value=item,
        attributes={
            "report_id": CURRENT_REPORT_ID,
            "version": 4,
            "field": "tomorrow_plan",
            "items": [item],
        },
    )

    assert result.decisions[0].status == "blocked"
    assert result.tickets == ()


def test_capture_daily_event_cannot_strip_parenthesized_attribution() -> None:
    result = _admit(
        action_type="capture_daily_event",
        text="今日工作：（1） 刘聪说完成合同审核",
        entity_type="daily_event",
        entity_value="完成合同审核",
        attributes={"field": "today_work", "statement_mode": "asserted"},
    )

    assert result.decisions[0].status == "blocked"
    assert result.tickets == ()


def test_replace_section_accepts_exact_parenthesized_numbered_items() -> None:
    text = "明日计划：\n（1）准备评审材料\n（2）联系业务确认"
    result = _admit(
        action_type="replace_daily_section",
        text=text,
        entity_type="daily_report",
        entity_value=text,
        attributes={
            "report_id": CURRENT_REPORT_ID,
            "version": 4,
            "field": "tomorrow_plan",
            "items": ["准备评审材料", "联系业务确认"],
        },
    )

    assert result.decisions[0].status == "admitted"
    assert len(result.tickets) == 1
    assert result.tickets[0].authority_scope["patch"] == {
        "field": "tomorrow_plan",
        "items": ("准备评审材料", "联系业务确认"),
    }


@pytest.mark.parametrize(
    ("text", "entity_value", "field"),
    [
        ("今日工作：半年度绩效评估", "半年度绩效评估", "today_work"),
        ("今天完成了半年度绩效评估", "今天完成了半年度绩效评估", "today_work"),
        ("合同复核已经完成", "合同复核已经完成", "today_work"),
        ("明天去南京参加分公司会议", "明天去南京参加分公司会议", "tomorrow_plan"),
        ("问题与风险：来函流程仍未解决", "来函流程仍未解决", "problems"),
        ("明日计划：跟进商标撤销", "跟进商标撤销", "tomorrow_plan"),
        ("明日计划：研究合同撤销", "研究合同撤销", "tomorrow_plan"),
        ("明日计划：处理公司决议撤销", "处理公司决议撤销", "tomorrow_plan"),
        ("明日计划：跟进案件撤回", "跟进案件撤回", "tomorrow_plan"),
        ("明日计划：评估项目暂停", "评估项目暂停", "tomorrow_plan"),
        ("明日计划：研究已签合同撤销", "研究已签合同撤销", "tomorrow_plan"),
        ("明日计划：处理已登记商标撤销", "处理已登记商标撤销", "tomorrow_plan"),
        ("明日计划：继续跟进商标撤销", "继续跟进商标撤销", "tomorrow_plan"),
        ("明日计划：持续研究合同撤销", "持续研究合同撤销", "tomorrow_plan"),
        ("明日计划：明天处理公司决议撤销", "明天处理公司决议撤销", "tomorrow_plan"),
        ("明日计划：重点跟进案件撤回", "重点跟进案件撤回", "tomorrow_plan"),
        ("明日计划：进一步评估项目暂停", "进一步评估项目暂停", "tomorrow_plan"),
        ("明日计划：研究正式合同撤销", "研究正式合同撤销", "tomorrow_plan"),
        ("明日计划：跟进确认书撤销", "跟进确认书撤销", "tomorrow_plan"),
        ("明日计划：分析主动撤回", "分析主动撤回", "tomorrow_plan"),
        (
            "明日计划：评估实际控制人资格取消",
            "评估实际控制人资格取消",
            "tomorrow_plan",
        ),
        (
            "明日计划：研究全面履行后的合同撤销",
            "研究全面履行后的合同撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：处理明确授权的撤销",
            "处理明确授权的撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：研究行政许可依法撤销",
            "研究行政许可依法撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：研究被许可人资格取消",
            "研究被许可人资格取消",
            "tomorrow_plan",
        ),
        (
            "明日计划：评估被投资企业项目暂停",
            "评估被投资企业项目暂停",
            "tomorrow_plan",
        ),
        (
            "明日计划：核查被审计单位资格撤销",
            "核查被审计单位资格撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：研究被监护人授权撤回",
            "研究被监护人授权撤回",
            "tomorrow_plan",
        ),
        (
            "明日计划：处理被特许经营资格取消",
            "处理被特许经营资格取消",
            "tomorrow_plan",
        ),
        (
            "明日计划：研究已经生效的合同撤销",
            "研究已经生效的合同撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：分析曾经签订的协议撤销",
            "分析曾经签订的协议撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：评估已经履行的项目终止",
            "评估已经履行的项目终止",
            "tomorrow_plan",
        ),
        (
            "明日计划：核查已经授权的资格取消",
            "核查已经授权的资格取消",
            "tomorrow_plan",
        ),
        (
            "明日计划：办理客户的申请撤回",
            "办理客户的申请撤回",
            "tomorrow_plan",
        ),
        (
            "明日计划：跟进客户的许可撤销",
            "跟进客户的许可撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：处理公司的决议撤销",
            "处理公司的决议撤销",
            "tomorrow_plan",
        ),
        (
            "明日计划：办理被许可人的资格取消",
            "办理被许可人的资格取消",
            "tomorrow_plan",
        ),
        (
            "明日计划：处理已经生效的合同撤销",
            "处理已经生效的合同撤销",
            "tomorrow_plan",
        ),
        ("明日计划：研究当前合同撤销", "研究当前合同撤销", "tomorrow_plan"),
        ("明日计划：研究临时合同撤销", "研究临时合同撤销", "tomorrow_plan"),
        (
            "今日工作：核查合同是否完成",
            "核查合同是否完成",
            "today_work",
        ),
        (
            "今日工作：研究合同撤销与否",
            "研究合同撤销与否",
            "today_work",
        ),
        (
            "今日工作：评估项目继续与否",
            "评估项目继续与否",
            "today_work",
        ),
        (
            "今日工作：核查授权有效与否",
            "核查授权有效与否",
            "today_work",
        ),
        (
            "今日工作：核查材料发没发",
            "核查材料发没发",
            "today_work",
        ),
        (
            "今日工作：研究项目做不做",
            "研究项目做不做",
            "today_work",
        ),
    ],
)
def test_explicit_daily_fact_can_open_current_draft_without_active_daily_task(
    text: str,
    entity_value: str,
    field: str,
) -> None:
    resources = _resources()
    resources["active_tasks"] = []

    result = _admit(
        action_type="capture_daily_event",
        text=text,
        entity_type="daily_event",
        entity_value=entity_value,
        attributes={"field": field},
        resources=resources,
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].reason_code == "explicit_daily_fact_authorized"
    assert len(result.tickets) == 1


def test_affirmative_daily_clause_is_not_poisoned_by_a_sibling_question() -> None:
    resources = _resources()
    resources["active_tasks"] = []

    result = _admit(
        action_type="capture_daily_event",
        text="今天完成合同复核；证据目录什么时候提交？",
        entity_type="daily_event",
        entity_value="今天完成合同复核",
        attributes={"field": "today_work", "statement_mode": "asserted"},
        resources=resources,
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].reason_code == "explicit_daily_fact_authorized"
    assert len(result.tickets) == 1


def test_hypothetical_daily_clause_stays_blocked_beside_a_question() -> None:
    resources = _resources()
    resources["active_tasks"] = []

    result = _admit(
        action_type="capture_daily_event",
        text="如果今天完成合同复核，再提交给业务；证据目录什么时候提交？",
        entity_type="daily_event",
        entity_value="如果今天完成合同复核，再提交给业务",
        attributes={"field": "today_work", "statement_mode": "asserted"},
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "daily_statement_not_asserted"
    assert result.tickets == ()


@pytest.mark.parametrize(
    ("text", "field", "expected_reason"),
    [
        (
            "半年度绩效评估",
            "today_work",
            "report_context_not_uniquely_authorized",
        ),
        (
            "南京出差安排是什么？",
            "tomorrow_plan",
            "daily_statement_not_asserted",
        ),
        ("最近有什么风险？", "problems", "daily_statement_not_asserted"),
        (
            "如果明天去南京，再提前准备材料",
            "tomorrow_plan",
            "daily_statement_not_asserted",
        ),
        (
            "刘某说今天完成了合同审核",
            "today_work",
            "daily_statement_reported_or_quoted",
        ),
        (
            "同事说合同复核已经完成",
            "today_work",
            "daily_statement_reported_or_quoted",
        ),
        (
            "如果合同复核已经完成，再提交材料",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核是否已经完成？",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了没",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了没有",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了-没",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了\u200b没",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了⚠️没",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了…没有",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成没有",
            "today_work",
            "daily_statement_not_asserted",
        ),
        ("合同复核完成没", "today_work", "daily_statement_not_asserted"),
        (
            "合同审核做完没有",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "材料整理好没有",
            "today_work",
            "daily_statement_not_asserted",
        ),
        ("合同复核完成否", "today_work", "daily_statement_not_asserted"),
        (
            "合同复核完成了对吧",
            "today_work",
            "daily_statement_not_asserted",
        ),
        (
            "合同复核完成了对不对",
            "today_work",
            "daily_statement_not_asserted",
        ),
        ("合同复核完成了吧", "today_work", "daily_statement_not_asserted"),
        ("合同复核完成没呢", "today_work", "daily_statement_not_asserted"),
        ("合同复核完成不", "today_work", "daily_statement_not_asserted"),
        ("合同看了没", "today_work", "daily_statement_not_asserted"),
        ("材料发了没", "today_work", "daily_statement_not_asserted"),
        ("会开完没有", "today_work", "daily_statement_not_asserted"),
        ("绩效评估弄完没", "today_work", "daily_statement_not_asserted"),
        ("文档写了没", "today_work", "daily_statement_not_asserted"),
        ("案件结了没", "today_work", "daily_statement_not_asserted"),
        ("材料发没发", "today_work", "daily_statement_not_asserted"),
        ("合同审没审", "today_work", "daily_statement_not_asserted"),
        ("会议开没开", "today_work", "daily_statement_not_asserted"),
        ("日报写没写", "today_work", "daily_statement_not_asserted"),
        ("材料发不发", "today_work", "daily_statement_not_asserted"),
        ("合同审不审", "today_work", "daily_statement_not_asserted"),
        ("材料发没", "today_work", "daily_statement_not_asserted"),
        ("合同审不", "today_work", "daily_statement_not_asserted"),
        ("材料发对吧", "today_work", "daily_statement_not_asserted"),
        ("材料发吧", "today_work", "daily_statement_not_asserted"),
        ("合同审没审完", "today_work", "daily_statement_not_asserted"),
        ("材料发没发完", "today_work", "daily_statement_not_asserted"),
        ("会议开没开完", "today_work", "daily_statement_not_asserted"),
        ("日报写没写完", "today_work", "daily_statement_not_asserted"),
        ("合同审不审完", "today_work", "daily_statement_not_asserted"),
        ("材料发还是不发", "today_work", "daily_statement_not_asserted"),
        ("材料发了还是没发完", "today_work", "daily_statement_not_asserted"),
        ("合同审没审完全部条款", "today_work", "daily_statement_not_asserted"),
        ("材料发没发给业务部门", "today_work", "daily_statement_not_asserted"),
        ("会议开没开出明确结论", "today_work", "daily_statement_not_asserted"),
        ("日报写没写完全部内容", "today_work", "daily_statement_not_asserted"),
        ("材料发还是不发给业务部门", "today_work", "daily_statement_not_asserted"),
        ("材料发没发给HR", "today_work", "daily_statement_not_asserted"),
        ("合同审没审完NDA", "today_work", "daily_statement_not_asserted"),
        ("会议开没开出Q3结论", "today_work", "daily_statement_not_asserted"),
        ("日报写没写完2026Q3内容", "today_work", "daily_statement_not_asserted"),
        ("材料发还是不发给A组", "today_work", "daily_statement_not_asserted"),
        ("NDA review了没", "today_work", "daily_statement_not_asserted"),
        ("合同review了没有", "today_work", "daily_statement_not_asserted"),
        ("材料check了没", "today_work", "daily_statement_not_asserted"),
        ("PR merge了没", "today_work", "daily_statement_not_asserted"),
        ("NDA review没review完", "today_work", "daily_statement_not_asserted"),
        ("文档check没check", "today_work", "daily_statement_not_asserted"),
        ("NDA review过没", "today_work", "daily_statement_not_asserted"),
        ("合同check过没有", "today_work", "daily_statement_not_asserted"),
        ("PR merge过没", "today_work", "daily_statement_not_asserted"),
        ("材料review过没", "today_work", "daily_statement_not_asserted"),
        ("NDA reviewed没", "today_work", "daily_statement_not_asserted"),
        ("PR merged没有", "today_work", "daily_statement_not_asserted"),
        ("合同checked没有", "today_work", "daily_statement_not_asserted"),
        ("合同是不是看完了", "today_work", "daily_statement_not_asserted"),
        ("材料有没有发出", "today_work", "daily_statement_not_asserted"),
        (
            "合同复核尚未完成",
            "today_work",
            "daily_positive_work_fact_negated",
        ),
        (
            "会议纪要写着明天去南京出差",
            "tomorrow_plan",
            "daily_statement_reported_or_quoted",
        ),
    ],
)
def test_unscoped_or_question_daily_candidate_stays_blocked_without_active_task(
    text: str,
    field: str,
    expected_reason: str,
) -> None:
    resources = _resources()
    resources["active_tasks"] = []

    result = _admit(
        action_type="capture_daily_event",
        text=text,
        entity_type="daily_event",
        entity_value=text,
        attributes={"field": field},
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == expected_reason
    assert result.tickets == ()


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("今天没有完成合同审核", "today_work"),
        ("明天不去南京出差了", "tomorrow_plan"),
        ("取消明天南京的会议安排", "tomorrow_plan"),
        ("不是明天，是后天", "tomorrow_plan"),
    ],
)
def test_correction_or_cancellation_cannot_authorize_a_positive_daily_append(
    text: str,
    field: str,
) -> None:
    """This blocks only a false positive append, not the business action itself.

    Cancellation and correction may still produce a Travel update, a Daily item
    move, or clarification through their own bound-object contracts.
    """

    resources = _resources()
    resources["active_tasks"] = []

    result = _admit(
        action_type="capture_daily_event",
        text=text,
        entity_type="daily_event",
        entity_value=text,
        attributes={"field": field},
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.tickets == ()


@pytest.mark.parametrize(
    ("text", "field", "statement_mode"),
    [
        ("如果今天完成合同审核，再提交给业务部门", "today_work", "hypothetical"),
        ("某同事说今天完成了合同审核", "today_work", "quoted"),
        ("今天完成合同审核了吗？", "today_work", "question"),
        ("如果今天完成合同审核，再提交给业务部门", "today_work", "asserted"),
        ("某同事说今天完成了合同审核", "today_work", "asserted"),
        ("今天完成合同审核了没", "today_work", "asserted"),
        ("今天完成合同审核了没有", "today_work", "asserted"),
        ("今天完成合同审核了-没", "today_work", "asserted"),
        ("今天完成合同审核了\u200b没", "today_work", "asserted"),
        ("今天完成合同审核了⚠️没", "today_work", "asserted"),
        ("今天完成合同审核了…没有", "today_work", "asserted"),
        ("明天南京出差取消", "tomorrow_plan", "asserted"),
        ("跟进客户拜访已经正式取消", "tomorrow_plan", "asserted"),
        ("处理公司会议已确认取消", "tomorrow_plan", "asserted"),
        ("评估项目目前已经全面暂停", "tomorrow_plan", "asserted"),
        ("跟进案件被法院裁定终止", "tomorrow_plan", "asserted"),
        ("研究合同现已依法撤销", "tomorrow_plan", "asserted"),
        ("跟进客户拜访曾经取消", "tomorrow_plan", "asserted"),
        ("跟进的客户拜访最终取消", "tomorrow_plan", "asserted"),
        ("跟进的客户拜访取消", "tomorrow_plan", "asserted"),
        ("跟进的客户拜访后来取消", "tomorrow_plan", "asserted"),
        ("跟进的客户拜访突然取消", "tomorrow_plan", "asserted"),
        ("跟进中的客户拜访取消", "tomorrow_plan", "asserted"),
        ("处理中的案件撤回", "tomorrow_plan", "asserted"),
        ("审核过的合同撤销", "tomorrow_plan", "asserted"),
        (
            "跟进客户拜访已经因为天气和场地安排发生重大变化而取消",
            "tomorrow_plan",
            "asserted",
        ),
        (
            "处理公司会议已由业务部门经过全面评估后决定取消",
            "tomorrow_plan",
            "asserted",
        ),
        (
            "跟进客户拜访目前由业务部门经过评估后决定取消",
            "tomorrow_plan",
            "asserted",
        ),
        (
            "跟进客户拜访后来因为天气和场地变化而取消",
            "tomorrow_plan",
            "asserted",
        ),
        ("跟进着的客户拜访取消", "tomorrow_plan", "asserted"),
        ("审核完的合同撤销", "tomorrow_plan", "asserted"),
        ("办理了的许可撤销", "tomorrow_plan", "asserted"),
        ("跟进多日的客户拜访取消", "tomorrow_plan", "asserted"),
        ("跟进了一段时间的客户拜访取消", "tomorrow_plan", "asserted"),
        ("审核多次的合同撤销", "tomorrow_plan", "asserted"),
        ("处理许久的案件撤回", "tomorrow_plan", "asserted"),
        ("办理到一半的许可撤销", "tomorrow_plan", "asserted"),
        ("审核完毕的那个合同撤销", "tomorrow_plan", "asserted"),
        ("跟进已久的客户拜访取消", "tomorrow_plan", "asserted"),
        ("跟进了三个月的客户拜访取消", "tomorrow_plan", "asserted"),
        ("处理过程中的案件撤回", "tomorrow_plan", "asserted"),
        ("办理期间的该项许可撤销", "tomorrow_plan", "asserted"),
        ("跟进超过一年的客户拜访取消", "tomorrow_plan", "asserted"),
        ("审核完成后的合同撤销", "tomorrow_plan", "asserted"),
        ("跟进客户拜访被告知取消", "tomorrow_plan", "asserted"),
        ("处理公司会议被告知取消", "tomorrow_plan", "asserted"),
        ("评估项目被告知暂停", "tomorrow_plan", "asserted"),
        ("研究合同被告知撤销", "tomorrow_plan", "asserted"),
        ("评估阶段的项目暂停", "tomorrow_plan", "asserted"),
        ("研究阶段的项目暂停", "tomorrow_plan", "asserted"),
        ("分析阶段的项目终止", "tomorrow_plan", "asserted"),
        ("审查期内的许可撤销", "tomorrow_plan", "asserted"),
        ("复核期间内的决定撤销", "tomorrow_plan", "asserted"),
        ("研究状态下的项目暂停", "tomorrow_plan", "asserted"),
        ("核查范围内的事项取消", "tomorrow_plan", "asserted"),
        ("评估阶段里的那个项目暂停", "tomorrow_plan", "asserted"),
        ("研究初期的那个项目暂停", "tomorrow_plan", "asserted"),
        ("复核环节里的那个决定撤销", "tomorrow_plan", "asserted"),
        ("审查流程上的该项许可撤销", "tomorrow_plan", "asserted"),
        ("跟进客户的项目已经取消", "tomorrow_plan", "asserted"),
        ("办理合作方的许可被撤销", "tomorrow_plan", "asserted"),
        ("处理供应商的申请最终撤回", "tomorrow_plan", "asserted"),
    ],
)
def test_active_daily_context_cannot_authorize_nonassertive_positive_capture(
    text: str,
    field: str,
    statement_mode: str,
) -> None:
    resources = _resources()
    resources["active_tasks"] = [
        {
            "workflow": "daily_report",
            "task_id": "active-daily-task",
            "status": "collecting",
        }
    ]

    result = _admit(
        action_type="capture_daily_event",
        text=text,
        entity_type="daily_event",
        entity_value=text,
        attributes={"field": field, "statement_mode": statement_mode},
        resources=resources,
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code in {
        "daily_statement_not_asserted",
        "daily_statement_reported_or_quoted",
        "daily_positive_plan_fact_cancelled",
    }
    assert result.tickets == ()


@pytest.mark.parametrize(
    "attributes",
    [
        {
            "target_item_ids": ["missing-item"],
            "source_field": "today_work",
            "target_field": "tomorrow_plan",
        },
        {
            "target_item_ids": ["today-work-1"],
            "source_field": "tomorrow_plan",
            "target_field": "today_work",
        },
        {
            "target_item_ids": ["today-work-1"],
            "source_field": "today_work",
            "target_field": "today_work",
        },
    ],
)
def test_move_daily_item_rejects_untrusted_source_or_target(attributes: dict[str, Any]) -> None:
    result = _admit(
        action_type="move_daily_items",
        text="今天完成合同审核，这是明天的计划",
        entity_type="daily_item_target",
        entity_value="今天完成合同审核",
        attributes=attributes,
    )

    assert result.decisions[0].status == "blocked"
    assert result.tickets == ()


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
