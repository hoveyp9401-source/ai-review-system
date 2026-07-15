from dataclasses import replace

from app.agent2.case_fact_validation import validate_case_fact_extraction
from app.agent2.report_projection_policy import CaseFactExtraction


def _fact():
    raw = "今天联系法院，法院说下周重新查控，金额是120万元。"
    return CaseFactExtraction(
        case_id="case-1", actor_user_id="user-1", raw_text=raw,
        normalized_fact=raw,
        factual_progress=("法院说下周重新查控",), completed_actions=("联系法院",),
        next_actions=(), action_time_scope="today", report_preference="automatic",
        confidence=0.97, evidence_spans=((0, len(raw)),),
    )


def test_case_fact_keeps_raw_text_and_protected_dates_and_amounts_grounded():
    result = validate_case_fact_extraction(_fact())

    assert result.valid is True
    assert result.raw_text_hash
    assert result.protected_facts == ("今天", "下周", "120万元")


def test_future_plan_cannot_be_validated_as_completed_work_today():
    raw = "明天与对方律师沟通。"
    fact = CaseFactExtraction(
        case_id="case-1",
        actor_user_id="user-1",
        raw_text=raw,
        normalized_fact=raw,
        factual_progress=(),
        completed_actions=("与对方律师沟通",),
        next_actions=(),
        action_time_scope="today",
        report_preference="automatic",
        confidence=0.99,
        evidence_spans=((0, len(raw)),),
    )

    result = validate_case_fact_extraction(fact)

    assert result.valid is False
    assert "action_time_scope_conflict" in result.reason_codes


def test_normalizer_cannot_strengthen_or_change_time_and_amount_facts():
    changed = replace(
        _fact(), normalized_fact="今天联系法院，法院确认明天查控，金额200万元。"
    )

    result = validate_case_fact_extraction(changed)

    assert result.valid is False
    assert "ungrounded_protected_fact" in result.reason_codes
    assert "protected_fact_removed" in result.reason_codes


def test_invalid_evidence_span_fails_closed():
    result = validate_case_fact_extraction(replace(_fact(), evidence_spans=((0, 999),)))

    assert result.valid is False
    assert result.reason_codes == ("invalid_evidence_span",)


def test_evidence_span_must_cover_each_extracted_business_fact():
    raw = "明天与对方律师沟通。"
    fact = CaseFactExtraction(
        case_id="case-1", actor_user_id="user-1", raw_text=raw,
        normalized_fact=raw, factual_progress=(), completed_actions=(),
        next_actions=("与对方律师沟通",), action_time_scope="future",
        report_preference="automatic", confidence=0.98,
        evidence_spans=((0, 2),),
    )

    result = validate_case_fact_extraction(fact)

    assert result.valid is False
    assert "evidence_does_not_cover_extracted_fact" in result.reason_codes
