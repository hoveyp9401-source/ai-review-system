from __future__ import annotations

import asyncio
import json

import pytest

from app.agent2.dialogue_replay import DialogueCase
from app.agent2.daily_execution_replay import replay_daily_execution_cases
from app.agent2.runtime.replay import (
    RuntimeReplayCase,
    RuntimeReplayTurn,
    finalize_runtime_input_manifest,
    load_runtime_dialogue_cases,
    replay_runtime_cases,
    summarize_runtime_replay,
)


def test_runtime_replay_emits_case_progress_candidate_without_legacy_daily_write():
    case = RuntimeReplayCase(
        dialogue_id="runtime-replay-case-progress-no-daily",
        turns=(
            RuntimeReplayTurn(
                turn_id="t1",
                text="项目案件调解结案了",
                expected={
                    "primary_workflow": "case_progress",
                    "should_enter_daily": False,
                },
            ),
        ),
    )
    baseline = {
        "dialogue_id": case.dialogue_id,
        "mismatch_count": 0,
        "turns": [
            {
                "turn_id": "t1",
                "text": "项目案件调解结案了",
                "direct_write": True,
                "blocked_by_gate": False,
                "report_before": {"today_work": []},
                "report_after": {"today_work": ["项目案件调解结案"]},
                "expected": {
                    "primary_workflow": "case_progress",
                    "should_enter_daily": False,
                },
            }
        ],
    }

    result = asyncio.run(replay_runtime_cases([case], baselines=[baseline]))[0]["turns"][0]
    candidate = result["candidate"]

    assert candidate["actions"] == ["record_case_progress"]
    assert candidate["daily_commands"] == []
    assert [command["command_type"] for command in candidate["business_commands"]] == [
        "record_case_progress_candidate"
    ]
    assert candidate["planning_blocks"] == []
    assert candidate["decision"]["required_actions"][0]["action_type"] == (
        "record_case_progress"
    )
    assert candidate["planner_output"]["business_commands"] == candidate["business_commands"]
    assert candidate["domain_results"][0]["status"] == "unavailable"
    assert candidate["domain_results"][0]["command_results"][0]["status"] == (
        "unsupported_domain_contract"
    )
    assert candidate["conversation_state"]["current_goal"] is None
    assert candidate["would_write"] is False
    assert candidate["report_after"]["today_work"] == []
    assert not any(diff["field"] == "unexpected_write_intent" for diff in result["diffs"])


def test_runtime_replay_routes_non_daily_travel_to_contract_only_receipt():
    case = RuntimeReplayCase(
        dialogue_id="runtime-replay-travel-no-daily",
        turns=(
            RuntimeReplayTurn(
                turn_id="t1",
                text="明天去南京出差",
                expected={
                    "primary_workflow": "travel_coordination",
                    "should_enter_daily": False,
                },
            ),
        ),
    )
    baseline = {
        "dialogue_id": case.dialogue_id,
        "mismatch_count": 0,
        "turns": [
            {
                "turn_id": "t1",
                "text": "明天去南京出差",
                "direct_write": True,
                "blocked_by_gate": False,
                "report_before": {"tomorrow_plan": []},
                "report_after": {"tomorrow_plan": ["去南京出差"]},
                "expected": {
                    "primary_workflow": "travel_coordination",
                    "should_enter_daily": False,
                },
            }
        ],
    }

    result = asyncio.run(replay_runtime_cases([case], baselines=[baseline]))[0]["turns"][0]
    candidate = result["candidate"]

    assert candidate["actions"] == ["record_travel_event"]
    assert candidate["daily_commands"] == []
    assert [command["command_type"] for command in candidate["business_commands"]] == [
        "record_travel_candidate"
    ]
    assert candidate["domain_results"][0]["status"] == "unavailable"
    assert candidate["domain_results"][0]["command_results"][0]["status"] == (
        "unsupported_domain_contract"
    )
    assert candidate["would_write"] is False
    assert not any(diff["field"] == "unexpected_write_intent" for diff in result["diffs"])


