from app.workflows.problem_evidence import (
    EVIDENCE_DEPENDENCY_BLOCKED,
    EVIDENCE_EXPLICIT_PROBLEM,
    EVIDENCE_NO_PROBLEM,
    EVIDENCE_PROCESS_BLOCKED,
    EVIDENCE_PROGRESS_EXCEPTION,
    EVIDENCE_QUALITY_GAP,
    extract_problem_evidence,
)


def test_problem_evidence_recognizes_explicit_no_problem_answer():
    evidence = extract_problem_evidence("\u6682\u65e0\u95ee\u9898")

    assert evidence.evidence_type == EVIDENCE_NO_PROBLEM
    assert evidence.is_no_problem


def test_problem_evidence_recognizes_explicit_problem_marker():
    evidence = extract_problem_evidence("\u95ee\u9898\u662f\u4f9b\u5e94\u5546\u8d44\u6599\u6ca1\u8865\u5168")

    assert evidence.evidence_type == EVIDENCE_EXPLICIT_PROBLEM
    assert evidence.is_problem


def test_problem_evidence_recognizes_business_dependency_blocked():
    evidence = extract_problem_evidence("\u4e1a\u52a1\u90e8\u95e8\u6750\u6599\u4e00\u76f4\u6ca1\u53cd\u9988")

    assert evidence.evidence_type == EVIDENCE_DEPENDENCY_BLOCKED
    assert evidence.is_business_problem


def test_problem_evidence_recognizes_quality_gap():
    evidence = extract_problem_evidence("\u5ba2\u6237\u8d44\u6599\u4e0d\u5b8c\u6574")

    assert evidence.evidence_type == EVIDENCE_QUALITY_GAP
    assert evidence.is_business_problem


def test_problem_evidence_recognizes_progress_exception():
    evidence = extract_problem_evidence("\u56de\u6b3e\u8282\u70b9\u903e\u671f")

    assert evidence.evidence_type == EVIDENCE_PROGRESS_EXCEPTION
    assert evidence.is_business_problem


def test_problem_evidence_recognizes_process_blocked():
    evidence = extract_problem_evidence("\u5408\u540c\u5ba1\u6279\u6d41\u7a0b\u5361\u4f4f\u4e86")

    assert evidence.evidence_type == EVIDENCE_PROCESS_BLOCKED
    assert evidence.is_business_problem


def test_problem_evidence_ignores_legal_question_even_with_problem_words():
    evidence = extract_problem_evidence("\u7834\u4ea7\u503a\u6743\u7533\u62a5\u903e\u671f\u6709\u4ec0\u4e48\u540e\u679c\uff1f")

    assert not evidence.has_evidence


def test_problem_evidence_ignores_future_resolution_plan():
    evidence = extract_problem_evidence("\u660e\u5929\u5904\u7406\u8d44\u6599\u7f3a\u5931\u95ee\u9898")

    assert not evidence.has_evidence
