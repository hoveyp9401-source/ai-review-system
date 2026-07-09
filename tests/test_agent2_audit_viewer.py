from __future__ import annotations

import json

from app.agent2.audit_viewer import load_execution_turns, render_audit_viewer_html


def test_audit_viewer_flattens_replay_jsonl_and_renders_html(tmp_path):
    source = tmp_path / "replay.jsonl"
    source.write_text(
        json.dumps(
            {
                "dialogue_id": "dlg-1",
                "turns": [
                    {
                        "turn_id": "t1",
                        "text": "hello",
                        "primary_workflow": "chat",
                        "execution_status": "read_only",
                        "direct_write": False,
                        "daily_commands": [],
                        "cognitive_decision": {
                            "primary_workflow": "chat",
                            "actions": [
                                {
                                    "workflow": "chat",
                                    "action_type": "small_talk",
                                    "operation": "reply",
                                    "target_field": "none",
                                    "write_policy": "read_only",
                                }
                            ],
                        },
                        "contract_invariant_violations": [],
                    },
                    {
                        "turn_id": "t2",
                        "text": "clear report",
                        "primary_workflow": "daily_report",
                        "execution_status": "agent2_direct_write",
                        "direct_write": True,
                        "daily_commands": [
                            {
                                "operation": "clear",
                                "target_field": "all",
                                "should_write": True,
                                "safety_flags": ["destructive_or_overwrite"],
                            }
                        ],
                        "cognitive_decision": {
                            "primary_workflow": "daily_report",
                            "actions": [
                                {
                                    "workflow": "daily_report",
                                    "action_type": "daily_clear",
                                    "operation": "clear",
                                    "target_field": "all",
                                    "write_policy": "write",
                                }
                            ],
                        },
                        "contract_invariant_violations": [
                            {
                                "rule": "example_rule",
                                "severity": "error",
                                "message": "example",
                            }
                        ],
                        "knowledge_facts": [
                            {
                                "fact_contract": {
                                    "contract_version": "fact_contract.v1",
                                    "metric": {"name": "case_count"},
                                    "value": 3,
                                }
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    turns = load_execution_turns(source)
    output = render_audit_viewer_html(turns, tmp_path / "audit.html")

    html = output.read_text(encoding="utf-8")
    assert len(turns) == 2
    assert "Agent2 Cognitive Audit Viewer" in html
    assert "daily_report" in html
    assert "clear/all/write/destructive_or_overwrite" in html
    assert "example_rule" in html
    assert "fact_contract.v1" in html