def test_runtime_replay_compiles_supported_merge_delta_to_typed_command():
    before = {
        "today_work": ["合同审核", "函件起草"],
        "problems": [],
        "tomorrow_plan": [],
        "status": "collecting",
    }
    after = {
        **before,
        "today_work": ["合同审核及函件起草"],
    }
    case = RuntimeReplayCase(
        dialogue_id="runtime-replay-merge",
        turns=(
            RuntimeReplayTurn(
                turn_id="t1",
                text="合并第一条和第二条",
                expected={
                    "primary_workflow": "daily_report",
                    "agent2_direct_write": True,
                    "expected_commands": ["merge"],
                    "target_field": "today_work",
                },
            ),
        ),
        metadata={"initial_report": before},
    )
    baseline = {
        "dialogue_id": case.dialogue_id,
        "mismatch_count": 0,
        "turns": [
            {
                "turn_id": "t1",
                "text": "合并第一条和第二条",
                "direct_write": True,
                "blocked_by_gate": False,
                "report_before": before,
                "report_after": after,
                "expected": case.turns[0].expected,
            }
        ],
    }

    result = asyncio.run(replay_runtime_cases([case], baselines=[baseline]))[0]["turns"][0]
    candidate = result["candidate"]

    assert candidate["actions"] == ["merge_daily_items"]
    assert [command["command_type"] for command in candidate["daily_commands"]] == [
        "merge_items"
    ]
    assert candidate["daily_commands"][0]["patch"] == {
        "replacement": "合同审核及函件起草"
    }
    assert len(candidate["daily_commands"][0]["target_item_ids"]) == 2
    assert candidate["domain_results"][0]["command_results"][0]["validation_status"] == (
        "authorized"
    )
    assert candidate["report_after"] == after
    assert not any(diff["field"] == "write_intent" for diff in result["diffs"])


def test_runtime_replay_fails_closed_when_copy_source_snapshot_is_unavailable():
    before = {
        "today_work": ["今天完成合同审核"],
        "problems": [],
        "tomorrow_plan": [],
        "status": "collecting",
    }
    after = {
        **before,
        "today_work": ["昨天完成案件归档"],
    }
    case = RuntimeReplayCase(
        dialogue_id="runtime-replay-copy-previous",
        turns=(
            RuntimeReplayTurn(
                turn_id="t1",
                text="把昨天的带过来",
                expected={
                    "primary_workflow": "daily_report",
                    "agent2_direct_write": True,
                    "expected_commands": ["copy_previous"],
                    "target_field": "all",
                },
            ),
        ),
        metadata={"initial_report": before},
    )
    baseline = {
        "dialogue_id": case.dialogue_id,
        "mismatch_count": 0,
        "turns": [
            {
                "turn_id": "t1",
                "text": "把昨天的带过来",
                "direct_write": True,
                "blocked_by_gate": False,
                "daily_commands": [{"operation": "copy_previous"}],
                "report_before": before,
                "report_after": after,
                "expected": case.turns[0].expected,
            }
        ],
    }

    result = asyncio.run(replay_runtime_cases([case], baselines=[baseline]))[0]["turns"][0]
    candidate = result["candidate"]

    assert candidate["actions"] == ["copy_previous_daily_report"]
    assert candidate["daily_commands"] == []
    assert candidate["planning_blocks"] == [
        {
            "action_id": "replay-copy-previous-daily-report",
            "reason_code": "daily_source_snapshot_required",
            "detail": "",
        }
    ]
    assert candidate["status"] == "blocked"
    assert candidate["would_write"] is False
    assert candidate["report_after"] == before


def test_runtime_replay_compares_isolated_baseline_with_harness_and_enforces_safety_gates():
    case = DialogueCase.from_mapping(
        {
            "dialogue_id": "runtime-replay-1",
            "source": "runtime_replay_test",
            "sender_id": "runtime-replay-user",
            "conversation_id": "runtime-replay-conversation",
            "turns": [
                {
                    "turn_id": "t1",
                    "text": "完成合同审核",
                    "expected": {
                        "primary_workflow": "daily_report",
                        "should_enter_daily": True,
                        "expected_commands": ["fill"],
                        "target_field": "today_work",
                        "agent2_direct_write": True,
                    },
                }
            ],
        },
        fallback_id="runtime-replay-fallback",
    )

    baselines = replay_daily_execution_cases([case])
    results = asyncio.run(replay_runtime_cases([case], baselines=baselines))
    summary = summarize_runtime_replay(results)

    assert summary["total_dialogues"] == 1
    assert summary["total_turns"] == 1
    assert summary["mismatch_count"] == 0
    assert summary["candidate_actual_write_count"] == 0
    assert summary["unexpected_write_intent_count"] == 0
    assert summary["legacy_fallback_count"] == 0
    assert summary["typed_executor_bypass_count"] == 0
    assert summary["diagnostic_execution_invariants_passed"] is True
    assert summary["acceptance_eligible"] is False
    assert summary["safety_ready"] is False
    assert summary["parity_ready"] is False
    assert summary["evaluation_scope"] == "baseline_derived_planner_executor_replay"
    assert summary["cognitive_semantic_independence"] is False
    turn = results[0]["turns"][0]
    assert turn["baseline"]["direct_write"] is True
    assert turn["candidate"]["would_write"] is True
    assert turn["candidate"]["actual_write"] is False
    assert turn["candidate"]["domain_results"][0]["status"] == "simulated"
    assert turn["candidate"]["trace"][-1]["stage"] == "audit_recorded"
    assert turn["diffs"] == []


