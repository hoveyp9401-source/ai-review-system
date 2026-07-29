from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent2.evaluation.runtime_scoring import _LABEL_FIELDS
from app.agent2.oracle_guard import assert_no_oracle_fields
from app.agent2.runtime.blind import BlindActualArtifact


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "app" / "agent2" / "runtime"


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
