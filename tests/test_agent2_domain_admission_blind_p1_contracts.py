from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent2.cognitive_core_v3 import CognitiveTurn, SemanticInterpretation
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.evaluation.semantic_admission_blind import SemanticAdmissionBlindPack


ROOT = Path(__file__).resolve().parents[1]


def _fixed_blind_replay(case_id: str):
    blind_payload = json.loads(
        (ROOT / "evals/agent2/semantic_admission/blind_input.json").read_text(
            encoding="utf-8"
        )
    )
    actual_payload = json.loads(
        (ROOT / "evals/agent2/semantic_admission/actual.json").read_text(
            encoding="utf-8"
        )
    )
    pack = SemanticAdmissionBlindPack.from_mapping(blind_payload)
    blind_case = next(case for case in pack.cases if case.case_id == case_id)
    proposal_payload = next(
        row["proposal"] for row in actual_payload["cases"] if row["case_id"] == case_id
    )
    scope = blind_case.scope
    turn = CognitiveTurn(
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
        actor_user_id=scope.actor_user_id,
        conversation_id=scope.conversation_id,
        message_id=scope.message_id,
        text=blind_case.raw_text,
        occurred_at=scope.occurred_at,
        resources=blind_case.as_mapping()["resources"],
    )
    return DomainAdmissionEngine().admit(
        turn,
        blind_case.conversation_state(),
        SemanticInterpretation.from_payload(proposal_payload),
    )


@pytest.mark.parametrize(
    ("case_id", "expected", "ticket_count", "selection_count"),
    (
        (
            "sa-blind-02-no_other_risk",
            (
                (
                    "case",
                    "answer_case_query",
                    "blocked",
                    "case_query_direct_question_required",
                ),
            ),
            0,
            0,
        ),
        (
            "sa-blind-08-unique_case_number",
            (("case", "record_case_progress", "admitted", ""),),
            1,
            0,
        ),
        (
            "sa-blind-10-travel_asserted",
            (("travel", "record_travel_event", "admitted", ""),),
            1,
            0,
        ),
        (
            "sa-blind-19-daily_and_travel",
            (
                ("report", "capture_daily_event", "admitted", ""),
                ("travel", "record_travel_event", "admitted", ""),
            ),
            2,
            0,
        ),
        (
            "sa-blind-20-daily_and_unique_case",
            (
                ("report", "capture_daily_event", "admitted", ""),
                ("case", "record_case_progress", "admitted", ""),
            ),
            2,
            0,
        ),
        (
            "sa-blind-23-ambiguous_sibling_isolated",
            (
                ("report", "capture_daily_event", "admitted", ""),
                (
                    "case",
                    "record_case_progress",
                    "blocked",
                    "case_reference_ambiguous",
                ),
                ("travel", "record_travel_event", "admitted", ""),
            ),
            2,
            1,
        ),
        (
            "sa-blind-24-risk_noop_plus_travel",
            (
                (
                    "case",
                    "answer_case_query",
                    "blocked",
                    "case_query_direct_question_required",
                ),
                ("travel", "record_travel_event", "admitted", ""),
            ),
            1,
            0,
        ),
        (
            "sa-blind-26-weekly_case_interruption",
            (
                ("report", "capture_report_event", "admitted", ""),
                ("case", "record_case_progress", "admitted", ""),
            ),
            2,
            0,
        ),
    ),
)
def test_fixed_blind_proposals_preserve_explicit_siblings_and_block_only_ambiguity(
    case_id: str,
    expected: tuple[tuple[str, str, str, str], ...],
    ticket_count: int,
    selection_count: int,
) -> None:
    result = _fixed_blind_replay(case_id)

    observed = tuple(
        (
            decision.domain,
            decision.operation,
            decision.status,
            (
                decision.reason_code
                if decision.status == "blocked"
                else ""
            ),
        )
        for decision in result.decisions
    )
    assert observed == expected
    assert len(result.tickets) == ticket_count
    assert len(result.selection_requests) == selection_count
