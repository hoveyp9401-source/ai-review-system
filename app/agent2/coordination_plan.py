from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any

from app.workflows.intake import (
    IncomingMessageEnvelope,
    RoutingPlan,
    WORKFLOW_CASE_PROGRESS,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_TRAVEL_COORDINATION,
    WorkflowRouter,
)
from app.workflows.problem_evidence import extract_problem_evidence
from app.workflows.relative_dates import date_hint_from_text


ACTION_DAILY_ENTRY = "daily_entry"
ACTION_TRAVEL_EVENT = "travel_event"
ACTION_APPEND_CASE_PROGRESS = "case_progress_entry"


@dataclass(frozen=True)
class CoordinationAction:
    """One workflow-local action extracted from a user turn."""

    action_type: str
    workflow: str
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    source_segment_index: int = 0
    source_text_hash: str = ""
    source_text_chars: int = 0
    confidence: float = 0.0
    requires_confirmation: bool = False
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "workflow": self.workflow,
            "target": dict(self.target),
            "payload_keys": sorted(self.payload.keys()),
            "source_segment_index": self.source_segment_index,
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "confidence": self.confidence,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CoordinationPlan:
    """Execution-facing Agent2 plan for workflows that can coexist in one turn."""

    primary_workflow: str
    matched_workflows: list[str]
    actions: list[CoordinationAction] = field(default_factory=list)
    commit_policy: str = "blocked"
    pending_action_types: list[str] = field(default_factory=list)
    safe_action_types: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_text_hash: str = ""
    source_text_chars: int = 0
    routing_plan: RoutingPlan | None = None

    def as_observation(self) -> dict[str, Any]:
        return {
            "primary_workflow": self.primary_workflow,
            "matched_workflows": list(self.matched_workflows),
            "action_types": [action.action_type for action in self.actions],
            "commit_policy": self.commit_policy,
            "pending_action_types": list(self.pending_action_types),
            "safe_action_types": list(self.safe_action_types),
            "warnings": list(self.warnings),
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "actions": [action.as_observation() for action in self.actions],
        }


@dataclass(frozen=True)
class _SegmentFrame:
    text: str
    inherited_field: str = ""


def compile_coordination_plan(
    envelope: IncomingMessageEnvelope,
    *,
    routing_plan: RoutingPlan | None = None,
    router: WorkflowRouter | None = None,
) -> CoordinationPlan:
    """Compile one user turn into independent workflow actions.

    This is deliberately a planning layer. It does not send DingTalk messages,
    write daily reports, or notify travelers.
    """

    plan = routing_plan or (router or WorkflowRouter()).plan(envelope)
    raw_text = str(envelope.raw_text or "")
    allowed_workflows = _allowed_coordination_workflows(plan) if routing_plan is not None else None
    actions: list[CoordinationAction] = []
    for index, frame in enumerate(_split_coordination_segment_frames(raw_text, received_at=getattr(envelope, "received_at", None)), start=1):
        actions.extend(
            _actions_for_segment(
                frame.text,
                index=index,
                whole_text=raw_text,
                allowed_workflows=allowed_workflows,
                received_at=getattr(envelope, "received_at", None),
                inherited_field=frame.inherited_field,
            )
        )

    actions = _dedupe_actions(actions)
    safe_action_types = [action.action_type for action in actions if not action.requires_confirmation]
    pending_action_types = [action.action_type for action in actions if action.requires_confirmation]
    commit_policy = _commit_policy(actions)
    matched_workflows = _matched_workflows(plan, actions)
    return CoordinationPlan(
        primary_workflow=_primary_workflow(plan, actions),
        matched_workflows=matched_workflows,
        actions=actions,
        commit_policy=commit_policy,
        pending_action_types=pending_action_types,
        safe_action_types=safe_action_types,
        warnings=_warnings(actions, plan),
        source_text_hash=_hash_text(raw_text),
        source_text_chars=len(raw_text),
        routing_plan=plan,
    )


