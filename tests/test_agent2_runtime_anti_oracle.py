from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent2.evaluation.runtime_scoring import _LABEL_FIELDS
from app.agent2.conversation_state import ConversationEntity, ConversationState
from app.agent2.oracle_guard import assert_no_oracle_fields
from app.agent2.runtime.blind import BlindActualArtifact
from app.agent2.semantic_interpreter_v3 import _conversation_state_oracle_guard_payload


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "app" / "agent2" / "runtime"
BLIND_RUNNER = ROOT / "scripts" / "run_agent2_runtime_blind.py"


def test_runtime_dependency_graph_cannot_import_scorer_or_label_store():
    violations: list[str] = []
    for path in sorted(RUNTIME.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                if module.startswith("app.agent2.evaluation"):
                    violations.append(f"{path.name}: {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.agent2.evaluation"):
                        violations.append(f"{path.name}: {alias.name}")

    assert violations == []


def test_blind_runner_cli_has_no_label_or_scorer_input_interface():
    tree = ast.parse(BLIND_RUNNER.read_text(encoding="utf-8"), filename=str(BLIND_RUNNER))
    imports: list[str] = []
    cli_flags: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(str(node.module or ""))
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "add_argument":
                cli_flags.extend(
                    str(arg.value)
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                )

    assert not any(name.startswith("app.agent2.evaluation") for name in imports)
    assert not any("label" in flag or "score" in flag for flag in cli_flags)


def test_scorer_cannot_accept_an_unfinished_actual_artifact():
    incomplete = {
        "schema_version": "agent2.runtime_actual_artifact.v1",
        "run_id": "unfinished",
        "input_pack_id": "pack",
        "input_pack_digest": "digest",
        "runtime_version_hash": "runtime",
        "completed": False,
        "cases": [],
        "artifact_hash": "not-a-completion-hash",
    }

    with pytest.raises(ValueError, match="not completed"):
        BlindActualArtifact.from_mapping(incomplete)


@pytest.mark.parametrize("field_name", sorted(name for name in _LABEL_FIELDS if name.startswith("expected_")))
def test_every_expected_label_field_is_rejected_from_nested_runtime_config(field_name):
    with pytest.raises(ValueError, match="forbidden oracle field"):
        assert_no_oracle_fields(
            {"daily_policy": {"nested": {field_name: "poison"}}},
            path="runtime_config",
        )


@pytest.mark.parametrize(
    "field_name",
    sorted(
        {
            "risk_annotation",
            "provenance",
            "independent_review_status",
            "adjudication",
            "labels",
            "sealed_label_hash",
        }
    ),
)
def test_review_and_sealed_label_metadata_is_rejected_from_runtime_inputs(field_name):
    with pytest.raises(ValueError, match="forbidden oracle field"):
        assert_no_oracle_fields(
            {"active_tasks": [{"metadata": {field_name: {"candidate": True}}}]},
            path="runtime_config",
        )


@pytest.mark.parametrize("entity_type", ["case_progress_ref", "travel_intent_ref"])
def test_trusted_optimistic_version_in_bound_runtime_entity_is_not_an_oracle(
    entity_type: str,
) -> None:
    state = ConversationState(
        user_id="tenant:user",
        conversation_id="conversation",
        current_entities=(
            ConversationEntity(
                entity_id="bound-entity",
                entity_type=entity_type,
                value="bound runtime entity",
                confidence=1.0,
                attributes={"expected_version": 3},
            ),
        ),
    )

    guarded = _conversation_state_oracle_guard_payload(state)
    assert_no_oracle_fields(guarded, path="conversation_state")
    assert guarded["current_entities"][0]["attributes"] == {}


def test_expected_version_remains_forbidden_in_untrusted_runtime_input() -> None:
    with pytest.raises(ValueError, match="forbidden oracle field"):
        assert_no_oracle_fields(
            {"arbitrary_payload": {"expected_version": 3}},
            path="runtime_config",
        )
