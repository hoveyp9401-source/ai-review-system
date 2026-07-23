from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app.agent2.runtime.blind import (
    BlindActualArtifact,
    BlindInputPack,
    BlindSemanticRunner,
    FileActualArtifactStore,
    _runtime_version_hash,
    _runtime_version_material_paths,
)
from app.agent2.evaluation.runtime_scoring import SealedLabelStore, score_actual_artifact
from app.agent2.evaluation.blind_pack_builder import build_blind_pack
from app.agent2.evaluation.review_packet import build_reviewer_packet
from app.agent2.evaluation.ledger_report import build_ledger_closure_report
from app.agent2.runtime.replay import RuntimeReplayCase, RuntimeReplayTurn
from app.agent2.cognitive_core_v3 import (
    CognitiveTurn,
    SemanticInputLimitExceeded,
    SemanticInterpretation,
)
from app.agent2.cognitive_contract_v3 import validate_semantic_interpretation_contract
from app.agent2.conversation_state import BoundPending, ConversationEntity, ConversationState
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter


def test_runtime_version_materials_cover_prompt_and_semantic_contract():
    repo_root = Path(__file__).resolve().parents[1]
    relative_paths = {
        path.relative_to(repo_root).as_posix()
        for path in _runtime_version_material_paths()
    }

    assert "app/agent2/cognitive_contract_v3.py" in relative_paths
    assert "app/llm/prompts/cognitive_core_v3.md" in relative_paths
    assert "app/agent2/runtime/shadow_adapter.py" not in relative_paths
    assert "app/agent2/runtime/replay.py" not in relative_paths