def _actions_for_segment(
    segment: str,
    *,
    index: int,
    whole_text: str,
    allowed_workflows: set[str] | None = None,
    received_at: Any = None,
    inherited_field: str = "",
) -> list[CoordinationAction]:
    actions: list[CoordinationAction] = []
    daily_field = _daily_field_for_segment(
        segment,
        whole_text=whole_text,
        received_at=received_at,
        inherited_field=inherited_field,
    )
    if daily_field and _workflow_allowed(allowed_workflows, WORKFLOW_DAILY_REPORT):
        actions.append(
            CoordinationAction(
                action_type=ACTION_DAILY_ENTRY,
                workflow=WORKFLOW_DAILY_REPORT,
                target={"field": daily_field},
                payload={
                    "content": segment,
                    "work_kind": "product_work" if _looks_like_product_work(segment) else "work_update",
                },
                source_segment_index=index,
                source_text_hash=_hash_text(segment),
                source_text_chars=len(segment),
                confidence=0.82,
                reason="segment contains daily-report work, problem, or plan content",
            )
        )

    if _workflow_allowed(allowed_workflows, WORKFLOW_TRAVEL_COORDINATION) and _looks_like_travel_event(segment):
        status = _travel_status(segment, received_at=received_at)
        actions.append(
            CoordinationAction(
                action_type=ACTION_TRAVEL_EVENT,
                workflow=WORKFLOW_TRAVEL_COORDINATION,
                target={
                    "destination": _travel_destination(segment),
                    "date_hint": _date_hint(segment, received_at=received_at),
                    "status": status,
                },
                payload={
                    "content": segment,
                    "activity_hint": _travel_activity_hint(segment),
                    "needs_return_confirmation": _needs_return_confirmation(segment, status),
                },
                source_segment_index=index,
                source_text_hash=_hash_text(segment),
                source_text_chars=len(segment),
                confidence=0.86,
                requires_confirmation=True,
                reason="segment contains a concrete trip rather than product work about travel coordination",
            )
        )

    matter_hint = _specific_matter_hint(segment)
    if matter_hint and _workflow_allowed(allowed_workflows, WORKFLOW_CASE_PROGRESS):
        actions.append(
            CoordinationAction(
                action_type=ACTION_APPEND_CASE_PROGRESS,
                workflow=WORKFLOW_CASE_PROGRESS,
                target={"matter_hint": matter_hint},
                payload={"content": segment},
                source_segment_index=index,
                source_text_hash=_hash_text(segment),
                source_text_chars=len(segment),
                confidence=0.78,
                requires_confirmation=True,
                reason="segment mentions a specific matter, not just generic case work",
            )
        )
    return actions


def _allowed_coordination_workflows(plan: RoutingPlan) -> set[str]:
    """Limit coordination extraction to workflows owned by the first routing layer."""

    allowed = {workflow for workflow in plan.matched_workflows if workflow}
    allowed.update(effect.target_system for effect in plan.effects if effect.target_system)
    return allowed


def _workflow_allowed(allowed_workflows: set[str] | None, workflow: str) -> bool:
    if allowed_workflows is None:
        return True
    return workflow in allowed_workflows


def _split_coordination_segments(raw_text: str) -> list[str]:
    return [frame.text for frame in _split_coordination_segment_frames(raw_text)]


