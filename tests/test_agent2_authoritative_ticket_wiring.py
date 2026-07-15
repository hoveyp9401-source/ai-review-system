from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _uses_trusted_runtime_authority(call: ast.Call) -> bool:
    return any(
        keyword.arg == "execution_authority"
        and isinstance(keyword.value, ast.Attribute)
        and keyword.value.attr == "mutation_execution_authority"
        and isinstance(keyword.value.value, ast.Name)
        and keyword.value.value.id == "turn_runtime_result"
        for keyword in call.keywords
    )


def test_webhook_and_stream_business_executor_share_turn_session_with_artifact_sink():
    """Ticket issuance must be visible without an intermediate commit."""

    for relative_path in ("app/api/webhook.py", "app/stream_runner.py"):
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        business_executors = _calls(tree, "SqlBusinessExecutor")
        runtime_factories = _calls(tree, "production_agent2_turn_runtime")

        assert runtime_factories
        assert business_executors
        assert all(
            call.args
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "session"
            for call in business_executors
        )
        assert all(_uses_trusted_runtime_authority(call) for call in business_executors)
        assert not any(
            isinstance(node, ast.Name) and node.id == "business_session"
            for node in ast.walk(tree)
        )


def test_all_sql_mutation_executors_default_to_authoritative_store_on_their_session():
    expectations = (
        (
            "app/agent2/typed_daily_executor.py",
            "execute_typed_agent2_daily_commands",
            "ticket_store",
        ),
        (
            "app/agent2/report_sql_executor.py",
            "execute_periodic_report_commands",
            "ticket_store",
        ),
    )
    for relative_path, function_name, assignment_name in expectations:
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
        )
        assignments = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == assignment_name
                for target in node.targets
            )
        ]
        assert assignments
        source = ast.unparse(assignments[0].value)
        assert "SqlAdmissionTicketStore(session)" in source

    business_tree = ast.parse(
        (ROOT / "app/agent2/business/sql_executor.py").read_text(encoding="utf-8")
    )
    constructor = next(
        node
        for node in ast.walk(business_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "__init__"
        and any(
            isinstance(child, ast.Name) and child.id == "SqlAdmissionTicketStore"
            for child in ast.walk(node)
        )
    )
    assert "SqlAdmissionTicketStore(session)" in ast.unparse(constructor)


def test_daily_ticket_consumption_occurs_only_after_receipt_flush():
    tree = ast.parse(
        (ROOT / "app/agent2/typed_daily_executor.py").read_text(encoding="utf-8")
    )
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_typed_agent2_daily_commands"
    )
    source = ast.unparse(function)

    assert "await session.flush()" in source
    assert "await ticket_store.consume" in source
    assert source.index("await session.flush()") < source.index(
        "await ticket_store.consume"
    )


def test_semantic_report_entrypoints_select_ticket_authority_explicitly():
    expected_calls = {
        "app/api/webhook.py": {
            "execute_typed_agent2_daily_commands",
            "execute_periodic_report_commands",
        },
        "app/stream_runner.py": {
            "execute_typed_agent2_daily_commands",
            "execute_periodic_report_commands",
        },
        "app/api/reports.py": {"execute_typed_agent2_daily_commands"},
    }
    for relative_path, function_names in expected_calls.items():
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        for function_name in function_names:
            calls = _calls(tree, function_name)
            assert calls
            assert all(_uses_trusted_runtime_authority(call) for call in calls)


def test_report_projection_cannot_open_independent_session_before_source_commit():
    tree = ast.parse(
        (ROOT / "app/agent2/case_report_projection_runtime.py").read_text(
            encoding="utf-8"
        )
    )
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "project_committed_case_followup_facts"
    )
    source = ast.unparse(function)
    assert "await source_session.commit()" in source
    assert source.index("await source_session.commit()") < source.index(
        "async with session_factory()"
    )


def test_mutation_execution_authority_is_required_and_all_app_calls_are_explicit():
    definitions = (
        ("app/agent2/business/sql_executor.py", "__init__", False),
        (
            "app/agent2/typed_daily_executor.py",
            "execute_typed_agent2_daily_commands",
            True,
        ),
        (
            "app/agent2/report_sql_executor.py",
            "execute_periodic_report_commands",
            True,
        ),
    )
    for relative_path, function_name, is_async in definitions:
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        function_type = ast.AsyncFunctionDef if is_async else ast.FunctionDef
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, function_type) and node.name == function_name
        )
        keyword_defaults = dict(
            zip(
                (item.arg for item in function.args.kwonlyargs),
                function.args.kw_defaults,
                strict=True,
            )
        )
        assert "execution_authority" in keyword_defaults
        assert keyword_defaults["execution_authority"] is None

    call_names = {
        "SqlBusinessExecutor",
        "execute_typed_agent2_daily_commands",
        "execute_periodic_report_commands",
    }
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call_name in call_names:
            for call in _calls(tree, call_name):
                assert any(
                    keyword.arg == "execution_authority"
                    for keyword in call.keywords
                ), f"{path.relative_to(ROOT)}:{call.lineno} lacks explicit authority"
