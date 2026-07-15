from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol
from uuid import UUID

from app.agent2.command_planner_v3 import CognitiveCommandPlan
from app.agent2.cognitive_core_v3 import CognitiveDecisionV3
from app.agent2.conversation_state import ConversationState


RuntimeMode = Literal["shadow", "replay", "live"]
RuntimeSuccessStatus = Literal["completed", "partial", "needs_clarification", "blocked"]
RuntimeStatus = RuntimeSuccessStatus | Literal["failed_closed"]
RuntimeReplyType = Literal["ack_write", "ack_simulated", "answer", "clarification", "partial", "failure"]
DomainExecutionStatus = Literal[
    "succeeded",
    "simulated",
    "duplicate",
    "blocked",
    "unavailable",
    "failed",
]


def _frozen_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class RuntimeActor:
    """Authenticated actor projected into the Runtime.

    `actor_id` is the single identity authority. String conversion happens only
    at the existing Cognitive Core compatibility seam.
    """

    actor_id: UUID
    display_name: str = ""
    dingtalk_user_id: str = ""
    role: str = "member"
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True)
class RuntimeTurnRequest:
    tenant_id: str
    actor: RuntimeActor
    conversation_id: str
    message_id: str
    text: str
    occurred_at: datetime
    channel: str
    request_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            str(self.actor.actor_id),
            self.conversation_id,
            self.message_id,
            self.text.strip(),
            self.channel,
        )
        if not all(required):
            raise ValueError("runtime turn requires tenant, actor, conversation, message, text, and channel")
        if self.occurred_at.tzinfo is None:
            raise ValueError("runtime turn timestamp must be timezone-aware")
        object.__setattr__(self, "request_metadata", _frozen_mapping(self.request_metadata))


@dataclass(frozen=True)
class RuntimeReply:
    reply_type: RuntimeReplyType
    text: str


@dataclass(frozen=True)
class RuntimeTraceEvent:
    sequence: int
    stage: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _frozen_mapping(self.detail))


@dataclass(frozen=True)
class DomainExecutionResult:
    domain_id: str
    status: DomainExecutionStatus
    command_count: int
    actual_write: bool
    would_write: bool
    reply_text: str
    command_results: tuple[Mapping[str, Any], ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.actual_write and self.status != "succeeded":
            raise ValueError("only a succeeded live domain result may report an actual write")
        if self.actual_write and not self.would_write:
            raise ValueError("actual write implies write intent")
        object.__setattr__(self, "command_results", tuple(_frozen_mapping(item) for item in self.command_results))
        object.__setattr__(self, "data", _frozen_mapping(self.data))


@dataclass(frozen=True)
class RuntimeAuditRecord:
    status: RuntimeSuccessStatus
    run_id: str
    mode: RuntimeMode
    request: RuntimeTurnRequest
    context_manifest: Mapping[str, Any]
    decision: CognitiveDecisionV3
    command_plan: CognitiveCommandPlan
    domain_results: tuple[DomainExecutionResult, ...]
    state_before_version: int
    state_after_version: int
    actual_write: bool
    would_write: bool
    reply: RuntimeReply
    trace: tuple[RuntimeTraceEvent, ...]
    legacy_fallback_used: bool = False


@dataclass(frozen=True)
class RuntimeFailureAuditRecord:
    run_id: str
    mode: RuntimeMode
    status: Literal["failed_closed"]
    request: RuntimeTurnRequest
    context_manifest: Mapping[str, Any]
    failed_stage: str
    error_code: str
    decision: CognitiveDecisionV3 | None
    command_plan: CognitiveCommandPlan | None
    domain_results: tuple[DomainExecutionResult, ...]
    state_before_version: int | None
    state_after_version: int | None
    actual_write: bool
    would_write: bool
    reply: RuntimeReply
    trace: tuple[RuntimeTraceEvent, ...]
    legacy_fallback_used: bool = False


RuntimeAuditEvent = RuntimeAuditRecord | RuntimeFailureAuditRecord


class RuntimeAuditSink(Protocol):
    async def record(self, record: RuntimeAuditEvent) -> None: ...


class InMemoryRuntimeAuditSink:
    def __init__(self) -> None:
        self.records: list[RuntimeAuditEvent] = []

    async def record(self, record: RuntimeAuditEvent) -> None:
        self.records.append(record)


@dataclass(frozen=True)
class RuntimeTurnOutcome:
    run_id: str
    mode: RuntimeMode
    status: RuntimeSuccessStatus
    reply: RuntimeReply
    decision: CognitiveDecisionV3
    command_plan: CognitiveCommandPlan
    domain_results: tuple[DomainExecutionResult, ...]
    actual_write: bool
    would_write: bool
    state_version: int
    state: ConversationState
    trace: tuple[RuntimeTraceEvent, ...]
    legacy_fallback_used: bool = False


@dataclass(frozen=True)
class RuntimeFailureOutcome:
    run_id: str
    mode: RuntimeMode
    status: Literal["failed_closed"]
    reply: RuntimeReply
    failed_stage: str
    error_code: str
    decision: CognitiveDecisionV3 | None
    command_plan: CognitiveCommandPlan | None
    domain_results: tuple[DomainExecutionResult, ...]
    actual_write: bool
    would_write: bool
    state_version: int | None
    state: ConversationState | None
    trace: tuple[RuntimeTraceEvent, ...]
    legacy_fallback_used: bool = False


RuntimeOutcome = RuntimeTurnOutcome | RuntimeFailureOutcome
