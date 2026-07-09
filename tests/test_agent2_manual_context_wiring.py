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


def test_manual_endpoint_uses_agent2_only_for_explicit_source_or_gray_user():
    source = REPORTS_API.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_manual_should_use_agent2"
    )

    assert "legacy" in ast.unparse(function)
    assert "disable_agent2" in ast.unparse(function)
    function_source = ast.unparse(function)
    assert "agent2" in function_source
    assert "source_text" in function_source
    assert "agent2_daily_enabled_for_user" in function_source
