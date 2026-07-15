from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal, TypeAlias

from app.agent2.case_followup_commands import TriggerCaseFollowupNow, UpdateCaseFollowupPolicy


@dataclass(frozen=True)
class BusinessCommandContext:
    tenant_id: str
    company_id: str
    department_id: str
    team_id: str
    actor_user_id: str
    actor_role_ids: tuple[str, ...]
    allowed_case_ids: tuple[str, ...]
    source_message_id: str
    source_channel: str
    occurred_at: datetime
    writable_case_ids: tuple[str, ...] | None = None
    conversation_id: str = ""
    execution_started_at: datetime | None = None
    admission_ticket: dict[str, Any] = field(default_factory=dict)
    admission_required: bool = False
    admission_action_id: str = ""
    admission_operation: str = ""
    conversation_state_version: int | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "actor_user_id", "source_message_id", "source_channel"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        if self.execution_started_at is not None and self.execution_started_at.tzinfo is None:
            raise ValueError("execution_started_at must be timezone-aware")
        if self.conversation_state_version is not None and self.conversation_state_version < 0:
            raise ValueError("conversation_state_version must be non-negative")
        if self.writable_case_ids is not None and not set(
            self.writable_case_ids
        ).issubset(set(self.allowed_case_ids)):
            raise ValueError("writable_case_ids must be a subset of allowed_case_ids")

    def can_create_case_progress(
        self,
        case_id: str,
        *,
        owner_user_id: str,
    ) -> bool:
        """Authorize a new progress record without conflating visibility and write access.

        Existing bindings that do not declare ``writable_case_ids`` retain the
        historical owner-only behavior.  A binding may explicitly grant a
        collaborator create access while the progress reporter remains the
        actual actor for audit and later edit/delete ownership.
        """

        if case_id not in set(self.allowed_case_ids):
            return False
        if self.writable_case_ids is None:
            return owner_user_id == self.actor_user_id
        return case_id in set(self.writable_case_ids)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CreateTravelIntent:
    command_id: str
    destination_raw: str
    destination_normalized: str
    city_code: str
    province_code: str
    start_at: datetime
    end_at: datetime
    time_precision: str
    purpose_summary: str
    related_case_ids: tuple[str, ...]
    confidence: float
    command_type: Literal["create_travel_intent"] = "create_travel_intent"


@dataclass(frozen=True)
class UpdateTravelIntent:
    command_id: str
    travel_intent_id: str
    expected_version: int
    start_at: datetime | None = None
    end_at: datetime | None = None
    destination_normalized: str | None = None
    city_code: str | None = None
    status: Literal["proposed", "planned", "confirmed", "changed", "cancelled", "completed"] | None = None
    command_type: Literal["update_travel_intent"] = "update_travel_intent"


@dataclass(frozen=True)
class RespondTravelCollaboration:
    command_id: str
    candidate_id: str
    response: Literal["accept", "decline", "later", "changed", "cancel"]
    expected_version: int | None = None
    command_type: Literal["respond_travel_collaboration"] = "respond_travel_collaboration"


@dataclass(frozen=True)
class CreateCaseProgress:
    command_id: str
    case_id: str
    occurred_at: datetime
    progress_type: str
    summary: str
    details: str
    related_party_ids: tuple[str, ...]
    related_document_ids: tuple[str, ...]
    related_travel_intent_ids: tuple[str, ...]
    confidence: float
    followup_notification_id: str = ""
    lifecycle_stage: str = ""
    lifecycle_node: str = ""
    current_status: str = ""
    next_actions: tuple[str, ...] = ()
    hearing_readiness: str = ""
    blocking_issues: tuple[str, ...] = ()
    command_type: Literal["create_case_progress"] = "create_case_progress"


@dataclass(frozen=True)
class SnoozeCaseFollowup:
    command_id: str
    pending_id: str
    case_id: str
    snoozed_until: datetime
    command_type: Literal["snooze_case_followup"] = "snooze_case_followup"


@dataclass(frozen=True)
class UpdateCaseProgress:
    command_id: str
    progress_id: str
    expected_version: int
    summary: str | None
    details: str | None
    command_type: Literal["update_case_progress"] = "update_case_progress"


@dataclass(frozen=True)
class DeleteCaseProgress:
    command_id: str
    progress_id: str
    expected_version: int
    reason: str
    command_type: Literal["delete_case_progress"] = "delete_case_progress"


@dataclass(frozen=True)
class QueryCaseProgress:
    command_id: str
    case_id: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    command_type: Literal["query_case_progress"] = "query_case_progress"


@dataclass(frozen=True)
class ListAssignedCases:
    command_id: str
    include_closed: bool = True
    command_type: Literal["list_assigned_cases"] = "list_assigned_cases"


