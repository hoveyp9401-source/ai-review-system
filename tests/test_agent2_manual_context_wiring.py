import ast
from pathlib import Path


REPORTS_API = Path(__file__).resolve().parents[1] / "app" / "api" / "reports.py"


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def test_agent2_manual_blocked_reply_uses_tool_assisted_context_pack():
    source = REPORTS_API.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "build_tool_assisted_reply" in source
    assert "build_agent2_context_pack" in source
    assert "CaseTableRagAdapter" in source
    assert "resolve_knowledge" in source
    assert "load_recent_case_context_messages" in source
    assert "recent_case_messages" in source

    tool_calls = _calls_named(tree, "build_tool_assisted_reply")
    assert tool_calls, "manual blocked reply must use the same assistant tool chain as stream"
    assert any(
        {keyword.arg for keyword in call.keywords} >= {"raw_text", "assistant_reply", "llm_client", "context_pack"}
        for call in tool_calls
    )

    context_pack_calls = _calls_named(tree, "build_agent2_context_pack")
    assert any(
        {keyword.arg for keyword in call.keywords} >= {"daily_report", "personal_memory", "knowledge"}
        for call in context_pack_calls
    )


def test_manual_endpoint_ignores_request_source_and_uses_trusted_gray_gate():
    source = REPORTS_API.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_manual_should_use_agent2"
    )

    function_source = ast.unparse(function)
    assert "source_text" not in function_source
    assert "'agent2' in" not in function_source
    assert "'legacy'" not in function_source
    assert "'agent1'" not in function_source
    assert "'disable_agent2'" not in function_source
    assert "agent2_daily_enabled_for_user" in function_source


def test_manual_cognitive_v3_path_executes_only_typed_daily_commands():
    source = REPORTS_API.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = _calls_named(tree, "execute_typed_agent2_daily_commands")

    assert calls, "manual Agent2 v3 path must call the typed executor"
    for call in calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert {"commands", "execution_context"} <= keyword_names
        assert "raw_input" not in keyword_names
        assert "actions" not in keyword_names
