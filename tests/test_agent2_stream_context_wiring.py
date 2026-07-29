import ast
from pathlib import Path


STREAM_RUNNER = Path(__file__).resolve().parents[1] / "app" / "stream_runner.py"


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def test_agent2_stream_reply_helpers_use_keyword_context_pack_wiring():
    tree = ast.parse(STREAM_RUNNER.read_text(encoding="utf-8"))

    blocked_calls = _calls_named(tree, "_agent2_blocked_reply_text")
    side_calls = _calls_named(tree, "_agent2_side_reply_text")

    assert blocked_calls, "expected blocked reply helper to be wired"
    assert side_calls, "expected side reply helper to be wired"
    for call in blocked_calls + side_calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert call.args == []
        assert "shadow" in keyword_names
        assert "context_pack" in keyword_names


def test_agent2_stream_builds_context_pack_from_live_daily_report():
    tree = ast.parse(STREAM_RUNNER.read_text(encoding="utf-8"))

    context_pack_calls = _calls_named(tree, "build_agent2_context_pack")

    assert context_pack_calls, "expected stream shadow evaluation to build Context Pack"
    assert any(
        any(keyword.arg == "daily_report" and isinstance(keyword.value, ast.Name) and keyword.value.id == "daily_report" for keyword in call.keywords)
        for call in context_pack_calls
    )


def test_agent2_stream_wires_personal_memory_into_context_pack():
    tree = ast.parse(STREAM_RUNNER.read_text(encoding="utf-8"))

    context_pack_calls = _calls_named(tree, "build_agent2_context_pack")

    assert any(
        any(keyword.arg == "personal_memory" for keyword in call.keywords)
        for call in context_pack_calls
    )


def test_agent2_stream_wires_live_knowledge_into_context_pack():
    source = STREAM_RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(source)

    context_pack_calls = _calls_named(tree, "build_agent2_context_pack")

    assert "CaseTableRagAdapter" in source
    assert "resolve_knowledge" in source
    assert any(
        any(keyword.arg == "knowledge" for keyword in call.keywords)
        for call in context_pack_calls
    )


def test_agent2_stream_wires_recent_case_messages_into_knowledge_query():
    source = STREAM_RUNNER.read_text(encoding="utf-8")

    assert "load_recent_case_context_messages" in source
    assert "recent_case_messages" in source


def test_agent2_stream_legacy_gate_block_uses_tool_assisted_reply():
    source = STREAM_RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(source)

    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_job"
    )
    function_source = ast.unparse(function)

    assert "_agent2_blocked_reply_text" in function_source
    assert "build_daily_candidate_clarification" in function_source
    assert "gate_decision.reply_text or" not in function_source


def test_agent2_stream_wires_coordination_candidate_feedback_into_replies():
    tree = ast.parse(STREAM_RUNNER.read_text(encoding="utf-8"))

    feedback_calls = _calls_named(tree, "_agent2_candidate_feedback_text")

    assert len(feedback_calls) >= 2


def test_agent2_stream_candidate_feedback_labels_future_weekday_as_known_time():
    tree = ast.parse(STREAM_RUNNER.read_text(encoding="utf-8"))
    target_function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_candidate_date_label"
    )

    constants = {node.value for node in ast.walk(target_function) if isinstance(node, ast.Constant)}

    assert "future_weekday" in constants
    assert "\u672c\u5468\u672a\u6765\u65e5\u671f" in constants
