from __future__ import annotations

from dataclasses import dataclass, field
import re


EVIDENCE_NONE = "none"
EVIDENCE_NO_PROBLEM = "no_problem"
EVIDENCE_EXPLICIT_PROBLEM = "explicit_problem"
EVIDENCE_DEPENDENCY_BLOCKED = "dependency_blocked"
EVIDENCE_QUALITY_GAP = "quality_gap"
EVIDENCE_PROGRESS_EXCEPTION = "progress_exception"
EVIDENCE_PROCESS_BLOCKED = "process_blocked"


@dataclass(frozen=True)
class ProblemEvidence:
    evidence_type: str = EVIDENCE_NONE
    confidence: float = 0.0
    markers: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def has_evidence(self) -> bool:
        return self.evidence_type != EVIDENCE_NONE and self.confidence >= 0.5

    @property
    def is_no_problem(self) -> bool:
        return self.evidence_type == EVIDENCE_NO_PROBLEM and self.confidence >= 0.7

    @property
    def is_problem(self) -> bool:
        return self.evidence_type not in {EVIDENCE_NONE, EVIDENCE_NO_PROBLEM} and self.confidence >= 0.65

    @property
    def is_explicit_problem(self) -> bool:
        return self.evidence_type == EVIDENCE_EXPLICIT_PROBLEM and self.confidence >= 0.7

    @property
    def is_business_problem(self) -> bool:
        return self.evidence_type in {
            EVIDENCE_DEPENDENCY_BLOCKED,
            EVIDENCE_QUALITY_GAP,
            EVIDENCE_PROGRESS_EXCEPTION,
            EVIDENCE_PROCESS_BLOCKED,
        } and self.confidence >= 0.65


def extract_problem_evidence(text: str) -> ProblemEvidence:
    raw = str(text or "").strip()
    if not raw or _looks_like_question(raw):
        return ProblemEvidence()
    compact = _compact(raw)
    no_problem = _matched_markers(compact, _NO_PROBLEM_MARKERS)
    if no_problem:
        return ProblemEvidence(
            evidence_type=EVIDENCE_NO_PROBLEM,
            confidence=0.92,
            markers=no_problem,
            reason="explicit no-problem answer",
        )

    explicit = _matched_markers(compact, _EXPLICIT_PROBLEM_MARKERS)
    future_plan = _looks_like_future_plan(raw)
    if explicit and _looks_like_future_resolution(raw):
        return ProblemEvidence()
    if explicit and not _looks_like_future_resolution(raw):
        return ProblemEvidence(
            evidence_type=EVIDENCE_EXPLICIT_PROBLEM,
            confidence=0.86,
            markers=explicit,
            reason="explicit problem/risk field marker",
        )

    if future_plan and not explicit:
        return ProblemEvidence()

    domain = _matched_markers(compact, _DOMAIN_OBJECT_MARKERS)
    actor = _matched_markers(compact, _DEPENDENCY_ACTOR_MARKERS)
    if domain or actor:
        dependency = _matched_patterns(raw, _DEPENDENCY_BLOCKED_PATTERNS)
        if dependency:
            return ProblemEvidence(
                evidence_type=EVIDENCE_DEPENDENCY_BLOCKED,
                confidence=0.82 if domain else 0.72,
                markers=tuple([*domain, *actor, *dependency]),
                reason="business dependency is not satisfied",
            )

        quality = _matched_patterns(raw, _QUALITY_GAP_PATTERNS)
        if quality:
            return ProblemEvidence(
                evidence_type=EVIDENCE_QUALITY_GAP,
                confidence=0.78,
                markers=tuple([*domain, *quality]),
                reason="business material or data quality gap",
            )

        progress = _matched_patterns(raw, _PROGRESS_EXCEPTION_PATTERNS)
        if progress:
            return ProblemEvidence(
                evidence_type=EVIDENCE_PROGRESS_EXCEPTION,
                confidence=0.78,
                markers=tuple([*domain, *progress]),
                reason="business progress is late or abnormal",
            )

        process = _matched_patterns(raw, _PROCESS_BLOCKED_PATTERNS)
        if process:
            return ProblemEvidence(
                evidence_type=EVIDENCE_PROCESS_BLOCKED,
                confidence=0.76,
                markers=tuple([*domain, *process]),
                reason="business process is blocked",
            )

    return ProblemEvidence()


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\[\]【】\"'“”‘’、/\\]+", "", str(value or "")).lower()


def _looks_like_question(value: str) -> bool:
    compact = _compact(value)
    if not compact:
        return False
    if str(value or "").strip().endswith(("?", "？")):
        return True
    return any(marker in compact for marker in ("\u4ec0\u4e48", "\u600e\u4e48", "\u5982\u4f55", "\u80fd\u5426", "\u53ef\u4e0d\u53ef\u4ee5", "\u4e3a\u4ec0\u4e48"))


