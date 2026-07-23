from __future__ import annotations

import asyncio
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from app.agent2.cognitive_core_v3 import CognitiveTurn, SemanticInterpretation
from app.agent2.conversation_state import ConversationState
from app.agent2.semantic_interpreter_v3 import (
    LLMCognitiveSemanticInterpreter,
    _normalize_action_free_daily_report_context,
    _normalize_daily_event_attributes,
    _normalize_daily_section_clear_binding,
    _normalize_multi_entity_daily_capture_actions,
)


class _PayloadClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0

    async def complete_json(self, **kwargs: object) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def test_clear_daily_section_repairs_provider_target_binding_through_interpreter() -> None:
    text = "请清空明日计划"
    report_id = "10000000-0000-0000-0000-000000000001"
    payload = {
        "intents": ["daily_modify"],
        "segments": [
            {
                "segment_id": "clear-plan",
                "text": text,
                "intents": ["daily_modify"],
                "entity_ids": ["wrong-item-target", "current-report"],
                "action_ids": ["clear-plan-action"],
            }
        ],
        "entities": [
            {
                "entity_id": "wrong-item-target",
                "entity_type": "daily_item_target",
                "value": "明日计划",
                "confidence": 1.0,
                "attributes": {
                    "target_field": "tomorrow_plan",
                    "target_item_ids": ["plan-1"],
                },
            },
            {
                "entity_id": "current-report",
                "entity_type": "daily_report",
                "value": "当前日报",
                "confidence": 1.0,
                "attributes": {
                    "report_id": report_id,
                    "version": 4,
                    "report_date": "2026-07-22",
                },
            },
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "clear-plan-action",
                "action_type": "clear_daily_section",
                "intent": "daily_modify",
                "entity_ids": ["wrong-item-target", "current-report"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_modify",
            "remember_entity_ids": ["wrong-item-target", "current-report"],
            "remember_turn": True,
        },
    }
    client = _PayloadClient(payload)
    interpreter = LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    )
    user_id = "tenant-clear-section:actor-clear-section"
    conversation_id = "conversation-clear-section"

    result = asyncio.run(
        interpreter.interpret(
            CognitiveTurn(
                tenant_id="tenant-clear-section",
                actor_user_id="actor-clear-section",
                user_id=user_id,
                conversation_id=conversation_id,
                message_id="message-clear-section",
                text=text,
                occurred_at=datetime(2026, 7, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                resources={},
            ),
            ConversationState.empty(
                user_id=user_id,
                conversation_id=conversation_id,
            ),
        )
    )

    assert client.calls == 1
    assert result.required_actions[0].entity_ids == ("current-report",)
    report = next(entity for entity in result.entities if entity.entity_id == "current-report")
    assert report.attributes["field"] == "tomorrow_plan"
    assert result.context_update.remember_entity_ids == ("current-report",)


def test_clear_daily_section_does_not_repair_conflicting_fields() -> None:
    payload = {
        "entities": [
            {
                "entity_id": "item-target",
                "entity_type": "daily_item_target",
                "attributes": {"target_field": "tomorrow_plan"},
            },
            {
                "entity_id": "report-target",
                "entity_type": "daily_report",
                "attributes": {"field": "today_work"},
            },
        ],
        "required_actions": [
            {
                "action_id": "clear-section",
                "action_type": "clear_daily_section",
                "entity_ids": ["item-target", "report-target"],
            }
        ],
    }

    assert _normalize_daily_section_clear_binding(payload) == payload


def test_batched_daily_capture_is_split_without_inventing_entities_or_values() -> None:
    payload = {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "segment-1",
                "text": "完成合同复核，存在材料延误风险，明天继续跟进",
                "intents": ["daily_append"],
                "entity_ids": ["work", "risk", "plan"],
                "action_ids": ["capture-all"],
            }
        ],
        "entities": [
            {
                "entity_id": "work",
                "entity_type": "daily_event",
                "value": "完成合同复核",
                "confidence": 0.9,
                "attributes": {"field": "today_work", "statement_mode": "asserted"},
            },
            {
                "entity_id": "risk",
                "entity_type": "daily_event",
                "value": "材料延误风险",
                "confidence": 0.9,
                "attributes": {"field": "problems", "statement_mode": "asserted"},
            },
            {
                "entity_id": "plan",
                "entity_type": "daily_event",
                "value": "继续跟进",
                "confidence": 0.9,
                "attributes": {"field": "tomorrow_plan", "statement_mode": "asserted"},
            },
        ],
        "confidence": 0.9,
        "required_actions": [
            {
                "action_id": "capture-all",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["work", "risk", "plan"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {"remember_turn": True},
    }

    normalized = _normalize_multi_entity_daily_capture_actions(payload)
    interpretation = SemanticInterpretation.from_payload(normalized)

    assert [entity.entity_id for entity in interpretation.entities] == [
        "work",
        "risk",
        "plan",
    ]
    assert [action.entity_ids for action in interpretation.required_actions] == [
        ("work",),
        ("risk",),
        ("plan",),
    ]
    assert interpretation.segments[0].action_ids == tuple(
        action.action_id for action in interpretation.required_actions
    )


def test_daily_capture_drops_non_daily_context_binding_before_split() -> None:
    payload = {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "daily-document",
                "text": "今日工作完成合同审核，明天继续跟进",
                "intents": ["daily_append"],
                "entity_ids": ["report", "work", "plan"],
                "action_ids": ["capture-work", "capture-plan"],
            }
        ],
        "entities": [
            {
                "entity_id": "report",
                "entity_type": "daily_report",
                "value": "当前日报",
                "attributes": {"report_id": "report-1", "version": 0},
            },
            {
                "entity_id": "work",
                "entity_type": "daily_event",
                "value": "完成合同审核",
                "attributes": {"field": "today_work", "statement_mode": "asserted"},
            },
            {
                "entity_id": "plan",
                "entity_type": "daily_event",
                "value": "继续跟进",
                "attributes": {"field": "tomorrow_plan", "statement_mode": "asserted"},
            },
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "capture-work",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["report", "work"],
                "parameters": {},
            },
            {
                "action_id": "capture-plan",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["report", "plan"],
                "parameters": {},
            },
        ],
        "clarification_need": None,
        "context_update": {"remember_entity_ids": ["report"], "remember_turn": True},
    }

    normalized = _normalize_multi_entity_daily_capture_actions(payload)
    interpretation = SemanticInterpretation.from_payload(normalized)

    assert [action.entity_ids for action in interpretation.required_actions] == [
        ("work",),
        ("plan",),
    ]
    assert interpretation.entities[0].entity_type == "daily_report"
    assert interpretation.context_update.remember_entity_ids == ("report",)


