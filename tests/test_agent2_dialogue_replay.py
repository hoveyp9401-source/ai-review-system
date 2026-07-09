import json
from pathlib import Path

from app.agent2.dialogue_replay import (
    load_dialogue_cases,
    replay_dialogue_cases,
    summarize_dialogue_results,
    write_dialogue_reports,
)


def test_dialogue_replay_keeps_non_daily_turns_from_active_daily_context(tmp_path: Path):
    dialogue_file = tmp_path / "dialogues.jsonl"
    dialogue_file.write_text(
        json.dumps(
            {
                "dialogue_id": "multi-turn-1",
                "turns": [
                    {
                        "turn_id": "daily",
                        "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                        "expected": {
                            "primary_workflow": "daily_report",
                            "should_enter_daily": True,
                            "expected_commands": ["fill"],
                            "legacy_write_impact": True,
                        },
                    },
                    {
                        "turn_id": "chatter",
                        "text": "\u54c8\u54c8\u5496\u5561\u592a\u82e6\u4e86",
                        "expected": {
                            "primary_workflow": "chat",
                            "should_enter_daily": False,
                            "legacy_write_impact": False,
                            "forbidden_commands": ["fill", "edit"],
                        },
                    },
                    {
                        "turn_id": "weekly",
                        "text": "\u5e2e\u6211\u751f\u6210\u672c\u5468\u5468\u62a5",
                        "expected": {
                            "primary_workflow": "weekly_report",
                            "should_enter_daily": False,
                            "legacy_write_impact": False,
                            "forbidden_commands": ["fill", "edit"],
                        },
                    },
                ],
            },
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )

    results = replay_dialogue_cases(load_dialogue_cases([dialogue_file]))
    turns = results[0]["turns"]

    assert results[0]["passed"] is True
    assert turns[1]["active_tasks_before"][0]["workflow"] == "daily_report"
    assert turns[1]["daily_commands"] == []
    assert turns[2]["primary_workflow"] == "weekly_report"
    assert turns[2]["daily_commands"] == []


def test_dialogue_replay_accepts_daily_fragments_and_confirmation_boundary(tmp_path: Path):
    dialogue_file = tmp_path / "dialogues.jsonl"
    dialogue_file.write_text(
        json.dumps(
            {
                "dialogue_id": "multi-turn-2",
                "turns": [
                    {
                        "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                        "expected": {"expected_commands": ["fill"], "legacy_write_impact": True},
                    },
                    {
                        "text": "\u6682\u65e0\u95ee\u9898",
                        "expected": {
                            "primary_workflow": "daily_report",
                            "expected_commands": ["fill"],
                            "legacy_write_impact": True,
                        },
                    },
                    {
                        "text": "\u6e05\u7a7a\u4eca\u65e5\u65e5\u62a5",
                        "expected": {
                            "primary_workflow": "daily_report",
                            "expected_commands": ["clear"],
                            "legacy_write_impact": True,
                            "adapter_status": "ready",
                        },
                    },
                    {
                        "text": "\u786e\u8ba4\u63d0\u4ea4",
                        "expected": {
                            "primary_workflow": "unknown_or_help",
                            "gate_reply_type": "clarify",
                            "legacy_write_impact": False,
                        },
                    },
                ],
            },
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )

    results = replay_dialogue_cases(load_dialogue_cases([dialogue_file]))
    turns = results[0]["turns"]

    assert results[0]["passed"] is True
    assert turns[1]["daily_commands"][0]["operation"] == "fill"
    assert turns[2]["legacy_adapter"][0]["status"] == "ready"
    assert turns[3]["daily_commands"] == []


def test_dialogue_replay_reports_summary_and_files(tmp_path: Path):
    dialogue_file = tmp_path / "dialogues.jsonl"
    dialogue_file.write_text(
        json.dumps(
            {
                "dialogue_id": "multi-turn-3",
                "turns": [
                    {
                        "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                        "expected": {"primary_workflow": "monthly_report"},
                    }
                ],
            },
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    results = replay_dialogue_cases(load_dialogue_cases([dialogue_file]))
    summary = summarize_dialogue_results(results)
    written = write_dialogue_reports(results, tmp_path / "reports")

    assert summary["mismatch_count"] == 1
    assert written["mismatch_count"] == 1
    assert (tmp_path / "reports" / "dialogue_results.jsonl").exists()
    assert (tmp_path / "reports" / "dialogue_summary.md").exists()
    assert (tmp_path / "reports" / "dialogue_risks.csv").exists()