def _split_coordination_segment_frames(raw_text: str, *, received_at: Any = None) -> list[_SegmentFrame]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    frames: list[_SegmentFrame] = []
    for sentence in re.split(r"[\r\n;；。！？?]+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if _looks_like_completed_previous_plan(sentence):
            frames.append(_SegmentFrame(sentence, "today_work"))
            continue
        current_field = ""
        for comma_piece in [piece.strip() for piece in re.split(r"[，,]", sentence) if piece.strip()]:
            list_pieces = _split_list_piece_for_context(
                comma_piece,
                current_field=current_field,
                received_at=received_at,
            )
            for piece in list_pieces:
                explicit_field = _explicit_split_field(piece, received_at=received_at)
                inherited_field = explicit_field
                if not inherited_field and _can_inherit_split_field(piece, current_field):
                    inherited_field = current_field
                frames.append(_SegmentFrame(piece, inherited_field))
                if explicit_field in {"today_work", "tomorrow_plan"}:
                    current_field = explicit_field
                elif inherited_field in {"today_work", "tomorrow_plan"}:
                    current_field = inherited_field
                elif explicit_field == "problems":
                    current_field = ""
    return frames


def _daily_field_for_segment(
    segment: str,
    *,
    whole_text: str,
    received_at: Any = None,
    inherited_field: str = "",
) -> str:
    if _looks_like_meta_conversation_request(segment):
        return ""
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if (
        _contains_any(segment, ("\u4e0b\u5468", "\u4e0b\u6708", "\u540e\u5929"))
        and not _contains_any(segment, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5"))
        and relative_hint not in {"today", "tomorrow"}
    ):
        return ""
    if relative_hint in {"future_weekday", "next_week", "past_weekday"} and not _contains_any(
        whole_text,
        ("\u65e5\u62a5", "\u4eca\u65e5", "\u4eca\u5929", "\u660e\u5929", "\u660e\u65e5"),
    ):
        return ""
    if _looks_like_completed_previous_plan(segment):
        return "today_work"
    problem_evidence = extract_problem_evidence(segment)
    if problem_evidence.is_no_problem:
        return "problems"
    if problem_evidence.is_problem:
        return "problems"
    if _looks_like_question(segment):
        return ""
    if inherited_field in {"today_work", "tomorrow_plan"} and (
        _looks_like_travel_event(segment) or _looks_like_daily_work(segment) or _has_business_work_evidence(segment)
    ):
        return inherited_field
    if relative_hint in {"future_weekday", "next_week", "past_weekday"}:
        return ""
    if relative_hint == "tomorrow" and (_looks_like_travel_event(segment) or _looks_like_daily_work(segment)):
        return "tomorrow_plan"
    if relative_hint == "today" and (_looks_like_travel_event(segment) or _looks_like_daily_work(segment)):
        return "today_work"
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5")):
        return "tomorrow_plan"
    if _looks_like_daily_work(segment):
        return "today_work"
    if _looks_like_travel_event(segment) and _contains_any(whole_text, ("\u65e5\u62a5", "\u4eca\u65e5", "\u4eca\u5929", "\u660e\u5929", "\u660e\u65e5")):
        return "today_work"
    return ""


def _split_list_piece_for_context(
    piece: str,
    *,
    current_field: str,
    received_at: Any = None,
) -> list[str]:
    value = str(piece or "").strip()
    if "、" not in value:
        return [value] if value else []
    explicit_field = _explicit_split_field(value, received_at=received_at)
    if not explicit_field and current_field not in {"today_work", "tomorrow_plan"}:
        return [value]
    parts = [part.strip() for part in value.split("、") if part.strip()]
    return parts or ([value] if value else [])


def _explicit_split_field(segment: str, *, received_at: Any = None) -> str:
    text = str(segment or "").strip()
    if not text or _looks_like_question(text):
        return ""
    problem_evidence = extract_problem_evidence(text)
    if problem_evidence.is_problem or problem_evidence.is_no_problem:
        return "problems"
    relative_hint = date_hint_from_text(text, received_at=received_at)
    if relative_hint == "today":
        return "today_work"
    if relative_hint == "tomorrow":
        return "tomorrow_plan"
    if _contains_any(text, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u4e2a", "\u660e\u5929\u8ba1\u5212", "\u660e\u65e5\u8ba1\u5212")):
        return "tomorrow_plan"
    if _contains_any(text, ("\u4eca\u5929", "\u4eca\u65e5", "\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5\u5de5\u4f5c")):
        return "today_work"
    return ""


def _can_inherit_split_field(segment: str, current_field: str) -> bool:
    if current_field not in {"today_work", "tomorrow_plan"}:
        return False
    if _looks_like_question(segment) or _looks_like_meta_conversation_request(segment):
        return False
    if extract_problem_evidence(segment).is_problem:
        return False
    return _looks_like_travel_event(segment) or _looks_like_daily_work(segment) or _has_business_work_evidence(segment)


def _has_business_work_evidence(segment: str) -> bool:
    return _contains_any(
        segment,
        (
            "\u5b8c\u6210",
            "\u53c2\u52a0",
            "\u6536\u96c6",
            "\u7f16\u8f91",
            "\u4fee\u8ba2",
            "\u4f18\u5316",
            "\u5904\u7406",
            "\u6c9f\u901a",
            "\u68b3\u7406",
            "\u6574\u7406",
            "\u5ba1\u6838",
            "\u8bc4\u5ba1",
            "\u53d1\u9001",
        ),
    ) or _contains_any(
        segment,
        (
            "\u5408\u540c",
            "\u51fd\u4ef6",
            "\u6750\u6599",
            "\u8d44\u6599",
            "\u6848\u4ef6",
            "\u65e5\u62a5",
            "\u6708\u62a5",
            "\u516c\u4f17\u53f7",
            "\u901a\u62a5",
            "\u90ae\u4ef6",
            "\u4f1a\u8bae",
        ),
    )


def _looks_like_meta_conversation_request(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if compact in {
        "\u548b\u8bf4",
        "\u600e\u4e48\u8bf4",
        "\u548b\u804a",
        "\u548b\u95f2\u804a",
        "\u600e\u4e48\u95f2\u804a",
        "\u804a\u804a",
        "\u804a\u4f1a",
        "\u804a\u4e00\u4f1a",
        "\u548c\u6211\u804a\u5929",
        "\u548c\u6211\u804a\u804a",
        "\u8ddf\u6211\u804a",
        "\u8ddf\u6211\u804a\u804a",
        "\u966a\u6211\u804a",
        "\u966a\u6211\u804a\u804a",
        "\u8bf4\u8bf4\u8bdd",
        "\u53ef\u4ee5\u548c\u6211\u804a\u804a\u5417",
        "\u53ef\u4ee5\u548c\u6211\u804a\u804a",
    }:
        return True
    return _contains_any(
        text,
        (
            "\u6211\u60f3\u804a",
            "\u60f3\u804a",
            "\u804a\u804a",
            "\u804a\u4f1a",
            "\u804a\u4e00\u4f1a",
            "\u548c\u6211\u804a",
            "\u8ddf\u6211\u804a",
            "\u966a\u6211\u804a",
            "\u8bf4\u8bf4\u8bdd",
            "\u95f2\u804a",
            "\u804a\u5929",
            "\u60f3\u8bf4\u4e2a",
            "\u60f3\u8bf4\u4e00\u4e2a",
            "\u6211\u60f3\u8bf4\u4e2a",
            "\u6211\u60f3\u8bf4\u4e00\u4e2a",
        ),
    )


def _looks_like_completed_previous_plan(segment: str) -> bool:
    return (
        _contains_any(segment, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929", "\u4e0a\u4e00\u4e2a\u5de5\u4f5c\u65e5"))
        and _contains_any(segment, ("\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u8ba1\u5212"))
        and _contains_any(segment, ("\u5df2\u5b8c\u6210", "\u5b8c\u6210\u4e86", "\u5b8c\u6210", "\u505a\u5b8c", "\u641e\u5b8c", "\u641e\u5b9a"))
    )


def _looks_like_daily_work(segment: str) -> bool:
    if _looks_like_question(segment):
        return False
    return _contains_any(
        segment,
        (
            "\u5408\u540c",
            "\u5ba1\u6838",
            "\u51fd\u4ef6",
            "\u8ba8\u85aa",
            "\u9879\u76ee\u8bc4\u5ba1",
            "\u8fc7\u5802\u4f1a",
            "\u5316\u503a",
            "\u6c9f\u901a",
            "\u5f00\u5ead",
            "\u4f18\u5316",
            "\u8131\u654f\u5de5\u5177",
            "\u65e5\u62a5\u7cfb\u7edf",
            "\u6848\u4ef6\u8fdb\u5c55",
            "\u51fa\u5dee\u534f\u540c",
            "\u5904\u7406",
            "\u68b3\u7406",
            "\u6750\u6599",
            "\u76d6\u7ae0",
            "\u51fa\u5dee",
            "\u5e38\u5dde",
            "\u5357\u4eac",
            "\u626c\u5dde",
            "\u5609\u5174",
        ),
    )


def _looks_like_problem(segment: str) -> bool:
    return extract_problem_evidence(segment).is_explicit_problem


def _looks_like_no_problem(segment: str) -> bool:
    return extract_problem_evidence(segment).is_no_problem


def _looks_like_travel_event(segment: str) -> bool:
    if _looks_like_product_work(segment) and not _looks_like_concrete_trip(segment):
        return False
    return _looks_like_concrete_trip(segment)


def _looks_like_concrete_trip(segment: str) -> bool:
    text = str(segment or "")
    if "\u51fa\u5dee" in text and _travel_destination(text):
        return True
    if re.search(r"(\u53bb|\u8d74|\u5230|\u53bb\u4e86).{0,12}(\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a)", text) and _travel_destination(text):
        return True
    return False


def _looks_like_product_work(segment: str) -> bool:
    return _contains_any(segment, ("\u505a", "\u5f00\u53d1", "\u4f18\u5316", "\u5efa\u8bbe", "\u642d\u5efa", "\u6d4b\u8bd5", "\u4e0a\u7ebf", "\u5f00\u59cb\u505a")) and _contains_any(
        segment,
        (
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u529f\u80fd",
            "\u5de5\u5177",
            "\u5e73\u53f0",
            "\u673a\u5668\u4eba",
            "\u51fa\u5dee\u534f\u540c",
            "\u6848\u4ef6\u8fdb\u5c55",
        ),
    )


def _travel_destination(segment: str) -> str:
    text = str(segment or "")
    for destination in (
        "\u5609\u5174\u5357\u6e56\u8857\u9053",
        "\u5357\u6e56\u8857\u9053",
        "\u5357\u901a",
        "\u5357\u4eac",
        "\u626c\u5dde",
        "\u5609\u5174",
        "\u5e38\u5dde",
        "\u82cf\u5dde",
        "\u4e0a\u6d77",
        "\u5317\u4eac",
        "\u5e7f\u5dde",
        "\u6df1\u5733",
    ):
        if destination in text:
            return destination
    match = re.search(
        r"(?:\u51fa\u5dee|\u53bb|\u8d74|\u5230|\u53bb\u4e86)([\u4e00-\u9fa5]{2,8}?)(?:\u529e\u7406|\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a|\u8ba8\u85aa|\u51fa\u5dee|$)",
        text,
    )
    if not match:
        return ""
    candidate = re.sub(r"^(?:\u53bb|\u5230|\u8d74|\u524d\u5f80)", "", match.group(1))
    if _contains_any(
        candidate,
        (
            "\u534f\u540c\u7cfb\u7edf",
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u529f\u80fd",
            "\u5de5\u5177",
            "\u5e73\u53f0",
            "\u5de5\u4f5c\u6c47\u62a5",
            "\u6c47\u62a5",
            "\u5de5\u4f5c",
        ),
    ):
        return ""
    if _invalid_travel_destination_candidate(candidate):
        return ""
    return candidate


def _invalid_travel_destination_candidate(candidate: str) -> bool:
    value = str(candidate or "").strip(" ：:，,。；;、的")
    if not value:
        return True
    if _contains_any(
        value,
        (
            "\u5f00\u5ead",
            "\u51fa\u5ead",
            "\u76d6\u7ae0",
            "\u8d70\u8bbf",
            "\u5904\u7406",
            "\u6c9f\u901a",
            "\u8ba8\u85aa",
            "\u6848\u4ef6",
            "\u6848",
            "\u8fdb\u5c55",
            "\u6750\u6599",
            "\u5dee\u5f02",
            "\u7ade\u4e89",
            "\u7b56\u7565",
            "\u7b80\u62a5",
            "\u6c47\u62a5",
            "\u62a5\u544a",
            "\u65b9\u6848",
        ),
    ):
        return True
    return False


def _date_hint(segment: str, *, received_at: Any = None) -> str:
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if relative_hint != "unknown":
        return relative_hint
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5")):
        return "tomorrow"
    if _contains_any(segment, ("\u4eca\u5929", "\u4eca\u65e5")):
        return "today"
    if "\u4e0b\u5468" in segment:
        return "next_week"
    return "unknown"


def _travel_status(segment: str, *, received_at: Any = None) -> str:
    if "\u53ef\u80fd" in segment:
        return "tentative"
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if relative_hint in {"tomorrow", "future_weekday", "next_week"}:
        return "planned"
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u8ba1\u5212", "\u62df", "\u9884\u8ba1", "\u5e94\u8be5", "\u6253\u7b97", "\u51c6\u5907")):
        return "planned"
    if _contains_any(segment, ("\u53bb\u4e86", "\u5df2\u53bb", "\u5df2\u7ecf\u5230", "\u4eca\u5929", "\u4eca\u65e5")):
        return "already_traveled"
    if "\u51fa\u5dee" in segment:
        return "already_traveled"
    return "unknown"


def _needs_return_confirmation(segment: str, status: str) -> bool:
    if status != "already_traveled":
        return False
    return not _contains_any(segment, ("\u8fd4\u7a0b", "\u56de\u6765", "\u5df2\u8fd4\u56de", "\u5df2\u56de"))


def _travel_activity_hint(segment: str) -> str:
    for marker in ("\u5f00\u5ead", "\u76d6\u7ae0", "\u8d70\u8bbf", "\u5904\u7406", "\u6c9f\u901a", "\u8ba8\u85aa"):
        if marker in segment:
            return marker
    return ""


def _specific_matter_hint(segment: str) -> str:
    text = str(segment or "")
    if _looks_like_case_count_or_lookup_question(text):
        return ""
    if not _contains_any(text, ("\u6848", "\u6848\u4ef6", "\u6848\u53f7", "\u6cd5\u9662", "\u6267\u884c\u8fdb\u5c55", "\u5f00\u5ead")):
        return ""
    if _looks_like_case_product_build_work(text):
        return ""
    if _contains_any(
        text,
        (
            "\u6848\u4ef6\u8fdb\u5c55",
            "\u6848\u4ef6\u6c47\u62a5",
            "\u6848\u4ef6\u6c9f\u901a",
            "\u6848\u4ef6\u8d44\u6599",
            "\u6848\u4ef6\u7ba1\u7406",
            "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
        ),
    ):
        return ""
    number_match = re.search(r"[\uff08(]?\d{4}[\uff09)]?[\u4e00-\u9fa5\d\u53f7-]{4,40}", text)
    if number_match:
        candidate = _clean_matter_hint_candidate(number_match.group(0).strip())
        return candidate if _valid_matter_hint(candidate) else ""
    patterns = [
        r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u6848\u4ef6)",
        r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u6848)(?!\u4ef6)",
    ]
    if _contains_any(text, ("\u6cd5\u9662", "\u6267\u884c\u8fdb\u5c55", "\u5f00\u5ead")):
        patterns.append(r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u4e8b\u9879)")
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = _clean_matter_hint_candidate(match.group(1))
            if _valid_matter_hint(candidate):
                return candidate
    return ""


def _looks_like_case_count_or_lookup_question(text: str) -> bool:
    value = str(text or "")
    if not _contains_any(value, ("\u6848", "\u6848\u4ef6", "\u539f\u544a", "\u88ab\u544a")):
        return False
    return _contains_any(
        value,
        (
            "\u6709\u591a\u5c11",
            "\u591a\u5c11",
            "\u51e0\u4e2a",
            "\u51e0\u4ef6",
            "\u51e0\u6761",
            "\u54ea\u4e9b",
            "\u67e5\u4e00\u4e0b",
            "\u67e5\u4e0b",
            "\u770b\u4e0b",
            "\u7edf\u8ba1",
            "\u540d\u4e0b",
            "\u624b\u91cc",
        ),
    )


def _clean_matter_hint_candidate(candidate: str) -> str:
    value = str(candidate or "").strip(" ：:，,。；;、")
    value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u6628\u5929|\u6628\u65e5|\u4e0b\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]?)", "", value)
    value = re.sub(r"^(?:\u540e\u5929|\u672c\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]?|\u53bb|\u8d74|\u5230|\u524d\u5f80|\u51fa\u5dee)+", "", value)
    for marker in (
        "\u6c9f\u901a",
        "\u5904\u7406",
        "\u8ddf\u8fdb",
        "\u63a8\u8fdb",
        "\u529e\u7406",
        "\u534f\u8c03",
        "\u5bf9\u63a5",
        "\u7814\u7a76",
        "\u6574\u7406",
        "\u8865\u5145",
        "\u5f00\u5ead",
    ):
        if marker in value:
            tail = value.rsplit(marker, 1)[-1].strip(" ：:，,。；;、")
            if len(tail) >= 3:
                value = tail
    return value


def _valid_matter_hint(candidate: str) -> bool:
    value = str(candidate or "").strip(" ：:，,。；;、")
    if len(value) < 3:
        return False
    if _contains_any(
        value,
        (
            "\u65b9\u6848",
            "\u6863\u6848",
            "\u5224\u6848",
            "\u672c\u6848",
            "\u6848\u60c5",
            "\u6848\u4f8b",
            "\u7b54\u6848",
            "\u884c\u52a8\u65b9\u6848",
            "\u7ecf\u8425\u65b9\u6848",
            "\u5de5\u4f5c\u65b9\u6848",
            "\u5206\u914d\u65b9\u6848",
            "\u670d\u52a1\u5668\u65b9\u6848",
            "\u516c\u53f8\u6863\u6848",
            "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
            "\u6848\u4ef6\u6c47\u62a5",
        ),
    ):
        return False
    if value.endswith(("\u65b9\u6848", "\u6863\u6848", "\u5224\u6848")):
        return False
    if value in {
        "\u8bc9\u8bbc\u6848\u4ef6",
        "\u62df\u8bc9\u6848\u4ef6",
        "\u5f85\u8bc9\u6848\u4ef6",
        "\u88ab\u544a\u6848\u4ef6",
        "\u8bbe\u8ba1\u6848\u4ef6",
        "\u90e8\u5206\u6848\u4ef6",
        "\u76f8\u5173\u6848\u4ef6",
        "\u91cd\u70b9\u6848\u4ef6",
        "\u6240\u6709\u6848\u4ef6",
        "\u5168\u90e8\u6848\u4ef6",
        "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
        "\u6848\u4ef6\u6c47\u62a5",
    }:
        return False
    if value.endswith("\u6848\u4ef6"):
        prefix = value[: -len("\u6848\u4ef6")]
        if len(prefix) < 2:
            return False
        if _contains_any(prefix, ("\u51fa\u5dee", "\u6c9f\u901a", "\u5904\u7406", "\u529e\u7406", "\u5f00\u5ead", "\u53bb", "\u8d74", "\u5230")):
            return False
        if prefix in {"\u62df\u8bc9", "\u5f85\u8bc9", "\u8bc9\u8bbc", "\u88ab\u544a", "\u8bbe\u8ba1", "\u90e8\u5206", "\u76f8\u5173", "\u91cd\u70b9", "\u6240\u6709", "\u5168\u90e8", "\u4e00\u822c", "\u591a\u4e2a", "\u5404\u7c7b"}:
            return False
    return True


def _looks_like_case_product_build_work(text: str) -> bool:
    return _contains_any(text, ("\u6848\u4ef6", "\u6848\u4ef6\u8fdb\u5c55", "\u6848")) and _contains_any(
        text,
        (
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u5de5\u5177",
            "\u5e73\u53f0",
            "\u529f\u80fd",
            "\u81ea\u52a8\u5173\u8054",
            "\u56fa\u5b9a\u65f6\u95f4",
            "\u8282\u70b9\u8be2\u95ee",
            "\u6784\u5efa",
            "\u5b9e\u73b0",
            "\u5f00\u53d1",
            "\u4f18\u5316",
        ),
    )


def _commit_policy(actions: list[CoordinationAction]) -> str:
    if not actions:
        return "blocked"
    if any(action.requires_confirmation for action in actions):
        return "needs_confirmation"
    return "partial_allowed"


def _matched_workflows(plan: RoutingPlan, actions: list[CoordinationAction]) -> list[str]:
    workflows: list[str] = []
    for workflow in [*plan.matched_workflows, *(action.workflow for action in actions)]:
        if workflow and workflow not in workflows:
            workflows.append(workflow)
    return workflows


def _primary_workflow(plan: RoutingPlan, actions: list[CoordinationAction]) -> str:
    if actions:
        for workflow in (
            WORKFLOW_TRAVEL_COORDINATION,
            WORKFLOW_CASE_PROGRESS,
            WORKFLOW_DAILY_REPORT,
        ):
            if any(action.workflow == workflow for action in actions):
                return workflow
    return plan.primary_workflow


def _warnings(actions: list[CoordinationAction], plan: RoutingPlan) -> list[str]:
    warnings: list[str] = []
    action_workflows = {action.workflow for action in actions}
    if len(action_workflows) > 1:
        warnings.append("multi_workflow_actions")
    if plan.safety_decision.commit_policy == "blocked" and actions:
        warnings.append("coordination_actions_extracted_from_blocked_route")
    return warnings


def _dedupe_actions(actions: list[CoordinationAction]) -> list[CoordinationAction]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[CoordinationAction] = []
    for action in actions:
        key = (
            action.action_type,
            str(action.target.get("field") or action.target.get("destination") or action.target.get("matter_hint") or ""),
            action.source_text_hash,
            str(action.source_segment_index),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _looks_like_question(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if _contains_any(text, ("\u95ee\u4e0b", "\u95ee\u4e00\u4e0b")):
        return any(marker in compact for marker in ("\u662f\u4e0d\u662f", "\u8981\u4e0d\u8981", "\u600e\u4e48", "\u4e3a\u4ec0\u4e48", "\u80fd\u5426", "\u53ef\u4e0d\u53ef\u4ee5", "\u5417"))
    return text.endswith(("?", "\uff1f")) or _contains_any(
        text,
        (
            "\u662f\u4e0d\u662f",
            "\u8981\u4e0d\u8981",
            "\u5417",
            "\u600e\u4e48",
            "\u4e3a\u4ec0\u4e48",
            "\u80fd\u5426",
            "\u53ef\u4e0d\u53ef\u4ee5",
        ),
    )


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    compact = _compact(value)
    return any(_compact(marker) in compact for marker in markers)


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；、()（）【】\[\]\"'“”‘’]+", "", str(value or "")).lower()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
