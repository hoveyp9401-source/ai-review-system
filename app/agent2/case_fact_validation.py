from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from app.agent2.report_projection_policy import CaseFactExtraction


_PROTECTED_FACT = re.compile(
    r"(?:今天|今日|上午|下午|刚刚|明天|明日|后天|本周|下周|本月|下月|"
    r"\d{1,4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|"
    r"\d+(?:\.\d+)?(?:元|万元|亿元))"
)

_TODAY_ANCHOR = re.compile(r"(?:今天|今日|今早|今晨|上午|中午|下午|今晚|刚刚|刚才)")
_FUTURE_ANCHOR = re.compile(
    r"(?:明天|明日|后天|下周(?:[一二三四五六日天])?|下个月|下月|"
    r"下个工作日|月底前|\d+天后)"
)
_CLAUSE_BOUNDARY = re.compile(r"[，,。；;！？!?\n]")


@dataclass(frozen=True)
class CaseFactValidationResult:
    valid: bool
    reason_codes: tuple[str, ...]
    raw_text_hash: str
    protected_facts: tuple[str, ...]


def _protected(text: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _PROTECTED_FACT.finditer(text))


def _action_clause(raw_text: str, action: str) -> str:
    start = raw_text.find(action)
    if start < 0:
        return ""
    end = start + len(action)
    left = 0
    for match in _CLAUSE_BOUNDARY.finditer(raw_text, 0, start):
        left = match.end()
    right_match = _CLAUSE_BOUNDARY.search(raw_text, end)
    right = right_match.start() if right_match is not None else len(raw_text)
    return raw_text[left:right]


def _grounded_action_scope(raw_text: str, actions: tuple[str, ...]) -> str:
    scopes: set[str] = set()
    for action in actions:
        clause = _action_clause(raw_text, action)
        if not clause:
            return "unknown"
        has_today = bool(_TODAY_ANCHOR.search(clause))
        has_future = bool(_FUTURE_ANCHOR.search(clause))
        if has_today == has_future:
            return "unknown"
        scopes.add("today" if has_today else "future")
    return next(iter(scopes)) if len(scopes) == 1 else "unknown"


def validate_case_fact_extraction(
    fact: CaseFactExtraction,
) -> CaseFactValidationResult:
    digest = sha256(fact.raw_text.encode("utf-8")).hexdigest()
    if not fact.raw_text.strip() or not fact.normalized_fact.strip():
        return CaseFactValidationResult(False, ("empty_fact_text",), digest, ())
    if not fact.case_id or not fact.actor_user_id:
        return CaseFactValidationResult(False, ("missing_bound_identity",), digest, ())
    for start, end in fact.evidence_spans:
        if start < 0 or end <= start or end > len(fact.raw_text):
            return CaseFactValidationResult(
                False, ("invalid_evidence_span",), digest, _protected(fact.raw_text)
            )
    if not fact.evidence_spans:
        return CaseFactValidationResult(
            False, ("evidence_required",), digest, _protected(fact.raw_text)
        )

    raw_facts = _protected(fact.raw_text)
    normalized_facts = _protected(fact.normalized_fact)
    reasons: list[str] = []
    if any(value not in raw_facts for value in normalized_facts):
        reasons.append("ungrounded_protected_fact")
    if any(value not in normalized_facts for value in raw_facts):
        reasons.append("protected_fact_removed")
    evidence_texts = tuple(
        fact.raw_text[start:end] for start, end in fact.evidence_spans
    )
    extracted_facts = tuple(
        value
        for value in (
            *fact.factual_progress,
            *fact.completed_actions,
            *fact.next_actions,
            fact.current_status,
            *fact.time_anchors,
            fact.hearing_readiness,
            *fact.blocking_issues,
        )
        if value.strip()
    )
    if any(
        not any(value in evidence_text for evidence_text in evidence_texts)
        for value in extracted_facts
    ):
        reasons.append("evidence_does_not_cover_extracted_fact")
    grounded_scope = _grounded_action_scope(
        fact.raw_text, fact.completed_actions + fact.next_actions
    )
    if fact.action_time_scope in {"today", "future"} and (
        grounded_scope == "unknown" or fact.action_time_scope != grounded_scope
    ):
        reasons.append("action_time_scope_conflict")
    return CaseFactValidationResult(not reasons, tuple(reasons), digest, raw_facts)
