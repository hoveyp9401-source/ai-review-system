from __future__ import annotations

import ast
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.daily_command_compiler import apply_legacy_daily_commands_as_typed
from app.agent2.daily_commands import compile_daily_commands
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT, WorkflowRouter


ENTRY_SOURCES = ("dingtalk_stream_text", "dingtalk_webhook_text", "manual_text")


@pytest.mark.parametrize("source", ENTRY_SOURCES)
@pytest.mark.parametrize(
    ("text", "expected_status", "expected_action", "expected_write"),
    (
        ("删除第 2 条", "executed", "delete_item", True),
        ("那条删掉", "blocked", "clarify_target", False),
    ),
)
def test_three_daily_entry_sources_share_typed_command_behavior(source: str, text: str, expected_status: str, expected_action: str, expected_write: bool):
    active_daily = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-active",
        status="collecting",
        reply_candidate=True,
    )
    envelope = IncomingMessageEnvelope(
        sender_id="entry-user",
        sender_name="Entry User",
        dingtalk_user_id="entry-dingtalk-user",
        source=source,
        raw_text=text,
        message_id=f"{source}:stable-message",
        conversation_id="entry-conversation",
        active_tasks=(active_daily,),
    )
    plan = WorkflowRouter().plan(envelope)
    commands = compile_daily_commands(plan, envelope)
    owner_id = uuid5(NAMESPACE_URL, "entry-owner")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "entry-report"),
        owner_user_id=owner_id,
        version=4,
        status="collecting",
        today_work=("alpha", "beta"),
        item_ids={"today_work": ("item-1", "item-2")},
    )

    result = apply_legacy_daily_commands_as_typed(
        commands,
        message_id=envelope.message_id,
        snapshot=snapshot,
        actor_user_id=owner_id,
        expected_report_version=4,
    )

    action = result.executions[0].command.command_type if result.executions else "clarify_target"
    assert result.status == expected_status
    assert action == expected_action
    assert result.should_write_db is expected_write


def test_stream_webhook_and_manual_wiring_pass_stable_id_and_expected_version():
    project_root = Path(__file__).parents[1]
    paths = (
        project_root / "app" / "stream_runner.py",
        project_root / "app" / "api" / "webhook.py",
        project_root / "app" / "api" / "reports.py",
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "execute_agent2_daily_commands"
        ]
        assert calls, f"{path} does not call the shared Agent2 daily orchestrator"
        for call in calls:
            keyword_names = {keyword.arg for keyword in call.keywords}
            assert "message_id" in keyword_names
            assert "expected_report_version" in keyword_names


def test_stream_cognitive_v3_path_calls_typed_executor_without_raw_text():
    path = Path(__file__).parents[1] / "app" / "stream_runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "execute_typed_agent2_daily_commands"
    ]

    assert calls
    for call in calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert {"commands", "execution_context"} <= keyword_names
        assert "raw_input" not in keyword_names
        assert "actions" not in keyword_names


def test_webhook_cognitive_v3_path_calls_typed_executor_without_raw_text():
    path = Path(__file__).parents[1] / "app" / "api" / "webhook.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "execute_typed_agent2_daily_commands"
    ]

    assert calls
    for call in calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert {"commands", "execution_context"} <= keyword_names
        assert "raw_input" not in keyword_names
        assert "actions" not in keyword_names


@pytest.mark.parametrize(
    "relative_path",
    ("app/stream_runner.py", "app/api/webhook.py", "app/api/reports.py"),
)
def test_all_cognitive_v3_entrypoints_call_typed_executor_without_raw_text(relative_path: str):
    path = Path(__file__).parents[1] / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "execute_typed_agent2_daily_commands"
    ]

    assert calls
    for call in calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert {"commands", "execution_context"} <= keyword_names
        assert "raw_input" not in keyword_names
        assert "actions" not in keyword_names


@pytest.mark.parametrize("relative_path", ("app/api/webhook.py", "app/api/reports.py"))
def test_enabled_cognitive_v3_does_not_fall_through_when_llm_client_is_missing(relative_path: str):
    source = (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")

    assert "if cognitive_core_v3_enabled(settings) and llm_client is not None:" not in source


def test_webhook_defines_no_legacy_daily_gate():
    source = (Path(__file__).parents[1] / "app/api/webhook.py").read_text(encoding="utf-8")

    assert "_evaluate_legacy_daily_gate" not in source
    assert "_observe_workflow_route" not in source


@pytest.mark.parametrize("relative_path", ("app/stream_runner.py", "app/api/webhook.py"))
def test_phase2_primary_never_executes_shadow_or_legacy_daily_commands(relative_path: str):
    source = (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")

    assert "phase2_primary and shadow.commands" not in source
    assert "agent2_phase2_dingtalk" not in source


def test_phase2_primary_does_not_invoke_shadow_semantics_or_shadow_reply_generation():
    root = Path(__file__).parents[1]
    webhook_source = (root / "app/api/webhook.py").read_text(encoding="utf-8")
    stream_source = (root / "app/stream_runner.py").read_text(encoding="utf-8")

    assert "shadow = None if phase2_primary else evaluate_daily_shadow" in webhook_source
    assert (
        "envelope, context_pack, daily_report, target_report_date = "
        "await _build_stream_cognitive_context(" in stream_source
    )
    assert "daily_context = await load_live_daily_context(" in stream_source
    assert "daily_context = await load_live_daily_context(" in webhook_source
    assert "if phase2_primary and cognitive_v3 is not None:" in stream_source
    assert "elif shadow is not None:" in stream_source
    assert "build_cognitive_side_reply_v3" in stream_source
    assert "build_cognitive_side_reply_v3" in webhook_source


def test_stream_typed_daily_execution_reuses_the_resolved_source_message_id():
    stream_source = Path("app/stream_runner.py").read_text(encoding="utf-8")
    function_source = stream_source.split(
        "async def _process_stream_agent2_daily_if_enabled", 1
    )[1].split("\nasync def ", 1)[0]

    assert "getattr(incoming" not in function_source
    assert "envelope = replace(envelope, message_id=source_message_id)" in function_source
    assert "turn_runtime_result.daily_execution_context()" in function_source
