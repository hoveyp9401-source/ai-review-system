import json
from pathlib import Path

from app.agent2.shadow_replay import (
    load_shadow_replay_records,
    replay_shadow_records,
    summarize_shadow_replay,
    write_shadow_replay_reports,
)


def test_shadow_replay_loads_jsonl_and_sanitizes_results(tmp_path: Path):
    replay_file = tmp_path / "history.jsonl"
    replay_file.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=True)
            for row in [
                {
                    "message_id": "msg-1",
                    "raw_text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                    "expected": {
                        "primary_workflow": "daily_report",
                        "should_enter_daily": True,
                        "legacy_write_impact": True,
                        "adapter_status": "ready",
                    },
                },
                {
                    "message_id": "msg-2",
                    "raw_text": "\u516c\u53f8\u5370\u7ae0\u501f\u7528\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f",
                    "expected": {
                        "should_enter_daily": False,
                        "legacy_write_impact": False,
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    records = load_shadow_replay_records([replay_file])
    results = replay_shadow_records(records)

    assert [record.record_id for record in records] == ["msg-1", "msg-2"]
    assert results[0]["primary_workflow"] == "daily_report"
    assert results[0]["daily_commands"][0]["operation"] == "fill"
    assert results[0]["legacy_adapter"][0]["write_impact"] is True
    assert results[0]["mismatches"] == []
    assert results[1]["daily_commands"] == []
    assert results[1]["mismatches"] == []
    assert "raw_text" not in results[0]
    assert "content" not in results[0]["daily_commands"][0]


def test_shadow_replay_supports_active_task_context_for_read_only_request(tmp_path: Path):
    replay_file = tmp_path / "active.jsonl"
    replay_file.write_text(
        json.dumps(
            {
                "record_id": "active-1",
                "text": "\u53d1\u6211\u770b\u4e0b",
                "context": {
                    "active_tasks": [
                        {
                            "workflow": "daily_report",
                            "task_id": "daily-1",
                            "status": "collecting",
                            "reply_candidate": True,
                        }
                    ]
                },
                "expected": {
                    "primary_workflow": "daily_report",
                    "legacy_write_impact": False,
                    "adapter_status": "ready",
                },
            },
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )

    results = replay_shadow_records(load_shadow_replay_records([replay_file]))

    assert results[0]["daily_commands"][0]["operation"] == "query_current"
    assert results[0]["legacy_adapter"][0]["read_only"] is True
    assert results[0]["mismatches"] == []


def test_shadow_replay_reports_summary_and_mismatches(tmp_path: Path):
    replay_file = tmp_path / "mismatch.jsonl"
    replay_file.write_text(
        json.dumps(
            {
                "record_id": "bad-1",
                "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
                "expected_primary_workflow": "monthly_report",
            },
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    results = replay_shadow_records(load_shadow_replay_records([replay_file]))
    summary = summarize_shadow_replay(results)
    written_summary = write_shadow_replay_reports(results, tmp_path / "reports")

    assert summary["mismatch_count"] == 1
    assert written_summary["mismatch_count"] == 1
    assert (tmp_path / "reports" / "shadow_replay_results.jsonl").exists()
    assert (tmp_path / "reports" / "shadow_replay_summary.md").exists()
    assert (tmp_path / "reports" / "shadow_replay_risks.csv").exists()
