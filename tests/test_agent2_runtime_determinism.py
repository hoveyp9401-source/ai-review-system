from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.agent2.cognitive_core_v3 import SemanticInterpretation
from app.agent2.evaluation.determinism_report import build_determinism_report
from app.agent2.runtime.blind import BlindInputPack, BlindSemanticRunner


class DeterministicInterpreter:
    async def interpret(self, turn, state):
        entity_id = f"event-{turn.message_id}"
        action_id = f"capture-{turn.message_id}"
        return SemanticInterpretation.from_payload(
            {
                "intents": ["daily_append"],
                "segments": [
                    {
                        "segment_id": f"segment-{turn.message_id}",
                        "text": turn.text,
                        "intents": ["daily_append"],
                        "entity_ids": [entity_id],
                        "action_ids": [action_id],
                    }
                ],
                "entities": [
                    {
                        "entity_id": entity_id,
                        "entity_type": "daily_event",
                        "value": turn.text,
                        "confidence": 1.0,
                        "attributes": {"field": "today_work"},
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": action_id,
                        "action_type": "capture_daily_event",
                        "intent": "daily_append",
                        "entity_ids": [entity_id],
                    }
                ],
                "clarification_need": None,
                "context_update": {
                    "current_goal": "daily_append",
                    "remember_entity_ids": [entity_id],
                    "remember_turn": True,
                },
            }
        )


def _pack(case_count: int = 8) -> BlindInputPack:
    cases = []
    for index in range(case_count):
        actor_id = uuid5(NAMESPACE_URL, f"determinism-actor-{index}")
        cases.append(
            {
                "case_id": f"opaque-determinism-case-{index}",
                "actor_id": str(actor_id),
                "conversation_id": f"opaque-determinism-conversation-{index}",
                "initial_state": None,
                "initial_daily_snapshot": {
                    "report_id": str(uuid5(NAMESPACE_URL, f"determinism-report-{index}")),
                    "version": 0,
                    "status": "collecting",
                    "today_work": [],
                    "problems": [],
                    "tomorrow_plan": [],
                    "item_ids": {},
                },
                "runtime_config": {
                    "daily_policy": {"current_report_date": "2026-07-10"},
                    "active_tasks": [],
                },
                "turns": [
                    {
                        "turn_id": "turn-1",
                        "raw_text": f"请记入日报：完成合同审核-{index}",
                        "occurred_at": datetime(
                            2026,
                            7,
                            10,
                            9,
                            index,
                            tzinfo=timezone.utc,
                        ).isoformat(),
                        "channel": "deterministic_replay",
                        "request_metadata": {"external_message_id": f"message-{index}"},
                    },
                    {
                        "turn_id": "turn-2",
                        "raw_text": f"请记入日报：整理案件材料-{index}",
                        "occurred_at": datetime(
                            2026,
                            7,
                            10,
                            10,
                            index,
                            tzinfo=timezone.utc,
                        ).isoformat(),
                        "channel": "deterministic_replay",
                        "request_metadata": {"external_message_id": f"message-{index}-2"},
                    },
                ],
            }
        )
    return BlindInputPack.from_mapping(
        {
            "schema_version": "agent2.runtime_blind_input.v1",
            "pack_id": "deterministic-pack",
            "cases": cases,
        }
    )


def test_same_blind_input_and_runtime_version_produce_identical_actual_artifact():
    pack = _pack()
    first = asyncio.run(
        BlindSemanticRunner(
            DeterministicInterpreter(),
            runtime_version_hash="fixed-runtime-version",
            max_concurrency=1,
        ).run(pack)
    )
    concurrent = asyncio.run(
        BlindSemanticRunner(
            DeterministicInterpreter(),
            runtime_version_hash="fixed-runtime-version",
            max_concurrency=4,
        ).run(pack)
    )

    assert first.as_mapping() == concurrent.as_mapping()
    assert first.artifact_hash == concurrent.artifact_hash
    assert first.run_id == concurrent.run_id


def test_ephemeral_cases_have_independent_state_and_command_identity():
    actual = asyncio.run(
        BlindSemanticRunner(
            DeterministicInterpreter(),
            runtime_version_hash="fixed-runtime-version",
            max_concurrency=4,
        ).run(_pack(case_count=4))
    )

    command_ids: set[str] = set()
    for case in actual.cases:
        turns = case["turns"]
        assert [turn["state"]["version"] for turn in turns] == [1, 2]
        case_command_ids = {
            command["command_id"]
            for turn in turns
            for command in turn["typed_commands"]
        }
        assert len(case_command_ids) == 2
        assert not command_ids.intersection(case_command_ids)
        command_ids.update(case_command_ids)
        assert all(turn["actual_write"] is False for turn in turns)
        assert all(turn["legacy_fallback_used"] is False for turn in turns)

    assert len(command_ids) == 8


def test_determinism_report_exposes_semantic_and_command_drift_without_hiding_safety():
    pack = _pack(case_count=1)
    first = asyncio.run(
        BlindSemanticRunner(
            DeterministicInterpreter(),
            runtime_version_hash="fixed-runtime-version",
        ).run(pack)
    )
    payload = first.as_mapping()
    payload["cases"][0]["turns"][0]["action_class"] = ["record_case_progress"]
    second = type(first).completed_artifact(
        input_pack=pack,
        runtime_version_hash="fixed-runtime-version",
        cases=tuple(payload["cases"]),
    )

    report = build_determinism_report(first, second)

    assert report["comparable"] is True
    assert report["determinism_passed"] is False
    assert report["turns"] == {
        "total": 2,
        "exact": 1,
        "different": 1,
        "missing_from_first": 0,
        "missing_from_second": 0,
    }
    assert report["top_level_field_difference_turn_counts"]["action_class"] == 1
    assert report["safety_envelope_stable"] is True


def test_determinism_report_rejects_different_input_or_runtime_versions():
    pack = _pack(case_count=1)
    first = asyncio.run(
        BlindSemanticRunner(
            DeterministicInterpreter(),
            runtime_version_hash="runtime-a",
        ).run(pack)
    )
    second = asyncio.run(
        BlindSemanticRunner(
            DeterministicInterpreter(),
            runtime_version_hash="runtime-b",
        ).run(pack)
    )

    report = build_determinism_report(first, second)

    assert report["comparable"] is False
    assert report["determinism_passed"] is False
    assert report["comparison_blockers"] == ["runtime_version_hash_mismatch"]
