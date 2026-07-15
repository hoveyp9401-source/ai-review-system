from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.cognitive_core_v3 import SemanticInterpretation
from app.agent2.evaluation.semantic_admission_blind import (
    SemanticAdmissionActualArtifact,
    SemanticAdmissionBlindPack,
    SemanticAdmissionBlindRunner,
)
from app.agent2.evaluation.semantic_admission_scoring import (
    SealedSemanticAdmissionLabels,
    score_semantic_admission_actual,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
REPORT_ID = str(uuid5(NAMESPACE_URL, "semantic-admission-blind-report"))


def _case() -> dict[str, object]:
    return {
        "case_id": "blind-old-date-report",
        "scope": {
            "tenant_id": "sandbox-agent2-phase2-20260711",
            "user_id": "sandbox-agent2-phase2-20260711:user-a",
            "actor_user_id": "user-a",
            "conversation_id": "blind-conversation-a",
            "message_id": "blind-message-a",
            "occurred_at": NOW.isoformat(),
            "channel": "blind_replay",
        },
        "raw_text": "补充昨天的日报：联系了法院",
        "state": None,
        "resources": {
            "timezone": "Asia/Shanghai",
            "daily_policy": {"current_report_date": "2026-07-14"},
            "daily_draft": {
                "report_id": REPORT_ID,
                "report_date": "2026-07-14",
                "version": 3,
                "status": "collecting",
                "items": [],
            },
            "daily_reports": [
                {
                    "report_id": REPORT_ID,
                    "report_date": "2026-07-14",
                    "version": 3,
                    "status": "collecting",
                    "items": [],
                }
            ],
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": REPORT_ID,
                    "status": "collecting",
                    "metadata": {"report_date": "2026-07-14"},
                }
            ],
        },
    }


def _pack_payload() -> dict[str, object]:
    return {
        "schema_version": "agent2.semantic_admission_blind_input.v1",
        "pack_id": "semantic-admission-contract-smoke",
        "cases": [_case()],
    }


@pytest.mark.parametrize("oracle_key", ["expected", "baseline", "oracle", "expected_domain"])
def test_blind_pack_recursively_rejects_oracle_fields(oracle_key: str) -> None:
    payload = deepcopy(_pack_payload())
    payload["cases"][0]["resources"]["nested"] = {oracle_key: "case"}  # type: ignore[index]

    with pytest.raises(ValueError, match="oracle"):
        SemanticAdmissionBlindPack.from_mapping(payload)


class _CapturingInterpreter:
    def __init__(self) -> None:
        self.observed: list[tuple[str, str, dict[str, object]]] = []

    async def interpret(self, turn, state):
        self.observed.append((turn.text, state.user_id, dict(turn.resources)))
        return SemanticInterpretation.from_payload(
            {
                "intents": ["daily_append"],
                "segments": [
                    {
                        "segment_id": "daily-segment",
                        "text": turn.text,
                        "intents": ["daily_append"],
                        "entity_ids": ["daily-event"],
                        "action_ids": ["append-daily"],
                        "start_offset": 0,
                        "end_offset": len(turn.text),
                    }
                ],
                "entities": [
                    {
                        "entity_id": "daily-event",
                        "entity_type": "daily_event",
                        "value": turn.text,
                        "confidence": 1.0,
                        "attributes": {"field": "today_work"},
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "append-daily",
                        "action_type": "capture_daily_event",
                        "intent": "daily_append",
                        "entity_ids": ["daily-event"],
                    }
                ],
                "clarification_need": None,
                "context_update": {},
            }
        )

    def runtime_identity(self):
        return {"adapter": "test-capturing-interpreter", "version": "1"}


@pytest.mark.asyncio
async def test_runner_calls_interpreter_then_admission_and_seals_label_free_actual() -> None:
    pack = SemanticAdmissionBlindPack.from_mapping(_pack_payload())
    interpreter = _CapturingInterpreter()

    actual = await SemanticAdmissionBlindRunner(interpreter).run(pack)

    assert interpreter.observed == [
        (
            "补充昨天的日报：联系了法院",
            "sandbox-agent2-phase2-20260711:user-a",
            _case()["resources"],
        )
    ]
    row = actual.cases[0]
    assert row["case_id"] == "blind-old-date-report"
    assert row["decisions"][0]["domain"] == "report"
    assert row["decisions"][0]["verdict"] == "blocked"
    assert row["decisions"][0]["reason_code"] in {
        "historical_daily_mutation_blocked",
        "daily_report_snapshot_not_uniquely_authorized",
    }
    assert row["tickets"] == ()
    assert row["information_pendings"] == ()
    assert row["selection_requests"] == ()
    assert row["evidence_classification"] == "machine_candidate"
    assert row["human_review_state"] == "pending"
    serialized = str(row).lower()
    assert "expected_domain" not in serialized
    assert "baseline" not in serialized
    assert "sealed_label" not in serialized
    assert SemanticAdmissionActualArtifact.from_mapping(actual.as_mapping()) == actual

    tampered = actual.as_mapping()
    tampered["cases"][0]["decisions"] = []
    with pytest.raises(ValueError, match="hash"):
        SemanticAdmissionActualArtifact.from_mapping(tampered)


