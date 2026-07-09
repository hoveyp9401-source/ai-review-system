from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class HarnessContext:
    user_id: str = "harness-user"
    sender_name: str = "Harness User"
    dingtalk_user_id: str = "harness-dingtalk-user"
    channel: str = "direct"
    source: str = "golden"
    message_time: datetime | None = None
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    recent_state: dict[str, Any] = field(default_factory=dict)
    context_quality: str = "full"
    current_date: str | None = None
    org_users: list[dict[str, Any]] = field(default_factory=list)
    org_teams: list[dict[str, Any]] = field(default_factory=list)
    daily_history: list[dict[str, Any]] = field(default_factory=list)
    current_daily_report: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None, *, source: str = "golden") -> "HarnessContext":
        data = dict(value or {})
        message_time = data.get("message_time") or data.get("received_at")
        parsed_time: datetime | None = None
        if message_time:
            parsed_time = datetime.fromisoformat(str(message_time).replace("Z", "+00:00"))
        recent_state = dict(data.get("recent_state") or {})
        return cls(
            user_id=str(data.get("user_id") or "harness-user"),
            sender_name=str(data.get("sender_name") or "Harness User"),
            dingtalk_user_id=str(data.get("dingtalk_user_id") or data.get("user_id") or "harness-dingtalk-user"),
            channel=str(data.get("channel") or "direct"),
            source=str(data.get("source") or source),
            message_time=parsed_time,
            active_tasks=list(data.get("active_tasks") or []),
            recent_state=recent_state,
            context_quality=str(data.get("context_quality") or "full"),
            current_date=(
                str(data.get("current_date") or recent_state.get("current_date") or recent_state.get("report_date") or "")
                or None
            ),
            org_users=list(data.get("org_users") or []),
            org_teams=list(data.get("org_teams") or []),
            daily_history=list(data.get("daily_history") or []),
            current_daily_report=data.get("current_daily_report") or recent_state.get("current_daily_report"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "sender_name": self.sender_name,
            "dingtalk_user_id": self.dingtalk_user_id,
            "channel": self.channel,
            "source": self.source,
            "message_time": self.message_time.isoformat() if self.message_time else None,
            "active_tasks": list(self.active_tasks),
            "recent_state": dict(self.recent_state),
            "context_quality": self.context_quality,
            "current_date": self.current_date,
            "org_users": list(self.org_users),
            "org_teams": list(self.org_teams),
            "daily_history": list(self.daily_history),
            "current_daily_report": dict(self.current_daily_report) if isinstance(self.current_daily_report, dict) else self.current_daily_report,
        }


@dataclass(frozen=True)
class ExpectedOutcome:
    primary_workflow: str | None = None
    matched_workflows: list[str] | None = None
    must_include_workflows: list[str] = field(default_factory=list)
    must_not_include_workflows: list[str] = field(default_factory=list)
    expected_effects: list[str] = field(default_factory=list)
    forbidden_effects: list[str] = field(default_factory=list)
    expected_coordination_actions: list[str] = field(default_factory=list)
    forbidden_coordination_actions: list[str] = field(default_factory=list)
    expected_sandbox_candidates: list[str] = field(default_factory=list)
    forbidden_sandbox_candidates: list[str] = field(default_factory=list)
    expected_commands: list[str] = field(default_factory=list)
    forbidden_commands: list[str] = field(default_factory=list)
    expected_user_actions: list[str] = field(default_factory=list)
    forbidden_user_actions: list[str] = field(default_factory=list)
    cognitive_allow_write: bool | None = None
    should_enter_daily: bool | None = None
    need_confirmation: bool | None = None
    need_clarification: bool | None = None
    safe_to_write: bool | None = None
    gate_reply_type: str | None = None
    safety_commit_policy: str | None = None
    min_segments: int | None = None
    segment_workflows: list[str] = field(default_factory=list)
    report_date: str | None = None
    target_field: str | None = None
    risk_level: str | None = None
    expected_knowledge_status: str | None = None
    expected_knowledge_sources: list[str] = field(default_factory=list)
    forbidden_knowledge_sources: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "ExpectedOutcome":
        data = dict(value or {})
        return cls(
            primary_workflow=data.get("primary_workflow"),
            matched_workflows=list(data["matched_workflows"]) if data.get("matched_workflows") is not None else None,
            must_include_workflows=list(data.get("must_include_workflows") or []),
            must_not_include_workflows=list(data.get("must_not_include_workflows") or []),
            expected_effects=list(data.get("expected_effects") or []),
            forbidden_effects=list(data.get("forbidden_effects") or []),
            expected_coordination_actions=list(data.get("expected_coordination_actions") or []),
            forbidden_coordination_actions=list(data.get("forbidden_coordination_actions") or []),
            expected_sandbox_candidates=list(data.get("expected_sandbox_candidates") or []),
            forbidden_sandbox_candidates=list(data.get("forbidden_sandbox_candidates") or []),
            expected_commands=list(data.get("expected_commands") or []),
            forbidden_commands=list(data.get("forbidden_commands") or []),
            expected_user_actions=list(data.get("expected_user_actions") or []),
            forbidden_user_actions=list(data.get("forbidden_user_actions") or []),
            cognitive_allow_write=data.get("cognitive_allow_write"),
            should_enter_daily=data.get("should_enter_daily"),
            need_confirmation=data.get("need_confirmation"),
            need_clarification=data.get("need_clarification"),
            safe_to_write=data.get("safe_to_write"),
            gate_reply_type=data.get("gate_reply_type"),
            safety_commit_policy=data.get("safety_commit_policy"),
            min_segments=data.get("min_segments"),
            segment_workflows=list(data.get("segment_workflows") or []),
            report_date=data.get("report_date"),
            target_field=data.get("target_field"),
            risk_level=data.get("risk_level"),
            expected_knowledge_status=data.get("expected_knowledge_status"),
            expected_knowledge_sources=list(data.get("expected_knowledge_sources") or []),
            forbidden_knowledge_sources=list(data.get("forbidden_knowledge_sources") or []),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_workflow": self.primary_workflow,
            "matched_workflows": self.matched_workflows,
            "must_include_workflows": list(self.must_include_workflows),
            "must_not_include_workflows": list(self.must_not_include_workflows),
            "expected_effects": list(self.expected_effects),
            "forbidden_effects": list(self.forbidden_effects),
            "expected_coordination_actions": list(self.expected_coordination_actions),
            "forbidden_coordination_actions": list(self.forbidden_coordination_actions),
            "expected_sandbox_candidates": list(self.expected_sandbox_candidates),
            "forbidden_sandbox_candidates": list(self.forbidden_sandbox_candidates),
            "expected_commands": list(self.expected_commands),
            "forbidden_commands": list(self.forbidden_commands),
            "expected_user_actions": list(self.expected_user_actions),
            "forbidden_user_actions": list(self.forbidden_user_actions),
            "cognitive_allow_write": self.cognitive_allow_write,
            "should_enter_daily": self.should_enter_daily,
            "need_confirmation": self.need_confirmation,
            "need_clarification": self.need_clarification,
            "safe_to_write": self.safe_to_write,
            "gate_reply_type": self.gate_reply_type,
            "safety_commit_policy": self.safety_commit_policy,
            "min_segments": self.min_segments,
            "segment_workflows": list(self.segment_workflows),
            "report_date": self.report_date,
            "target_field": self.target_field,
            "risk_level": self.risk_level,
            "expected_knowledge_status": self.expected_knowledge_status,
            "expected_knowledge_sources": list(self.expected_knowledge_sources),
            "forbidden_knowledge_sources": list(self.forbidden_knowledge_sources),
        }


@dataclass(frozen=True)
class HarnessCase:
    case_id: str
    text: str
    source: str = "golden"
    description: str = ""
    context: HarnessContext = field(default_factory=HarnessContext)
    expected: ExpectedOutcome = field(default_factory=ExpectedOutcome)
    tags: list[str] = field(default_factory=list)
    severity: str = "medium"
    expected_failure: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "HarnessCase":
        source = str(value.get("source") or "golden")
        return cls(
            case_id=str(value["case_id"]),
            text=str(value.get("text") or ""),
            source=source,
            description=str(value.get("description") or ""),
            context=HarnessContext.from_mapping(value.get("context"), source=source),
            expected=ExpectedOutcome.from_mapping(value.get("expected")),
            tags=list(value.get("tags") or []),
            severity=str(value.get("severity") or "medium"),
            expected_failure=bool(value.get("expected_failure") or False),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source": self.source,
            "description": self.description,
            "text": self.text,
            "context": self.context.as_dict(),
            "expected": self.expected.as_dict(),
            "tags": list(self.tags),
            "severity": self.severity,
            "expected_failure": self.expected_failure,
        }


@dataclass(frozen=True)
class ActualOutcome:
    primary_workflow: str
    matched_workflows: list[str]
    effect_types: list[str]
    safety_commit_policy: str
    safety_flags: list[str]
    gate_allow_legacy_daily: bool
    gate_block_legacy_daily: bool
    gate_reply_type: str
    gate_need_confirmation: bool
    gate_need_clarification: bool
    gate_audit_tags: list[str]
    target_fields: list[str] = field(default_factory=list)
    coordination_action_types: list[str] = field(default_factory=list)
    coordination_actions: list[dict[str, Any]] = field(default_factory=list)
    sandbox_candidate_types: list[str] = field(default_factory=list)
    sandbox_candidates: list[dict[str, Any]] = field(default_factory=list)
    sandbox_notification_count: int = 0
    sandbox_official_write_count: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    legacy_adapter_results: list[dict[str, Any]] = field(default_factory=list)
    cognitive_decision: dict[str, Any] = field(default_factory=dict)
    contract_invariant_violations: list[dict[str, Any]] = field(default_factory=list)
    knowledge_status: str = "not_retrieved_or_no_match"
    knowledge_source_types: list[str] = field(default_factory=list)
    knowledge_titles: list[str] = field(default_factory=list)
    knowledge_facts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_workflow": self.primary_workflow,
            "matched_workflows": list(self.matched_workflows),
            "effect_types": list(self.effect_types),
            "safety_commit_policy": self.safety_commit_policy,
            "safety_flags": list(self.safety_flags),
            "gate_allow_legacy_daily": self.gate_allow_legacy_daily,
            "gate_block_legacy_daily": self.gate_block_legacy_daily,
            "gate_reply_type": self.gate_reply_type,
            "gate_need_confirmation": self.gate_need_confirmation,
            "gate_need_clarification": self.gate_need_clarification,
            "gate_audit_tags": list(self.gate_audit_tags),
            "target_fields": list(self.target_fields),
            "coordination_action_types": list(self.coordination_action_types),
            "coordination_actions": list(self.coordination_actions),
            "sandbox_candidate_types": list(self.sandbox_candidate_types),
            "sandbox_candidates": list(self.sandbox_candidates),
            "sandbox_notification_count": self.sandbox_notification_count,
            "sandbox_official_write_count": self.sandbox_official_write_count,
            "segments": list(self.segments),
            "commands": list(self.commands),
            "legacy_adapter_results": list(self.legacy_adapter_results),
            "cognitive_decision": dict(self.cognitive_decision),
            "contract_invariant_violations": list(self.contract_invariant_violations),
            "knowledge_status": self.knowledge_status,
            "knowledge_source_types": list(self.knowledge_source_types),
            "knowledge_titles": list(self.knowledge_titles),
            "knowledge_facts": list(self.knowledge_facts),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class HarnessResult:
    case_id: str
    source: str
    passed: bool
    severity: str
    expected_failure: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    actual: ActualOutcome | None = None
    expected: ExpectedOutcome | None = None

    @property
    def is_unexpected_failure(self) -> bool:
        return not self.passed and not self.expected_failure

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source": self.source,
            "passed": self.passed,
            "severity": self.severity,
            "expected_failure": self.expected_failure,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "tags": list(self.tags),
            "actual": self.actual.as_dict() if self.actual else None,
            "expected": self.expected.as_dict() if self.expected else None,
        }


@dataclass(frozen=True)
class HarnessRunSummary:
    total_cases: int
    passed_cases: int
    failed_cases: int
    expected_failure_cases: int
    unexpected_failure_cases: int
    failure_by_severity: dict[str, int]
    failure_by_tag: dict[str, int]
    multi_intent_failure_count: int
    dangerous_action_failure_count: int
    naked_confirmation_failure_count: int
    non_daily_false_allow_count: int
    daily_false_block_count: int
    legacy_adapter_status_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "expected_failure_cases": self.expected_failure_cases,
            "unexpected_failure_cases": self.unexpected_failure_cases,
            "failure_by_severity": dict(self.failure_by_severity),
            "failure_by_tag": dict(self.failure_by_tag),
            "multi_intent_failure_count": self.multi_intent_failure_count,
            "dangerous_action_failure_count": self.dangerous_action_failure_count,
            "naked_confirmation_failure_count": self.naked_confirmation_failure_count,
            "non_daily_false_allow_count": self.non_daily_false_allow_count,
            "daily_false_block_count": self.daily_false_block_count,
            "legacy_adapter_status_counts": dict(self.legacy_adapter_status_counts),
        }