def _looks_like_future_plan(value: str) -> bool:
    compact = _compact(value)
    return any(marker in compact for marker in ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u8ba1\u5212", "\u62df", "\u4e0b\u4e00\u6b65"))


def _looks_like_future_resolution(value: str) -> bool:
    compact = _compact(value)
    if not _looks_like_future_plan(value):
        return False
    return any(marker in compact for marker in ("\u5904\u7406", "\u89e3\u51b3", "\u8ddf\u8fdb", "\u63a8\u8fdb", "\u5b8c\u5584", "\u8865\u9f50", "\u8865\u5145"))


def _matched_markers(compact_text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(marker for marker in markers if _compact(marker) in compact_text)


def _matched_patterns(text: str, patterns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(pattern for pattern in patterns if re.search(pattern, text))


_NO_PROBLEM_MARKERS = (
    "\u6ca1\u5565\u95ee\u9898",
    "\u6ca1\u4ec0\u4e48\u95ee\u9898",
    "\u6ca1\u95ee\u9898",
    "\u6ca1\u6709\u95ee\u9898",
    "\u6ca1\u98ce\u9669",
    "\u6ca1\u6709\u98ce\u9669",
    "\u6682\u65e0\u95ee\u9898",
    "\u65e0\u95ee\u9898",
    "\u6682\u65e0\u98ce\u9669",
    "\u65e0\u98ce\u9669",
)

_EXPLICIT_PROBLEM_MARKERS = (
    "\u95ee\u9898/\u98ce\u9669",
    "\u95ee\u9898\u98ce\u9669",
    "\u98ce\u9669\u95ee\u9898",
    "\u5b58\u5728\u95ee\u9898",
    "\u95ee\u9898",
    "\u98ce\u9669",
    "\u56f0\u96be",
    "\u5361\u70b9",
    "\u9690\u60a3",
)

_DOMAIN_OBJECT_MARKERS = (
    "\u5408\u540c",
    "\u6761\u6b3e",
    "\u5370\u7ae0",
    "\u7528\u5370",
    "\u51fd\u4ef6",
    "\u6750\u6599",
    "\u8d44\u6599",
    "\u6e05\u5355",
    "\u6848\u4ef6",
    "\u6848\u53f7",
    "\u8ba8\u85aa",
    "\u8d77\u8bc9\u72b6",
    "\u9879\u76ee",
    "\u7cfb\u7edf",
    "\u6a21\u5757",
    "\u53f0\u8d26",
    "\u6d41\u7a0b",
    "\u5de5\u5177",
    "\u65e5\u62a5",
    "\u65e5\u5fd7",
    "\u6708\u62a5",
    "\u5468\u62a5",
    "\u6587\u6863",
    "\u6a21\u677f",
    "\u6570\u636e",
    "\u9700\u6c42",
    "\u673a\u5668\u4eba",
    "\u4f1a\u8bae",
    "\u6cd5\u9662",
    "\u5ba2\u6237",
    "\u4f9b\u5e94\u5546",
    "\u4e1a\u52a1",
    "\u90e8\u95e8",
    "\u5ba1\u6279",
    "\u8868\u683c",
    "\u5de5\u5355",
    "\u65b9\u6848",
    "\u62a5\u544a",
    "\u7ed3\u7b97",
    "\u6536\u6b3e",
    "\u56de\u6b3e",
    "\u5229\u606f",
    "\u7d22\u8d54",
    "\u975e\u8bc9",
    "\u6267\u884c",
    "\u7834\u4ea7",
    "\u503a\u6743",
)

_DEPENDENCY_ACTOR_MARKERS = (
    "\u5ba2\u6237",
    "\u4f9b\u5e94\u5546",
    "\u4e1a\u4e3b",
    "\u5bf9\u65b9",
    "\u6cd5\u9662",
    "\u4e1a\u52a1\u90e8\u95e8",
    "\u5185\u90e8\u90e8\u95e8",
)

_DEPENDENCY_BLOCKED_PATTERNS = (
    r"(\u6ca1|\u672a|\u5c1a\u672a|\u6ca1\u6709).{0,8}(\u53cd\u9988|\u63d0\u4f9b|\u786e\u8ba4|\u8865\u5145|\u914d\u5408|\u56de\u590d|\u5230\u4f4d)",
    r"(\u7b49|\u5f85|\u9700\u7b49|\u8fd8\u8981\u7b49).{0,10}(\u53cd\u9988|\u786e\u8ba4|\u63d0\u4f9b|\u8865\u5145|\u914d\u5408)",
    r"(\u65e0\u6cd5|\u4e0d\u80fd|\u96be\u4ee5).{0,10}(\u63a8\u8fdb|\u529e\u7406|\u5b8c\u6210|\u786e\u8ba4|\u63d0\u4ea4|\u5ba1\u6279|\u6267\u884c)",
)

_QUALITY_GAP_PATTERNS = (
    r"(\u4e0d\u5b8c\u6574|\u4e0d\u9f50|\u4e0d\u4e00\u81f4|\u4e0d\u6e05\u6670|\u4e0d\u7b26\u5408|\u6709\u8bef|\u9519\u8bef)",
    r"(\u7f3a\u5931|\u7f3a\u5c11|\u7f3a\u53e3|\u6f0f\u9879|\u9057\u6f0f)",
)

_PROGRESS_EXCEPTION_PATTERNS = (
    r"(\u903e\u671f|\u8d85\u671f|\u5ef6\u8bef|\u5ef6\u8fdf|\u6ede\u540e|\u6ede\u7559|\u62d6\u5ef6)",
    r"(\u672a\u6309\u671f|\u6ca1\u6309\u671f|\u8fdb\u5ea6.{0,6}\u6162|\u8282\u70b9.{0,6}\u8d85)",
)

_PROCESS_BLOCKED_PATTERNS = (
    r"(\u5361\u4f4f|\u5361\u70b9|\u963b\u6ede|\u963b\u585e|\u5835\u70b9|\u63a8\u4e0d\u52a8)",
    r"(\u6d41\u7a0b|\u5ba1\u6279|\u7cfb\u7edf|\u63a5\u53e3).{0,8}(\u5361|\u963b\u6ede|\u5ef6\u8bef|\u62a5\u9519|\u5931\u8d25)",
)
