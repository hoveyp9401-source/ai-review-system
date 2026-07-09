from app.agent2.contract_invariants import evaluate_cognitive_invariants
from app.agent2.cognitive_decision import COGNITIVE_CONTRACT_VERSION
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


def _envelope(raw_text: str, *, active_daily: bool = False) -> IncomingMessageEnvelope:
    tasks = ()
    if active_daily:
        tasks = (
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-1",
                status="collecting",
                reply_candidate=True,
            ),
        )
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        active_tasks=tasks,
    )


def test_cognitive_contract_declares_version_and_raw_text_hash_alias():
    evaluation = evaluate_daily_shadow(_envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"), mode="protective_gate")

    decision = evaluation.cognitive_decision.as_dict()

    assert decision["contract_version"] == COGNITIVE_CONTRACT_VERSION
    assert decision["raw_text_hash"] == decision["source_text_hash"]
    assert decision["raw_text_chars"] == decision["source_text_chars"]


def test_invariants_allow_normal_daily_write_contract():
    evaluation = evaluate_daily_shadow(_envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"), mode="protective_gate")

    violations = evaluate_cognitive_invariants(evaluation.cognitive_decision, actual_write=True)

    assert violations == []


def test_invariants_block_actual_write_when_cognition_disallows_write():
    evaluation = evaluate_daily_shadow(
        _envelope("\u738b\u559c\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11", active_daily=True),
        mode="protective_gate",
    )

    violations = evaluate_cognitive_invariants(evaluation.cognitive_decision, actual_write=True)

    assert [violation.rule for violation in violations] == [
        "actual_write_requires_allow_write",
        "actual_write_requires_write_action",
    ]


def test_invariants_detect_pure_internal_qa_daily_write_command():
    decision = {
        "contract_version": COGNITIVE_CONTRACT_VERSION,
        "primary_workflow": "internal_qa",
        "matched_workflows": ["internal_qa"],
        "allow_write": False,
        "need_confirmation": False,
        "actions": [
            {
                "workflow": "internal_qa",
                "action_type": "internal_qa",
                "write_policy": "read_only",
            }
        ],
        "daily_commands": [
            {
                "operation": "fill",
                "should_write": True,
            }
        ],
    }

    violations = evaluate_cognitive_invariants(decision)

    assert [violation.rule for violation in violations] == [
        "read_only_non_daily_must_not_emit_daily_write_command"
    ]


def test_invariants_detect_allow_write_without_write_action():
    decision = {
        "contract_version": COGNITIVE_CONTRACT_VERSION,
        "primary_workflow": "daily_report",
        "matched_workflows": ["daily_report"],
        "allow_write": True,
        "need_confirmation": False,
        "need_clarification": False,
        "gate_reply_type": "none",
        "actions": [
            {
                "workflow": "daily_report",
                "action_type": "daily_query",
                "operation": "query_current",
                "write_policy": "read_only",
            }
        ],
        "daily_commands": [
            {
                "operation": "query_current",
                "should_write": False,
            }
        ],
    }

    violations = evaluate_cognitive_invariants(decision)

    assert [violation.rule for violation in violations] == ["allow_write_requires_write_action"]


def test_invariants_detect_blocked_gate_with_should_write_command():
    decision = {
        "contract_version": COGNITIVE_CONTRACT_VERSION,
        "primary_workflow": "daily_report",
        "matched_workflows": ["daily_report"],
        "allow_write": False,
        "need_confirmation": True,
        "need_clarification": False,
        "gate_reply_type": "confirm",
        "actions": [
            {
                "workflow": "daily_report",
                "action_type": "daily_fill",
                "operation": "append",
                "write_policy": "write",
            }
        ],
        "daily_commands": [
            {
                "operation": "fill",
                "should_write": True,
                "requires_confirmation": True,
            }
        ],
    }

    violations = evaluate_cognitive_invariants(decision)

    assert [violation.rule for violation in violations] == [
        "gate_block_must_not_emit_should_write_command"
    ]


def test_invariants_detect_destructive_command_without_safety_flag():
    decision = {
        "contract_version": COGNITIVE_CONTRACT_VERSION,
        "primary_workflow": "daily_report",
        "matched_workflows": ["daily_report"],
        "allow_write": True,
        "need_confirmation": False,
        "need_clarification": False,
        "gate_reply_type": "none",
        "actions": [
            {
                "workflow": "daily_report",
                "action_type": "daily_clear",
                "operation": "clear",
                "write_policy": "write",
            }
        ],
        "daily_commands": [
            {
                "operation": "clear",
                "target_field": "all",
                "target_date": "today",
                "should_write": True,
                "safety_flags": [],
            }
        ],
    }

    violations = evaluate_cognitive_invariants(decision)

    assert [violation.rule for violation in violations] == [
        "destructive_command_requires_safety_flag"
    ]


def test_invariants_detect_destructive_actual_write_without_target_scope():
    decision = {
        "contract_version": COGNITIVE_CONTRACT_VERSION,
        "primary_workflow": "daily_report",
        "matched_workflows": ["daily_report"],
        "allow_write": True,
        "need_confirmation": False,
        "need_clarification": False,
        "gate_reply_type": "none",
        "actions": [
            {
                "workflow": "daily_report",
                "action_type": "daily_clear",
                "operation": "clear",
                "write_policy": "write",
            }
        ],
        "daily_commands": [
            {
                "operation": "clear",
                "target_field": "all",
                "target_date": "",
                "task_id": "",
                "should_write": True,
                "safety_flags": ["destructive_or_overwrite"],
            }
        ],
    }

    violations = evaluate_cognitive_invariants(decision, actual_write=True)

    assert [violation.rule for violation in violations] == [
        "destructive_actual_write_requires_target_scope"
    ]