def test_runtime_version_hash_changes_when_material_changes(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("version one", encoding="utf-8")
    first = _runtime_version_hash(paths=(prompt,))

    prompt.write_text("version two", encoding="utf-8")
    second = _runtime_version_hash(paths=(prompt,))

    assert first != second


def test_runtime_version_hash_changes_with_interpreter_identity(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text("runtime", encoding="utf-8")

    first = _runtime_version_hash(
        paths=(source,),
        runtime_identity={"model": "model-a", "thinking_enabled": False},
    )
    second = _runtime_version_hash(
        paths=(source,),
        runtime_identity={"model": "model-b", "thinking_enabled": False},
    )

    assert first != second


def _blind_pack_payload() -> dict:
    return {
        "schema_version": "agent2.runtime_blind_input.v1",
        "pack_id": "blind-pack-test",
        "cases": [
            {
                "case_id": "opaque-case-1",
                "actor_id": "69ceef64-4218-59fa-9c74-3182bccdd710",
                "conversation_id": "opaque-conversation-1",
                "initial_state": None,
                "initial_daily_snapshot": {
                    "report_id": "899f82c1-712a-536e-b9b4-9940b21c406d",
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
                        "turn_id": "opaque-turn-1",
                        "raw_text": "今天完成合同审核",
                        "occurred_at": datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc).isoformat(),
                        "channel": "blind_replay",
                        "request_metadata": {"source": "blind_test"},
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("cases", 0, "expected"), {"write_intent": True}),
        (("cases", 0, "runtime_config", "baseline"), {"direct_write": True}),
        (("cases", 0, "turns", 0, "request_metadata", "gold"), "daily_append"),
        (("cases", 0, "initial_state", "reviewer_conclusion"), "approved"),
    ],
)
def test_blind_input_pack_recursively_rejects_oracle_fields(path, value):
    payload = deepcopy(_blind_pack_payload())
    current = payload
    for part in path[:-1]:
        nested = current[part]
        if nested is None:
            nested = {}
            current[part] = nested
        current = nested
    current[path[-1]] = value

    with pytest.raises(ValueError, match="forbidden oracle field"):
        BlindInputPack.from_mapping(payload)


def test_blind_input_pack_deep_freezes_nested_json_and_returns_detached_mappings():
    payload = _blind_pack_payload()
    payload["cases"][0]["runtime_config"]["daily_policy"]["nested"] = {
        "categories": ["daily"]
    }
    pack = BlindInputPack.from_mapping(payload)
    before = pack.as_mapping()

    payload["cases"][0]["runtime_config"]["daily_policy"]["nested"]["categories"].append(
        "poison"
    )
    detached = pack.as_mapping()
    detached["cases"][0]["runtime_config"]["daily_policy"]["nested"]["categories"].append(
        "detached-poison"
    )

    assert pack.as_mapping() == before
    with pytest.raises(TypeError):
        pack.cases[0].runtime_config["daily_policy"]["nested"]["new"] = True
    with pytest.raises(AttributeError):
        pack.cases[0].runtime_config["daily_policy"]["nested"]["categories"].append(
            "frozen-poison"
        )


def test_completed_actual_artifact_deep_freezes_cases_before_hashing():
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    mutable_cases = (
        {
            "case_id": "opaque-case-1",
            "turns": [{"turn_id": "opaque-turn-1", "trace": [{"stage": "started"}]}],
        },
    )
    artifact = BlindActualArtifact.completed_artifact(
        input_pack=pack,
        runtime_version_hash="runtime-test-v1",
        cases=mutable_cases,
    )
    before = artifact.as_mapping()

    mutable_cases[0]["turns"][0]["trace"][0]["stage"] = "poisoned"
    detached = artifact.as_mapping()
    detached["cases"][0]["turns"][0]["trace"][0]["stage"] = "detached-poison"

    assert artifact.as_mapping() == before
    assert BlindActualArtifact.from_mapping(before).artifact_hash == artifact.artifact_hash
    with pytest.raises(TypeError):
        artifact.cases[0]["turns"][0]["trace"][0]["stage"] = "frozen-poison"


def test_sealed_label_store_deep_freezes_nested_labels_before_hashing():
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    mutable_label = {
        "case_id": "opaque-case-1",
        "turn_id": "opaque-turn-1",
        "expected_entities": [{"entity_type": "daily_event"}],
        "provenance": {"reviewers": ["machine"]},
        "independent_review_status": "pending",
    }
    labels = SealedLabelStore.seal(input_pack=pack, labels=[mutable_label])
    before = labels.as_mapping()

    mutable_label["expected_entities"][0]["entity_type"] = "poisoned"
    mutable_label["provenance"]["reviewers"].append("poisoned")
    detached = labels.as_mapping()
    detached["labels"][0]["provenance"]["reviewers"].append("detached-poison")

    assert labels.as_mapping() == before
    assert SealedLabelStore.from_mapping(before).seal_hash == labels.seal_hash
    with pytest.raises(TypeError):
        labels.labels[0]["expected_entities"][0]["entity_type"] = "frozen-poison"


def test_blind_runner_revalidates_direct_constructor_pack_before_execution():
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    poisoned_case = replace(
        pack.cases[0],
        runtime_config={"daily_policy": {"expected_action_class": ["capture_daily_event"]}},
    )
    poisoned_pack = replace(pack, cases=(poisoned_case,), digest="forged-digest")

    class ModelMustNotRun:
        async def interpret(self, turn, state):
            raise AssertionError("forged Blind pack must fail before the semantic boundary")

    with pytest.raises(ValueError, match="forbidden oracle field"):
        asyncio.run(BlindSemanticRunner(ModelMustNotRun()).run(poisoned_pack))


def test_scorer_revalidates_direct_constructor_sealed_labels():
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    actual = BlindActualArtifact.completed_artifact(
        input_pack=pack,
        runtime_version_hash="runtime-test-v1",
        cases=(),
    )
    forged = SealedLabelStore(
        input_pack_id=pack.pack_id,
        input_pack_digest=pack.digest,
        labels=(
            {
                "case_id": "opaque-case-1",
                "turn_id": "opaque-turn-1",
                "expected_write_intent": False,
                "independent_review_status": "human_approved",
            },
        ),
        seal_hash="forged-human-approval",
    )

    with pytest.raises(ValueError, match="sealed label store hash is invalid"):
        score_actual_artifact(actual, forged)


def test_runtime_semantic_interpreter_rejects_oracle_fields_before_calling_model():
    class ModelMustNotRun:
        async def complete_json(self, **kwargs):
            raise AssertionError("oracle-bearing input must be rejected before the model boundary")

    turn = CognitiveTurn(
        user_id="blind-user",
        conversation_id="blind-conversation",
        message_id="blind-message",
        text="今天完成合同审核",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        resources={"daily_policy": {"expected_write_intent": True}},
    )

    with pytest.raises(ValueError, match="forbidden oracle field"):
        import asyncio

        asyncio.run(
            LLMCognitiveSemanticInterpreter(ModelMustNotRun()).interpret(
                turn,
                ConversationState.empty(
                    user_id=turn.user_id,
                    conversation_id=turn.conversation_id,
                ),
            )
        )


def test_blind_runner_emits_completed_actual_artifact_without_labels_or_raw_text():
    class IndependentSemanticInterpreter:
        async def interpret(self, turn, state):
            from app.agent2.cognitive_core_v3 import SemanticInterpretation

            return SemanticInterpretation.from_payload(
                {
                    "intents": ["daily_append"],
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "text": "今天完成合同审核",
                            "intents": ["daily_append"],
                            "entity_ids": ["daily-event-1"],
                            "action_ids": ["capture-daily-1"],
                        }
                    ],
                    "entities": [
                        {
                            "entity_id": "daily-event-1",
                            "entity_type": "daily_event",
                            "value": "完成合同审核",
                            "confidence": 1.0,
                            "attributes": {"field": "today_work"},
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "capture-daily-1",
                            "action_type": "capture_daily_event",
                            "intent": "daily_append",
                            "entity_ids": ["daily-event-1"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {"current_goal": "daily_append", "remember_turn": True},
                }
            )

    pack = BlindInputPack.from_mapping(_blind_pack_payload())

    import asyncio

    artifact = asyncio.run(BlindSemanticRunner(IndependentSemanticInterpreter()).run(pack))
    payload = artifact.as_mapping()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["completed"] is True
    assert payload["input_pack_digest"] == pack.digest
    assert payload["artifact_hash"]
    assert len(payload["cases"]) == 1
    actual = payload["cases"][0]["turns"][0]
    assert actual["status"] == "completed"
    assert actual["segments"][0]["segment_id"] == "segment-1"
    assert actual["segments"][0]["text"] == "今天完成合同审核"
    assert actual["segments"][0]["text_hash"] == hashlib.sha256(
        "今天完成合同审核".encode("utf-8")
    ).hexdigest()
    assert actual["segments"][0]["action_ids"] == ["capture-daily-1"]
    assert actual["write_intent"] is True
    assert [command["command_type"] for command in actual["typed_commands"]] == [
        "append_item"
    ]
    assert actual["actual_write"] is False
    assert actual["would_write"] is True
    assert '"raw_text"' not in serialized
    assert '"expected"' not in serialized
    assert '"baseline"' not in serialized


def test_replacing_sealed_labels_changes_only_score_not_runtime_actual():
    class DailyInterpreter:
        async def interpret(self, turn, state):
            from app.agent2.cognitive_core_v3 import SemanticInterpretation

            return SemanticInterpretation.from_payload(
                {
                    "intents": ["daily_append"],
                    "entities": [
                        {
                            "entity_id": "daily-event-1",
                            "entity_type": "daily_event",
                            "value": "完成合同审核",
                            "confidence": 1.0,
                            "attributes": {"field": "today_work"},
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "capture-daily-1",
                            "action_type": "capture_daily_event",
                            "intent": "daily_append",
                            "entity_ids": ["daily-event-1"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {"current_goal": "daily_append"},
                }
            )

    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    import asyncio

    actual = asyncio.run(BlindSemanticRunner(DailyInterpreter()).run(pack))
    positive = SealedLabelStore.seal(
        input_pack=pack,
        labels=[
            {
                "case_id": "opaque-case-1",
                "turn_id": "opaque-turn-1",
                "expected_write_intent": True,
                "expected_action_class": ["capture_daily_event"],
                "independent_review_status": "pending",
            }
        ],
    )
    poisoned = SealedLabelStore.seal(
        input_pack=pack,
        labels=[
            {
                "case_id": "opaque-case-1",
                "turn_id": "opaque-turn-1",
                "expected_write_intent": False,
                "expected_action_class": [],
                "independent_review_status": "pending",
            }
        ],
    )

    positive_score = score_actual_artifact(actual, positive)
    poisoned_score = score_actual_artifact(actual, poisoned)

    assert actual.artifact_hash == actual.as_mapping()["artifact_hash"]
    assert positive_score["mismatch_count"] == 0
    assert poisoned_score["mismatch_count"] == 1
    assert positive_score["actual_artifact_hash"] == poisoned_score["actual_artifact_hash"]
    assert positive_score["actual_artifact_hash"] == actual.artifact_hash


def test_expected_label_replacement_cannot_change_built_blind_input_pack():
    original = RuntimeReplayCase(
        dialogue_id="source-dialogue",
        turns=(
            RuntimeReplayTurn(
                turn_id="t1",
                text="今天完成合同审核",
                expected={"agent2_direct_write": True, "expected_commands": ["fill"]},
            ),
        ),
    )
    poisoned = RuntimeReplayCase(
        dialogue_id="source-dialogue",
        turns=(
            RuntimeReplayTurn(
                turn_id="t1",
                text="今天完成合同审核",
                expected={"agent2_direct_write": False, "expected_commands": []},
            ),
        ),
    )

    original_pack, original_labels = build_blind_pack([original], pack_id="anti-oracle")
    poisoned_pack, poisoned_labels = build_blind_pack([poisoned], pack_id="anti-oracle")

    assert original_pack.digest == poisoned_pack.digest
    assert original_pack.as_mapping() == poisoned_pack.as_mapping()
    assert original_labels.seal_hash != poisoned_labels.seal_hash


def test_sealed_label_builder_preserves_legacy_write_impact_as_candidate_write_intent():
    case = RuntimeReplayCase(
        dialogue_id="legacy-impact-dialogue",
        turns=(
            RuntimeReplayTurn(
                turn_id="merge",
                text="合并第一条和第二条",
                expected={
                    "primary_workflow": "daily_report",
                    "expected_commands": ["merge"],
                    "legacy_write_impact": True,
                },
            ),
        ),
    )

    _, labels = build_blind_pack([case], pack_id="legacy-impact")

    assert labels.labels[0]["expected_write_intent"] is True


def test_internally_inconsistent_machine_action_label_is_not_scored_as_gold():
    case = RuntimeReplayCase(
        dialogue_id="stale-travel-label",
        turns=(
            RuntimeReplayTurn(
                turn_id="travel",
                text="明天去南京出差",
                expected={
                    "primary_workflow": "travel_coordination",
                    "expected_commands": ["fill"],
                    "legacy_write_impact": False,
                },
            ),
        ),
    )
    pack, labels = build_blind_pack([case], pack_id="stale-action-candidate")
    actual = BlindActualArtifact.completed_artifact(
        input_pack=pack,
        runtime_version_hash="runtime-test-v1",
        cases=(
            {
                "case_id": pack.cases[0].case_id,
                "turns": [
                    {
                        "turn_id": pack.cases[0].turns[0].turn_id,
                        "write_intent": False,
                        "clarification_requirement": False,
                        "action_class": ["record_travel_event"],
                        "typed_commands": [
                            {"command_type": "record_travel_candidate"}
                        ],
                    }
                ],
            },
        ),
    )

    score = score_actual_artifact(actual, labels)

    assert labels.labels[0]["provenance"]["action_coverage"] == "inconsistent"
    assert score["mismatch_count"] == 0
    assert score["scored_turns"][0]["excluded_fields"] == [
        "expected_action_class",
        "expected_command_type",
    ]


def test_ledger_report_never_grants_closure_to_pending_machine_label():
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    actual = BlindActualArtifact.completed_artifact(
        input_pack=pack,
        runtime_version_hash="runtime-test-v1",
        cases=(
            {
                "case_id": "opaque-case-1",
                "turns": [
                    {
                        "turn_id": "opaque-turn-1",
                        "input_hash": "a" * 64,
                        "write_intent": False,
                        "actual_write": False,
                        "legacy_fallback_used": False,
                        "clarification_requirement": False,
                        "action_class": ["record_case_progress"],
                        "typed_commands": [],
                        "planning_blocks": [],
                        "status": "blocked",
                        "failed_stage": None,
                        "error_code": None,
                    }
                ],
            },
        ),
    )
    labels = SealedLabelStore.seal(
        input_pack=pack,
        labels=[
            {
                "case_id": "opaque-case-1",
                "turn_id": "opaque-turn-1",
                "expected_write_intent": False,
                "expected_clarification_requirement": False,
                "expected_action_class": ["record_case_progress"],
                "expected_command_type": [],
                "provenance": {"action_coverage": "machine_candidate"},
                "independent_review_status": "pending",
                "adjudication": {
                    "historical_anomaly_kind": "unexpected_write_intent",
                    "historical_risk_level": "high",
                    "historical_root_cause_category": "legacy_router",
                    "present_in_before_replay": True,
                },
            }
        ],
    )

    report = build_ledger_closure_report(pack, actual, labels)

    assert report["summary"]["original_42_29"]["unexpected_write_intent"] == {
        "total": 1,
        "machine_candidate_resolved": 1,
        "open": 0,
    }
    assert report["cases"][0]["closure_eligibility"] is False
    assert report["cases"][0]["local_status"] == (
        "machine_candidate_pass_pending_independent_review"
    )


@pytest.mark.parametrize(
    ("planning_blocks", "domain_results", "disposition"),
    [
        (
            [{"action_id": "merge", "reason_code": "target_not_found", "detail": ""}],
            [],
            "legal_resource_missing",
        ),
        (
            [
                {
                    "action_id": "copy",
                    "reason_code": "unsupported_phase1_copy_previous",
                    "detail": "",
                }
            ],
            [],
            "phase1_contract_unsupported",
        ),
        (
            [],
            [
                {
                    "domain_id": "daily",
                    "command_results": [{"reason": "invalid_report_state"}],
                }
            ],
            "legal_state_disallows_mutation",
        ),
    ],
)
def test_ledger_separates_resource_inconsistent_machine_candidates_from_runtime_misses(
    planning_blocks,
    domain_results,
    disposition,
):
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    actual = BlindActualArtifact.completed_artifact(
        input_pack=pack,
        runtime_version_hash="runtime-test-v1",
        cases=(
            {
                "case_id": "opaque-case-1",
                "turns": [
                    {
                        "turn_id": "opaque-turn-1",
                        "input_hash": "a" * 64,
                        "write_intent": False,
                        "actual_write": False,
                        "would_write": False,
                        "legacy_fallback_used": False,
                        "clarification_requirement": False,
                        "action_class": ["merge_daily_items"],
                        "typed_commands": [],
                        "planning_blocks": planning_blocks,
                        "domain_results": domain_results,
                        "status": "blocked",
                        "failed_stage": None,
                        "error_code": None,
                    }
                ],
            },
        ),
    )
    labels = SealedLabelStore.seal(
        input_pack=pack,
        labels=[
            {
                "case_id": "opaque-case-1",
                "turn_id": "opaque-turn-1",
                "expected_write_intent": True,
                "expected_clarification_requirement": False,
                "provenance": {"action_coverage": "coarse_legacy_command"},
                "independent_review_status": "pending",
                "adjudication": {
                    "historical_anomaly_kind": "expected_write_not_executed",
                    "present_in_before_replay": True,
                },
            }
        ],
    )

    report = build_ledger_closure_report(pack, actual, labels)
    row = report["cases"][0]

    assert row["candidate_disposition"] == disposition
    assert row["local_status"] == (
        "machine_candidate_non_executable_pending_independent_review"
    )
    assert row["closure_eligibility"] is False
    assert report["summary"]["original_42_29"]["expected_write_not_executed"] == {
        "total": 1,
        "machine_candidate_resolved": 0,
        "machine_candidate_non_executable": 1,
        "open": 0,
    }


def test_semantic_interpreter_repairs_entity_attributes_before_runtime_validation():
    class SequenceClient:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **kwargs):
            self.calls += 1
            payload = {
                "intents": ["travel_event"],
                "segments": [
                    {
                        "segment_id": "travel-segment",
                        "text": "明天去南京出差",
                        "intents": ["travel_event"],
                        "entity_ids": ["travel-1"],
                        "action_ids": ["record-travel-1"],
                    }
                ],
                "entities": [
                    {
                        "entity_id": "travel-1",
                        "entity_type": "travel_event",
                        "value": "明天去南京出差",
                        "confidence": 1.0,
                        "attributes": (
                            {"destination": "南京", "date": "2026-07-11"}
                            if self.calls == 1
                            else {"destination": "南京", "date_hint": "tomorrow"}
                        ),
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "record-travel-1",
                        "action_type": "record_travel_event",
                        "intent": "travel_event",
                        "entity_ids": ["travel-1"],
                    }
                ],
                "clarification_need": None,
                "context_update": {"current_goal": "travel_event"},
            }
            return json.dumps(payload, ensure_ascii=False)

    client = SequenceClient()
    turn = CognitiveTurn(
        user_id="blind-user",
        conversation_id="blind-conversation",
        message_id="blind-travel-message",
        text="明天去南京出差",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
    )
    import asyncio

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            turn,
            ConversationState.empty(user_id=turn.user_id, conversation_id=turn.conversation_id),
        )
    )

    assert client.calls == 2
    assert interpretation.entities[0].attributes == {
        "destination": "南京",
        "date_hint": "tomorrow",
        "purpose": "出差",
        "statement_mode": "asserted",
        "traveler_scope": "self",
        "evidence_spans": [[0, len(turn.text)]],
    }


def test_semantic_interpreter_repairs_null_daily_report_version_before_runtime_validation():
    class SequenceClient:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **kwargs):
            self.calls += 1
            return json.dumps(
                {
                    "intents": ["daily_copy_previous"],
                    "segments": [
                        {
                            "segment_id": "copy-segment",
                            "text": "复制昨天的日报",
                            "intents": ["daily_copy_previous"],
                            "entity_ids": ["previous-report"],
                            "action_ids": ["copy-previous"],
                        }
                    ],
                    "entities": [
                        {
                            "entity_id": "previous-report",
                            "entity_type": "daily_report",
                            "value": "昨天的日报",
                            "confidence": 1.0,
                            "attributes": (
                                {"report_id": None, "version": None}
                                if self.calls == 1
                                else {}
                            ),
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "copy-previous",
                            "action_type": "copy_previous_daily_report",
                            "intent": "daily_copy_previous",
                            "entity_ids": ["previous-report"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {"current_goal": "daily_copy_previous"},
                },
                ensure_ascii=False,
            )

    client = SequenceClient()
    turn = CognitiveTurn(
        user_id="blind-user",
        conversation_id="blind-conversation",
        message_id="blind-copy-message",
        text="复制昨天的日报",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
    )
    import asyncio

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            turn,
            ConversationState.empty(user_id=turn.user_id, conversation_id=turn.conversation_id),
        )
    )

    assert client.calls == 2
    assert interpretation.entities[0].attributes == {}
    assert interpretation.required_actions[0].action_type == "copy_previous_daily_report"


def test_semantic_interpreter_rejects_oversized_input_before_model_call():
    class NoCallClient:
        calls = 0

        async def complete_json(self, **kwargs):
            self.calls += 1
            raise AssertionError("oversized input must not reach the model")

    client = NoCallClient()
    interpreter = LLMCognitiveSemanticInterpreter(client, model="test-model")
    turn = CognitiveTurn(
        user_id="oversized-user",
        conversation_id="oversized-conversation",
        message_id="oversized-message",
        text="超" * 2001,
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(SemanticInputLimitExceeded):
        import asyncio

        asyncio.run(
            interpreter.interpret(
                turn,
                ConversationState.empty(
                    user_id="oversized-user",
                    conversation_id="oversized-conversation",
                ),
            )
        )

    assert client.calls == 0


def test_closed_semantic_contract_accepts_cognition_only_pending_continuation():
    interpretation = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_clear"],
            "segments": [
                {
                    "segment_id": "confirm-segment",
                    "text": "确认",
                    "intents": ["daily_clear"],
                    "entity_ids": ["report-entity"],
                    "action_ids": ["continue-clear"],
                }
            ],
            "entities": [
                {
                    "entity_id": "report-entity",
                    "entity_type": "daily_report",
                    "value": "当前日报",
                    "confidence": 1.0,
                    "attributes": {"version": 2},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "continue-clear",
                    "action_type": "continue_pending",
                    "intent": "daily_clear",
                    "entity_ids": ["report-entity"],
                    "parameters": {
                        "pending_id": "pending-1",
                        "bound_action": "clear_daily_report",
                    },
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )

    validate_semantic_interpretation_contract(interpretation)


def test_non_unique_short_confirmation_is_sanitized_before_contract_validation():
    class DirectClearClient:
        calls = 0

        async def complete_json(self, **kwargs):
            self.calls += 1
            return json.dumps(
                {
                    "intents": ["daily_clear"],
                    "segments": [
                        {
                            "segment_id": "confirm-segment",
                            "text": "确认",
                            "intents": ["daily_clear"],
                            "entity_ids": ["report-entity"],
                            "action_ids": ["clear-direct"],
                        }
                    ],
                    "entities": [
                        {
                            "entity_id": "report-entity",
                            "entity_type": "daily_report",
                            "value": "当前日报",
                            "confidence": 1.0,
                            "attributes": {"version": 2},
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "clear-direct",
                            "action_type": "clear_daily_report",
                            "intent": "daily_clear",
                            "entity_ids": ["report-entity"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {},
                },
                ensure_ascii=False,
            )

    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    entity = ConversationEntity("report-entity", "daily_report", "当前日报", 1.0)
    state = ConversationState(
        user_id="pending-user",
        conversation_id="pending-conversation",
        current_entities=(entity,),
        pending=tuple(
            BoundPending(
                pending_id=f"pending-{index}",
                user_id="pending-user",
                conversation_id="pending-conversation",
                intent="daily_clear",
                action="clear_daily_report",
                entity_ids=(entity.entity_id,),
                context_id=f"context-{index}",
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=10),
            )
            for index in (1, 2)
        ),
    )
    client = DirectClearClient()
    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            CognitiveTurn(
                user_id=state.user_id,
                conversation_id=state.conversation_id,
                message_id="pending-confirmation",
                text="确认",
                occurred_at=now,
            ),
            state,
        )
    )

    assert client.calls == 1
    assert interpretation.required_actions == ()
    assert interpretation.segments[0].action_ids == ()
    assert interpretation.clarification_need is not None
    assert interpretation.clarification_need.reason == "pending_binding_mismatch"


def test_actual_artifact_store_publishes_only_a_completion_hashed_artifact(tmp_path):
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    artifact = BlindActualArtifact.completed_artifact(
        input_pack=pack,
        runtime_version_hash="runtime-test-v1",
        cases=(),
    )
    target = tmp_path / "actual.json"
    store = FileActualArtifactStore(target)

    store.publish(artifact)
    loaded = store.load_completed()

    assert loaded.artifact_hash == artifact.artifact_hash
    assert loaded.input_pack_digest == pack.digest
    assert target.exists()
    assert not (tmp_path / "actual.json.tmp").exists()


def test_reviewer_packet_exposes_machine_candidate_without_faking_human_review():
    pack = BlindInputPack.from_mapping(_blind_pack_payload())
    labels = SealedLabelStore.seal(
        input_pack=pack,
        labels=[
            {
                "case_id": "opaque-case-1",
                "turn_id": "opaque-turn-1",
                "expected_write_intent": True,
                "expected_action_class": ["capture_daily_event"],
                "risk_annotation": "critical_false_positive",
                "provenance": {"annotation_source": "machine_proposed"},
                "confidence": 0.8,
                "independent_review_status": "pending",
            }
        ],
    )

    packet = build_reviewer_packet(pack, labels)

    assert packet["record_count"] == 1
    record = packet["records"][0]
    assert record["input_text"] == "今天完成合同审核"
    assert record["machine_proposed_label"]["expected_write_intent"] is True
    assert record["independent_review_status"] == "pending"
    assert record["adjudication"] is None
    assert record["disagreement_status"] == "unreviewed"


def test_blind_runtime_routes_internal_knowledge_query_to_contract_receipt():
    class KnowledgeInterpreter:
        async def interpret(self, turn, state):
            from app.agent2.cognitive_core_v3 import SemanticInterpretation

            return SemanticInterpretation.from_payload(
                {
                    "intents": ["internal_query"],
                    "segments": [
                        {
                            "segment_id": "knowledge-segment",
                            "text": "公司印章借用流程是什么？",
                            "intents": ["internal_query"],
                            "entity_ids": ["knowledge-query"],
                            "action_ids": ["search-knowledge"],
                        }
                    ],
                    "entities": [
                        {
                            "entity_id": "knowledge-query",
                            "entity_type": "knowledge_query",
                            "value": "公司印章借用流程",
                            "confidence": 1.0,
                            "attributes": {"query": "公司印章借用流程是什么？"},
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "search-knowledge",
                            "action_type": "search_enterprise_knowledge",
                            "intent": "internal_query",
                            "entity_ids": ["knowledge-query"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {"current_goal": "internal_query"},
                }
            )

    payload = _blind_pack_payload()
    payload["cases"][0]["turns"][0]["raw_text"] = "公司印章借用流程是什么？"
    pack = BlindInputPack.from_mapping(payload)
    import asyncio

    artifact = asyncio.run(BlindSemanticRunner(KnowledgeInterpreter()).run(pack))
    actual = artifact.as_mapping()["cases"][0]["turns"][0]

    assert actual["status"] == "blocked"
    assert actual["error_code"] is None
    assert actual["actual_write"] is False
    assert actual["would_write"] is False
    assert actual["typed_commands"][0]["command_type"] == "search_enterprise_knowledge"
    assert actual["domain_results"][0]["domain_id"] == "knowledge"
    assert actual["domain_results"][0]["status"] == "unavailable"