def test_non_daily_or_single_entity_actions_are_not_rewritten() -> None:
    payload = {
        "entities": [
            {
                "entity_id": "travel",
                "entity_type": "travel_event",
                "value": "南京",
            }
        ],
        "required_actions": [
            {
                "action_id": "travel-action",
                "action_type": "record_travel_event",
                "entity_ids": ["travel"],
            }
        ],
        "segments": [],
    }

    assert _normalize_multi_entity_daily_capture_actions(payload) == payload


def test_daily_event_drops_only_extraneous_model_evidence_spans() -> None:
    payload = {
        "entities": [
            {
                "entity_id": "daily",
                "entity_type": "daily_event",
                "value": "今天完成合同审核",
                "attributes": {
                    "field": "today_work",
                    "statement_mode": "asserted",
                    "evidence_spans": [[0, 8]],
                    "unsupported_key": "must-still-fail-closed",
                },
            },
            {
                "entity_id": "travel",
                "entity_type": "travel_event",
                "value": "南京",
                "attributes": {"evidence_spans": [[0, 2]]},
            },
        ]
    }

    normalized = _normalize_daily_event_attributes(payload)

    assert normalized["entities"][0]["attributes"] == {
        "field": "today_work",
        "statement_mode": "asserted",
        "unsupported_key": "must-still-fail-closed",
    }
    assert normalized["entities"][1]["attributes"] == {
        "evidence_spans": [[0, 2]]
    }


def test_action_free_daily_report_drops_only_invalid_embedded_items() -> None:
    payload = {
        "intents": ["daily_report"],
        "segments": [
            {
                "segment_id": "report-context",
                "text": "当前日报",
                "intents": ["daily_report"],
                "entity_ids": ["report"],
                "action_ids": [],
            }
        ],
        "entities": [
            {
                "entity_id": "report",
                "entity_type": "daily_report",
                "value": "当前日报",
                "attributes": {
                    "report_id": "report-1",
                    "version": 2,
                    "report_date": "2026-07-22",
                    "field": "today_work",
                    "items": [{"value": "provider-shaped-item"}],
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"remember_entity_ids": ["report"]},
    }

    normalized = _normalize_action_free_daily_report_context(payload)
    interpretation = SemanticInterpretation.from_payload(normalized)

    assert interpretation.entities[0].attributes == {
        "report_id": "report-1",
        "version": 2,
        "report_date": "2026-07-22",
        "field": "today_work",
    }


def test_action_bound_daily_report_invalid_items_remain_fail_closed() -> None:
    payload = {
        "entities": [
            {
                "entity_id": "report",
                "entity_type": "daily_report",
                "value": "当前日报",
                "attributes": {
                    "report_id": "report-1",
                    "version": 2,
                    "items": [{"value": "provider-shaped-item"}],
                },
            }
        ],
        "required_actions": [
            {
                "action_id": "replace",
                "action_type": "replace_daily_section",
                "intent": "daily_modify",
                "entity_ids": ["report"],
                "parameters": {},
            }
        ],
        "segments": [],
        "context_update": {},
    }

    assert _normalize_action_free_daily_report_context(payload) == payload