@pytest.mark.asyncio
async def test_sealed_scorer_is_separate_and_label_changes_cannot_change_actual() -> None:
    pack = SemanticAdmissionBlindPack.from_mapping(_pack_payload())
    actual = await SemanticAdmissionBlindRunner(_CapturingInterpreter()).run(pack)
    actual_before = deepcopy(actual.as_mapping())
    matching_labels = SealedSemanticAdmissionLabels.seal(
        input_pack=pack,
        labels=(
            {
                "case_id": "blind-old-date-report",
                "expected_decisions": [
                    {
                        "domain": "report",
                        "operation": "capture_daily_event",
                        "status": "blocked",
                    }
                ],
                "expected_ticket_count": 0,
                "expected_information_pending_count": 0,
                "expected_selection_request_count": 0,
                "annotation_class": "machine_candidate",
                "review_status": "pending_human_review",
            },
        ),
    )
    conflicting_labels = SealedSemanticAdmissionLabels.seal(
        input_pack=pack,
        labels=(
            {
                "case_id": "blind-old-date-report",
                "expected_decisions": [
                    {
                        "domain": "report",
                        "operation": "capture_daily_event",
                        "status": "admitted",
                    }
                ],
                "expected_ticket_count": 1,
                "expected_information_pending_count": 0,
                "expected_selection_request_count": 0,
                "annotation_class": "machine_candidate",
                "review_status": "pending_human_review",
            },
        ),
    )

    matching_score = score_semantic_admission_actual(actual, matching_labels)
    conflicting_score = score_semantic_admission_actual(actual, conflicting_labels)

    assert matching_score["mismatch_count"] == 0
    assert conflicting_score["mismatch_count"] > 0
    assert matching_score["independent_metrics_available"] is False
    assert matching_score["acceptance_eligible"] is False
    assert actual.as_mapping() == actual_before
    assert actual.artifact_hash == actual_before["artifact_hash"]
    assert matching_labels.seal_hash != conflicting_labels.seal_hash


def test_builder_physically_splits_twenty_plus_adversarial_inputs_from_pending_labels(
    tmp_path: Path,
) -> None:
    output = tmp_path / "semantic-admission"
    subprocess.run(
        [
            sys.executable,
            "scripts/build_agent2_semantic_admission_blind_pack.py",
            "--output-dir",
            str(output),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    blind_payload = json.loads((output / "blind_input.json").read_text(encoding="utf-8"))
    label_payload = json.loads((output / "sealed_labels.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert len(blind_payload["cases"]) >= 20
    assert len(label_payload["labels"]) == len(blind_payload["cases"])
    assert all(manifest["required_coverage"].values())
    assert manifest["human_review_state"] == "pending_human_review"
    assert manifest["acceptance_eligible"] is False
    assert all(
        label["annotation_class"] == "machine_candidate"
        and label["review_status"] == "pending_human_review"
        for label in label_payload["labels"]
    )
    serialized_input = json.dumps(blind_payload, ensure_ascii=False).lower()
    for forbidden in (
        '"expected"',
        '"expected_domain"',
        '"baseline"',
        '"oracle"',
        '"labels"',
        '"seal_hash"',
    ):
        assert forbidden not in serialized_input
    SemanticAdmissionBlindPack.from_mapping(blind_payload)
    SealedSemanticAdmissionLabels.from_mapping(label_payload)

    second = tmp_path / "semantic-admission-second"
    subprocess.run(
        [
            sys.executable,
            "scripts/build_agent2_semantic_admission_blind_pack.py",
            "--output-dir",
            str(second),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    second_blind = json.loads((second / "blind_input.json").read_text(encoding="utf-8"))
    second_labels = json.loads((second / "sealed_labels.json").read_text(encoding="utf-8"))
    assert second_blind["digest"] == blind_payload["digest"]
    assert second_labels["seal_hash"] == label_payload["seal_hash"]


def test_runner_dependency_graph_and_cli_have_no_label_or_scorer_interface() -> None:
    root = Path(__file__).resolve().parents[1]
    runner_module = (root / "app/agent2/evaluation/semantic_admission_blind.py").read_text(
        encoding="utf-8"
    )
    runner_cli = (root / "scripts/run_agent2_semantic_admission_blind.py").read_text(
        encoding="utf-8"
    )

    assert "semantic_admission_scoring" not in runner_module
    assert "SealedSemanticAdmissionLabels" not in runner_module
    assert "score_semantic_admission_actual" not in runner_module
    assert "semantic_admission_scoring" not in runner_cli
    assert "--sealed-labels" not in runner_cli
    help_result = subprocess.run(
        [sys.executable, "scripts/run_agent2_semantic_admission_blind.py", "--help"],
        check=True,
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert "--blind-input" in help_result.stdout
    assert "--actual-output" in help_result.stdout
    assert "--sealed-labels" not in help_result.stdout


@pytest.mark.asyncio
async def test_scorer_rejects_incomplete_actual_and_cross_pack_labels() -> None:
    pack = SemanticAdmissionBlindPack.from_mapping(_pack_payload())
    actual = await SemanticAdmissionBlindRunner(_CapturingInterpreter()).run(pack)
    label = {
        "case_id": "blind-old-date-report",
        "expected_decisions": [
            {
                "domain": "report",
                "operation": "capture_daily_event",
                "status": "admitted",
            }
        ],
        "expected_ticket_count": 1,
        "expected_information_pending_count": 0,
        "expected_selection_request_count": 0,
        "annotation_class": "machine_candidate",
        "review_status": "pending_human_review",
    }
    labels = SealedSemanticAdmissionLabels.seal(input_pack=pack, labels=(label,))

    incomplete = actual.as_mapping()
    incomplete["completed"] = False
    with pytest.raises(ValueError, match="completed"):
        SemanticAdmissionActualArtifact.from_mapping(incomplete)

    other_payload = _pack_payload()
    other_payload["pack_id"] = "other-blind-pack"
    other_pack = SemanticAdmissionBlindPack.from_mapping(other_payload)
    other_labels = SealedSemanticAdmissionLabels.seal(
        input_pack=other_pack, labels=(label,)
    )
    with pytest.raises(ValueError, match="different blind packs"):
        score_semantic_admission_actual(actual, other_labels)
