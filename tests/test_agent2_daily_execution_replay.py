import json
from pathlib import Path

from app.agent2.daily_execution_replay import replay_daily_execution_cases, summarize_daily_execution_results
from app.agent2.dialogue_replay import load_dialogue_cases


def _load_one(tmp_path: Path, payload: dict):
    path = tmp_path / "dialogues.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
    return load_dialogue_cases([path])


def test_execution_replay_blocks_agent2_direct_writes_for_active_context_noise(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "agent2-grey-incident",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {
                        "agent2_direct_write": True,
                        "report_today_work": ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"],
                    },
                },
                {
                    "turn_id": "age",
                    "text": "\u4f60\u591a\u5927\u4e86",
                    "expected": {
                        "agent2_direct_write": False,
                        "fallback_to_legacy": False,
                        "report_today_work": ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"],
                        "forbidden_today_work_contains": ["\u4f60\u591a\u5927\u4e86"],
                    },
                },
                {
                    "turn_id": "short-noise",
                    "text": "\u6674\u7a7a",
                    "expected": {
                        "agent2_direct_write": False,
                        "fallback_to_legacy": False,
                        "report_today_work": ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"],
                        "forbidden_today_work_contains": ["\u6674\u7a7a"],
                    },
                },
                {
                    "turn_id": "meta-question",
                    "text": "\u4f60\u90fd\u8bb0\u5f55\u7684\u662f\u5565\u554a",
                    "expected": {
                        "agent2_direct_write": False,
                        "fallback_to_legacy": False,
                        "report_today_work": ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"],
                        "forbidden_today_work_contains": ["\u4f60\u90fd\u8bb0\u5f55\u7684\u662f\u5565\u554a"],
                    },
                },
                {
                    "turn_id": "tomorrow-trip",
                    "text": "\u4f30\u8ba1\u660e\u513f\u8981\u53bb\u5357\u4eac",
                    "expected": {
                        "agent2_direct_write": True,
                        "fallback_to_legacy": False,
                        "report_today_work": ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"],
                        "report_tomorrow_plan": ["\u4f30\u8ba1\u660e\u513f\u8981\u53bb\u5357\u4eac"],
                        "forbidden_today_work_contains": ["\u4f30\u8ba1\u660e\u513f\u8981\u53bb\u5357\u4eac"],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    summary = summarize_daily_execution_results(results)

    assert results[0]["passed"] is True
    assert summary["gray_ready"] is True
    assert summary["direct_write_count"] == 2
    assert summary["raw_text_written_count"] == 1


def test_execution_replay_allows_structural_delete_without_polluting_other_fields(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "structural-delete",
            "metadata": {
                "initial_report": {
                    "today_work": ["\u5408\u540c\u5ba1\u6838", "\u51fd\u4ef6\u8d77\u8349"],
                    "problems": ["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
                    "tomorrow_plan": ["\u7ee7\u7eed\u8ddf\u8fdb"],
                }
            },
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": "daily-edit",
                    "status": "collecting",
                    "reply_candidate": True,
                }
            ],
            "turns": [
                {
                    "turn_id": "delete-first",
                    "text": "\u5220\u6389\u7b2c\u4e00\u6761",
                    "expected": {
                        "agent2_direct_write": True,
                        "report_today_work": ["\u51fd\u4ef6\u8d77\u8349"],
                        "report_problems": ["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
                        "report_tomorrow_plan": ["\u7ee7\u7eed\u8ddf\u8fdb"],
                    },
                }
            ],
        },
    )

    results = replay_daily_execution_cases(cases)

    assert results[0]["passed"] is True
    assert results[0]["final_report"]["today_work"] == ["\u51fd\u4ef6\u8d77\u8349"]


def test_execution_replay_does_not_fail_allowed_edit_when_target_is_missing(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "missing-target-edit",
            "metadata": {"initial_report": {"today_work": ["合同审核"], "problems": [], "tomorrow_plan": []}},
            "turns": [
                {
                    "turn_id": "missing-edit",
                    "text": "项目会议改成果各部门协调会。",
                    "expected": {"agent2_direct_write": True, "fallback_to_legacy": False},
                }
            ],
        },
    )

    results = replay_daily_execution_cases(cases)

    assert results[0]["passed"] is True
    assert results[0]["turns"][0]["execution_status"] == "no_change"