def test_replay_safety_gate_detects_unexpected_simulated_write_intent():
    command = {"command_id": "command-1", "command_type": "append_item"}
    results = [
        {
            "dialogue_id": "unexpected-write",
            "baseline_mismatch_count": 0,
            "turns": [
                {
                    "baseline": {
                        "direct_write": False,
                        "expected": {"agent2_direct_write": False},
                    },
                    "candidate": {
                        "actual_write": False,
                        "would_write": True,
                        "legacy_fallback_used": False,
                        "daily_commands": [command],
                        "business_commands": [],
                        "domain_results": [
                            {
                                "domain_id": "daily",
                                "command_count": 1,
                                "would_write": True,
                                "command_results": [
                                    {
                                        "typed_command": command,
                                        "validation_status": "authorized",
                                        "simulated": True,
                                    }
                                ],
                            }
                        ],
                    },
                    "diffs": [],
                }
            ],
        }
    ]

    summary = summarize_runtime_replay(results)

    assert summary["candidate_actual_write_count"] == 0
    assert summary["unexpected_write_intent_count"] == 1
    assert summary["unexpected_write_count"] == 1
    assert summary["safety_ready"] is False


def test_replay_safety_gate_detects_planned_command_without_receipt():
    results = [
        {
            "dialogue_id": "dropped-receipt",
            "baseline_mismatch_count": 0,
            "turns": [
                {
                    "baseline": {
                        "direct_write": False,
                        "expected": {"agent2_direct_write": False},
                    },
                    "candidate": {
                        "actual_write": False,
                        "would_write": False,
                        "legacy_fallback_used": False,
                        "daily_commands": [
                            {"command_id": "command-without-receipt", "command_type": "append_item"}
                        ],
                        "business_commands": [],
                        "domain_results": [],
                    },
                    "diffs": [],
                }
            ],
        }
    ]

    summary = summarize_runtime_replay(results)

    assert summary["typed_executor_bypass_count"] == 1
    assert summary["safety_ready"] is False


def test_baseline_derived_replay_keeps_non_independent_scope_when_chat_matches():
    case = DialogueCase.from_mapping(
        {
            "dialogue_id": "runtime-replay-chat",
            "source": "runtime_replay_test",
            "turns": [
                {
                    "turn_id": "chat",
                    "text": "早上好，咖啡有点苦",
                    "expected": {
                        "primary_workflow": "chat",
                        "agent2_direct_write": False,
                        "assistant_reply_type": "chat",
                    },
                }
            ],
        },
        fallback_id="runtime-replay-chat-fallback",
    )

    baselines = replay_daily_execution_cases([case])
    results = asyncio.run(replay_runtime_cases([case], baselines=baselines))
    summary = summarize_runtime_replay(results)
    assert results[0]["turns"][0]["diffs"] == []
    assert summary["diagnostic_execution_invariants_passed"] is True
    assert summary["acceptance_eligible"] is False
    assert summary["parity_ready"] is False
    assert summary["safety_ready"] is False
    assert summary["evaluation_scope"] == "baseline_derived_planner_executor_replay"
    assert summary["cognitive_semantic_independence"] is False


def test_runtime_replay_loader_rejects_unscored_turns(tmp_path):
    path = tmp_path / "unscored.jsonl"
    path.write_text(
        json.dumps(
            {
                "dialogue_id": "unscored",
                "turns": [{"turn_id": "t1", "text": "没有 expected"}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires scored expected assertions"):
        load_runtime_dialogue_cases([path])


def test_runtime_replay_manifest_digest_binds_actual_limited_selection(tmp_path):
    path = tmp_path / "selection.jsonl"
    rows = [
        {
            "dialogue_id": f"dialogue-{index}",
            "turns": [
                {
                    "turn_id": "t1",
                    "text": f"turn-{index}",
                    "expected": {"agent2_direct_write": False},
                }
            ],
        }
        for index in range(2)
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    cases, full_manifest = load_runtime_dialogue_cases([path])
    limited_manifest = finalize_runtime_input_manifest(
        full_manifest,
        cases[:1],
        selection_limit=1,
    )

    assert full_manifest["selection"]["dialogue_count"] == 2
    assert limited_manifest["selection"]["dialogue_count"] == 1
    assert limited_manifest["selection"]["turn_count"] == 1
    assert limited_manifest["selection"]["selection_limit"] == 1
    assert limited_manifest["digest"] != full_manifest["digest"]
