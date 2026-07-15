from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    (
        "app/api/webhook.py",
        "app/stream_runner.py",
        "app/api/reports.py",
    ),
)
def test_entrypoints_preserve_typed_selection_block_reason(
    relative_path: str,
) -> None:
    """Ambiguous/expired/forbidden answers require a factual clarification reply."""

    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    typed_handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and node.type is not None
        and "SelectionContinuationBlocked" in ast.unparse(node.type)
    ]

    assert typed_handlers, (
        f"{relative_path} collapses SelectionContinuationBlocked into a generic "
        "semantic failure instead of explaining the zero-write selection state"
    )


def test_manual_selection_success_persists_the_same_operation_outcome_evidence() -> None:
    """Manual must not be the only ingress that drops receipt-backed Outcomes."""

    tree = ast.parse((ROOT / "app/api/reports.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_submit_manual_agent2_if_applicable"
    )
    persistence_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "persist_operation_outcomes"
    ]

    assert persistence_calls, (
        "manual selection/business success returns an Outcome reply but drops the "
        "Outcome audit record that webhook and stream persist"
    )