@dataclass(frozen=True)
class QueryOperationStatus:
    command_id: str
    domain: Literal["case_progress", "travel"]
    command_type: Literal["query_operation_status"] = "query_operation_status"


@dataclass(frozen=True)
class QueryPartyCases:
    command_id: str
    party_id: str
    match_basis: str
    role_type: str = ""
    include_recent_progress: bool = True
    command_type: Literal["query_party_cases"] = "query_party_cases"


@dataclass(frozen=True)
class LinkCaseProgress:
    command_id: str
    progress_id: str
    expected_version: int
    related_party_ids: tuple[str, ...] = ()
    related_document_ids: tuple[str, ...] = ()
    related_travel_intent_ids: tuple[str, ...] = ()
    command_type: Literal["link_case_progress"] = "link_case_progress"


BusinessCommand: TypeAlias = (
    CreateTravelIntent
    | UpdateTravelIntent
    | RespondTravelCollaboration
    | CreateCaseProgress
    | SnoozeCaseFollowup
    | UpdateCaseProgress
    | DeleteCaseProgress
    | QueryCaseProgress
    | ListAssignedCases
    | QueryOperationStatus
    | QueryPartyCases
    | LinkCaseProgress
    | UpdateCaseFollowupPolicy
    | TriggerCaseFollowupNow
)


BUSINESS_COMMAND_TYPES = (
    CreateTravelIntent,
    UpdateTravelIntent,
    RespondTravelCollaboration,
    CreateCaseProgress,
    SnoozeCaseFollowup,
    UpdateCaseProgress,
    DeleteCaseProgress,
    QueryCaseProgress,
    ListAssignedCases,
    QueryOperationStatus,
    QueryPartyCases,
    LinkCaseProgress,
    UpdateCaseFollowupPolicy,
    TriggerCaseFollowupNow,
)


def business_command_fingerprint(command: BusinessCommand) -> str:
    """Hash stable command semantics while excluding model-generated identifiers."""

    if not isinstance(command, BUSINESS_COMMAND_TYPES):
        raise TypeError("typed business command only")
    payload = asdict(command)
    payload.pop("command_id", None)
    if isinstance(command, CreateCaseProgress):
        # Retries of one source message can be reinterpreted seconds later and
        # receive different model confidence/stage labels.  Those values must
        # not turn one asserted progress statement into a second DB record.
        payload.pop("occurred_at", None)
        payload.pop("progress_type", None)
        payload.pop("confidence", None)
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_fingerprint_json_default,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def travel_intent_fact_fingerprint(command: CreateTravelIntent) -> str:
    """Hash the conservative business identity of one travel intent.

    Transport/message identifiers, model confidence and the raw location wording
    are deliberately excluded.  The normalized city, exact travel window and
    user-authored purpose remain material, so nearby but distinct trips are not
    collapsed.  Tenant and actor fencing are added by the executor.
    """

    if not isinstance(command, CreateTravelIntent):
        raise TypeError("CreateTravelIntent required")
    return travel_intent_fact_fingerprint_fields(
        city_code=command.city_code,
        start_at=command.start_at,
        end_at=command.end_at,
        time_precision=command.time_precision,
        purpose_summary=command.purpose_summary,
    )


def travel_intent_fact_fingerprint_fields(
    *,
    city_code: str,
    start_at: datetime,
    end_at: datetime,
    time_precision: str,
    purpose_summary: str,
) -> str:
    material = json.dumps(
        {
            "city_code": city_code.strip(),
            "start_at": _fingerprint_json_default(start_at),
            "end_at": _fingerprint_json_default(end_at),
            "time_precision": time_precision.strip().lower(),
            "purpose_summary": normalize_travel_fact_text(purpose_summary),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_travel_fact_text(value: str) -> str:
    """Normalize representation only; never strengthen travel business facts."""

    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "")).strip()


def _fingerprint_json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).isoformat()
        return value.isoformat()
    raise TypeError(f"unsupported business command fingerprint value: {type(value).__name__}")


@dataclass(frozen=True)
class BusinessReceipt:
    receipt_id: str
    command_id: str
    command_type: str
    tenant_id: str
    actor_user_id: str
    source_message_id: str
    idempotency_key: str
    status: Literal["executed", "duplicate", "blocked", "failed"]
    resource_type: str
    resource_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    error_code: str | None
    failed_stage: str | None
    actual_write: bool
    created_at: datetime


@dataclass(frozen=True)
class BusinessAuditEntry:
    audit_id: str
    receipt_id: str
    tenant_id: str
    actor_user_id: str
    source_message_id: str
    source_channel: str
    command_type: str
    resource_type: str
    resource_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    occurred_at: datetime


class BusinessCommandError(RuntimeError):
    def __init__(self, code: str, stage: str, message: str):
        super().__init__(message)
        self.code = code
        self.stage = stage