def test_execution_replay_writes_active_context_empty_problem_reply(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "short-problem-fallback",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {"agent2_direct_write": True},
                },
                    {
                        "turn_id": "problem",
                        "text": "\u6682\u65e0\u95ee\u9898",
                        "expected": {
                            "execution_status": "agent2_direct_write",
                            "agent2_direct_write": True,
                            "fallback_to_legacy": False,
                            "report_problems": ["\u6682\u65e0\u95ee\u9898"],
                        },
                    },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    summary = summarize_daily_execution_results(results)

    assert results[0]["passed"] is True
    assert summary["fallback_to_legacy_count"] == 0


def test_execution_replay_uses_saved_last_modified_item_for_recent_reference(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "recent-reference-edit",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "今天完成合同审核",
                    "expected": {
                        "agent2_direct_write": True,
                        "report_today_work": ["今天完成合同审核"],
                    },
                },
                {
                    "turn_id": "rewrite-recent",
                    "text": "把刚才那条改成今天完成合同审核复核",
                    "expected": {
                        "agent2_direct_write": True,
                        "fallback_to_legacy": False,
                        "report_today_work": ["今天完成合同审核复核"],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)

    assert results[0]["passed"] is True
    assert results[0]["turns"][1]["command_actions"][0]["item_indices"] == [1]


def test_execution_replay_treats_polished_daily_wording_as_semantically_equal(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "semantic-polish",
            "turns": [
                {
                    "turn_id": "today",
                    "text": "今天起草律师函",
                    "expected": {
                        "agent2_direct_write": True,
                        "report_today_work": ["今天起草律师函"],
                    },
                },
                {
                    "turn_id": "tomorrow",
                    "text": "明天去南京出差盖章",
                    "expected": {
                        "agent2_direct_write": True,
                        "report_today_work": ["今天起草律师函"],
                        "report_tomorrow_plan": ["明天去南京出差盖章"],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)

    assert results[0]["passed"] is True


def test_execution_replay_blocks_actionless_context_instruction_instead_of_writing_it(tmp_path: Path):
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "actionless-context-instruction",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {"agent2_direct_write": True},
                },
                {
                    "turn_id": "vague-editorial-request",
                    "text": "\u5e2e\u6211\u6574\u5408\u4f18\u5316",
                    "expected": {
                        "agent2_direct_write": False,
                        "blocked_by_gate": True,
                        "forbidden_today_work_contains": ["\u5e2e\u6211\u6574\u5408\u4f18\u5316"],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    blocked_turn = results[0]["turns"][1]

    assert results[0]["passed"] is True
    assert blocked_turn["cognitive_decision"]["allow_write"] is False
    assert [action["action_type"] for action in blocked_turn["cognitive_decision"]["actions"]] == [
        "disambiguation_required"
    ]
    assert blocked_turn["cognitive_decision"]["actions"][0]["safety_flags"] == [
        "ambiguous_daily_edit_target"
    ]
    assert blocked_turn["gate"]["reply_type"] == "clarify"
    assert blocked_turn["contract_invariant_violations"] == []


def test_execution_replay_authorizes_concrete_contextual_personnel_work_before_writing(tmp_path: Path):
    personnel_work = "\u554a\uff0c\u5904\u7406\u4e86\u5173\u4e8e\u79bb\u804c\u4eba\u5458\u7684\u624b\u7eed\u5bf9\u63a5\u5de5\u4f5c"
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "authorized-contextual-personnel-work",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {"agent2_direct_write": True},
                },
                {
                    "turn_id": "personnel-work",
                    "text": personnel_work,
                    "expected": {
                        "agent2_direct_write": True,
                        "fallback_to_legacy": False,
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    written_turn = results[0]["turns"][1]

    assert results[0]["passed"] is True
    assert written_turn["cognitive_decision"]["allow_write"] is True
    assert [action["action_type"] for action in written_turn["cognitive_decision"]["actions"]] == ["daily_write"]
    assert written_turn["contract_invariant_violations"] == []
    assert "\u5904\u7406\u5173\u4e8e\u79bb\u804c\u4eba\u5458\u7684\u624b\u7eed\u5bf9\u63a5\u5de5\u4f5c" in written_turn["report_after"]["today_work"]


def test_execution_replay_keeps_personnel_and_project_questions_read_only_in_active_daily(tmp_path: Path):
    personnel_question = "\u79bb\u804c\u624b\u7eed\u600e\u4e48\u5904\u7406\uff1f"
    project_question = "\u63a8\u52a8\u9879\u76ee\u662f\u4ec0\u4e48\u610f\u601d\uff1f"
    project_negation = "\u6ca1\u6709\u63a8\u52a8\u9879\u76ee"
    personnel_meta = "\u5165\u804c\u6750\u6599\u6a21\u677f"
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "business-object-questions-stay-read-only",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {"agent2_direct_write": True},
                },
                {
                    "turn_id": "personnel-question",
                    "text": personnel_question,
                    "expected": {
                        "agent2_direct_write": False,
                        "forbidden_today_work_contains": [personnel_question],
                    },
                },
                {
                    "turn_id": "project-question",
                    "text": project_question,
                    "expected": {
                        "agent2_direct_write": False,
                        "forbidden_today_work_contains": [project_question],
                    },
                },
                {
                    "turn_id": "project-negation",
                    "text": project_negation,
                    "expected": {
                        "agent2_direct_write": False,
                        "forbidden_today_work_contains": [project_negation],
                    },
                },
                {
                    "turn_id": "personnel-meta",
                    "text": personnel_meta,
                    "expected": {
                        "agent2_direct_write": False,
                        "forbidden_today_work_contains": [personnel_meta],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    summary = summarize_daily_execution_results(results)

    assert results[0]["passed"] is True
    assert summary["gray_ready"] is True
    assert all(turn["contract_invariant_violations"] == [] for turn in results[0]["turns"])


def test_execution_replay_blocks_bare_contextual_correction_without_item_focus(tmp_path: Path):
    correction = "\u662f\u65e5\u5e38\u8fd0\u8425\u5ba1\u6838"
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "contextual-correction-without-item-focus",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u505a\u4e86\u65e5\u5e38\u7528\u8bed\u5ba1\u6838",
                    "expected": {"agent2_direct_write": True},
                },
                {
                    "turn_id": "bare-correction",
                    "text": correction,
                    "expected": {
                        "agent2_direct_write": False,
                        "blocked_by_gate": True,
                        "forbidden_today_work_contains": [correction],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    correction_turn = results[0]["turns"][1]

    assert results[0]["passed"] is True
    assert correction_turn["gate"]["reply_type"] == "clarify"
    assert correction_turn["contract_invariant_violations"] == []


def test_execution_replay_explicit_do_not_write_overrides_contextual_system_failure(tmp_path: Path):
    instruction = (
        "\u4eca\u5929\u8bd5\u4e86\u62a5\u9500\u7cfb\u7edf\uff0c\u8fd8\u662f\u4e0d\u884c\uff0c"
        "\u4e0d\u8981\u5199\u65e5\u62a5\uff0c\u53ea\u5e2e\u6211\u770b\u770b"
    )
    cases = _load_one(
        tmp_path,
        {
            "dialogue_id": "system-failure-explicit-no-write",
            "turns": [
                {
                    "turn_id": "daily",
                    "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {"agent2_direct_write": True},
                },
                {
                    "turn_id": "system-question",
                    "text": instruction,
                    "expected": {
                        "agent2_direct_write": False,
                        "forbidden_problems_contains": ["\u62a5\u9500\u7cfb\u7edf"],
                        "forbidden_today_work_contains": [instruction],
                    },
                },
            ],
        },
    )

    results = replay_daily_execution_cases(cases)
    question_turn = results[0]["turns"][1]

    assert results[0]["passed"] is True
    assert question_turn["cognitive_decision"]["allow_write"] is False
    assert question_turn["contract_invariant_violations"] == []
