from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent2.memory import TrustedPersonalMemoryContext
from app.agent2.periodic_report_context import TrustedPeriodicReportContext
from app.agent2.weekly_plan_context import TrustedWeeklyPlanContext

SHADOW_STATE_NAMESPACE = "agent2.tool_calling.shadow.v1"
CANARY_STATE_NAMESPACE = "agent2.tool_calling.canary.v1"
ToolCallStateNamespace = Literal[
    "agent2.tool_calling.shadow.v1",
    "agent2.tool_calling.canary.v1",
]
ReportField = Literal["today_work", "problems", "tomorrow_plan"]
ReportStatus = Literal[
    "collecting",
    "pending_confirmation",
    "completed",
    "skipped",
    "cancelled",
]
ResourceProvenance = Literal["trusted_context", "read_tool"]
ReceiptStatus = Literal[
    "success",
    "no_op",
    "blocked",
    "clarification_required",
    "failed",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TrustedPrincipal(_FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    conversation_id: str = Field(min_length=1, max_length=256)
    source_message_id: str = Field(min_length=1, max_length=512)
    timezone: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    conversation_kind: Literal["direct", "group", "unknown"] = "unknown"


class TrustedReportItem(_FrozenModel):
    item_id: str = Field(min_length=1, max_length=256)
    field: ReportField
    content: str = Field(min_length=1, max_length=4000)
    report_id: UUID
    report_version: int = Field(ge=0)
    provenance: ResourceProvenance = "trusted_context"


class TrustedDateCorrectionReference(_FrozenModel):
    """Server-owned evidence of the latest atomic date relocation."""

    report_id: UUID
    source_message_id: str = Field(min_length=1, max_length=512)
    source_report_date: date
    target_report_date: date


class TrustedReportSnapshot(_FrozenModel):
    report_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    owner_user_id: UUID
    report_date: date
    version: int = Field(ge=0)
    status: ReportStatus
    items: tuple[TrustedReportItem, ...] = ()
    acknowledged_empty_fields: frozenset[ReportField] = frozenset()
    date_correction_reference: TrustedDateCorrectionReference | None = None
    provenance: ResourceProvenance = "trusted_context"

    @model_validator(mode="after")
    def item_bindings_match_snapshot(self) -> "TrustedReportSnapshot":
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("trusted report item IDs must be unique")
        if any(
            item.report_id != self.report_id or item.report_version != self.version
            for item in self.items
        ):
            raise ValueError("trusted item binding must match report ID and version")
        if any(
            item.field in self.acknowledged_empty_fields for item in self.items
        ):
            raise ValueError(
                "a trusted report field cannot contain items and be acknowledged empty"
            )
        reference = self.date_correction_reference
        if reference is not None and (
            reference.report_id != self.report_id
            or reference.target_report_date != self.report_date
            or reference.source_report_date == reference.target_report_date
        ):
            raise ValueError(
                "trusted date-correction evidence must bind this exact report and date"
            )
        return self

    def item(self, item_id: str) -> TrustedReportItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)

    def safe_snapshot(self) -> dict[str, Any]:
        fields = {name: [] for name in ("today_work", "problems", "tomorrow_plan")}
        for item in self.items:
            fields[item.field].append(
                {"item_id": item.item_id, "content": item.content, "provenance": item.provenance}
            )
        return {
            "report_id": str(self.report_id),
            "report_date": self.report_date.isoformat(),
            "version": self.version,
            "status": self.status,
            "fields": fields,
            "acknowledged_empty_fields": sorted(
                self.acknowledged_empty_fields
            ),
            "provenance": self.provenance,
        }

    @property
    def state_sha256(self) -> str:
        canonical = json.dumps(
            self.safe_snapshot(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TrustedClearPending(_FrozenModel):
    pending_id: UUID
    namespace: ToolCallStateNamespace = SHADOW_STATE_NAMESPACE
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    conversation_id: str = Field(min_length=1, max_length=256)
    report_id: UUID
    report_version: int = Field(ge=0)
    target_date: date
    expires_at: datetime
    source_message_id: str = Field(min_length=1, max_length=512)
    consumed: bool = False

    @field_validator("expires_at")
    @classmethod
    def expiry_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("pending expiry must be timezone-aware")
        return value


class TrustedDailyWriteRetryCandidate(_FrozenModel):
    """One server-verified Daily write that the immediately next turn may retry."""

    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    conversation_id: str = Field(min_length=1, max_length=256)
    origin_source_message_id: str = Field(min_length=1, max_length=512)
    origin_received_at: datetime
    source_messages: tuple[str, ...] = Field(min_length=1, max_length=1)
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_date: date
    target_was_absent: bool
    target_version: int | None = Field(default=None, ge=0)
    target_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_local_date: date
    retry_chain_depth: int = Field(default=0, ge=0, le=3)
    provenance: Literal["server_block_observation"] = (
        "server_block_observation"
    )

    @field_validator("origin_received_at")
    @classmethod
    def origin_time_must_be_timezone_aware(
        cls,
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Daily retry origin time must be timezone-aware")
        return value

    @field_validator("source_messages")
    @classmethod
    def source_messages_must_be_complete(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 12000
            for message in value
        ):
            raise ValueError("Daily retry source message is invalid")
        return value

    @model_validator(mode="after")
    def target_existence_matches_version(
        self,
    ) -> "TrustedDailyWriteRetryCandidate":
        if self.target_was_absent != (self.target_version is None):
            raise ValueError(
                "Daily retry target absence must match its recorded version"
            )
        return self

    def model_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "target_report_date": self.target_date.isoformat(),
            "status": "previous_write_not_applied",
            "source_messages": [
                {"sequence": index, "content": content}
                for index, content in enumerate(
                    self.source_messages,
                    start=1,
                )
            ],
            "provenance": self.provenance,
        }


class TrustedRecentMessage(_FrozenModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)
    source_message_id: str = Field(min_length=1, max_length=512)
    source_turn_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
    )
    read_snapshot_verified: bool = False
    fact_time_scope: Literal["past_snapshot"] | None = None
    delivery_status: Literal[
        "verified",
        "accepted_unverified",
        "delivery_failed",
        "failed",
        "unknown",
    ] = Field(default="unknown", exclude=True)

    @model_validator(mode="after")
    def only_assistant_replies_can_be_read_snapshots(
        self,
    ) -> TrustedRecentMessage:
        if self.role != "assistant" and (
            self.read_snapshot_verified or self.fact_time_scope is not None
        ):
            raise ValueError(
                "only an assistant reply can carry a verified read snapshot"
            )
        if self.read_snapshot_verified and self.source_turn_id is None:
            raise ValueError(
                "a verified read snapshot requires a trusted source turn"
            )
        if self.role != "assistant" and self.delivery_status != "unknown":
            raise ValueError(
                "only an assistant reply can carry delivery evidence"
            )
        return self


class TrustedReportReference(_FrozenModel):
    """A server-verified report pointer preserved by a prior tool receipt."""

    report_id: UUID
    report_date: date
    report_version: int = Field(ge=0)
    report_status: ReportStatus
    report_state_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provenance: Literal["server_receipt"] = "server_receipt"


class TrustedRecentOperation(_FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    conversation_id: str = Field(min_length=1, max_length=256)
    source_message_id: str = Field(min_length=1, max_length=512)
    tool_call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=128)
    status: ReceiptStatus
    changed: bool
    target_type: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=256)
    before_version: int | None = Field(default=None, ge=0)
    after_version: int | None = Field(default=None, ge=0)
    affected_item_ids: tuple[str, ...] = ()
    report_reference: TrustedReportReference | None = None
    occurred_at: datetime
    provenance: Literal["server_receipt"] = "server_receipt"

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recent operation time must be timezone-aware")
        return value

    @field_validator("affected_item_ids")
    @classmethod
    def affected_item_ids_must_be_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not item or len(item) > 256 for item in value):
            raise ValueError("recent operation item IDs must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("recent operation item IDs must be unique")
        return value

    def model_payload(
        self,
        *,
        item_contents: dict[str, str],
    ) -> dict[str, Any]:
        payload = {
            "tool_name": self.tool_name,
            "status": self.status,
            "changed": self.changed,
            "target_type": self.target_type,
            "affected_items": [
                item_contents[item_id]
                for item_id in self.affected_item_ids
                if item_id in item_contents
            ],
            "affected_item_count": len(self.affected_item_ids),
            "occurred_at": self.occurred_at.isoformat(),
            "provenance": self.provenance,
        }
        if self.report_reference is not None:
            payload["report_reference"] = self.report_reference.model_dump(
                mode="json",
                exclude={"report_state_sha256"},
            )
        return payload


class TrustedRuntimeIdentity(_FrozenModel):
    provider_name: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    provenance: Literal["server_runtime"] = "server_runtime"


class TrustedContext(_FrozenModel):
    namespace: ToolCallStateNamespace = SHADOW_STATE_NAMESPACE
    now: datetime
    principal: TrustedPrincipal
    runtime_identity: TrustedRuntimeIdentity | None = None
    today_report: TrustedReportSnapshot | None = None
    historical_reports: tuple[TrustedReportSnapshot, ...] = ()
    active_clear_pending: TrustedClearPending | None = None
    retryable_daily_write: TrustedDailyWriteRetryCandidate | None = None
    recent_messages: tuple[TrustedRecentMessage, ...] = ()
    recent_operations: tuple[TrustedRecentOperation, ...] = ()
    personal_memory: TrustedPersonalMemoryContext | None = None
    current_weekly_report: TrustedPeriodicReportContext | None = None
    weekly_plan: TrustedWeeklyPlanContext | None = None
    weekly_plans: tuple[TrustedWeeklyPlanContext, ...] = ()
    business_glossary: dict[str, str] = Field(default_factory=dict)
    allowed_tool_names: frozenset[str] = frozenset()
    gate_decisions: dict[str, bool] = Field(default_factory=dict)
    assembly_warnings: tuple[str, ...] = ()

    @field_validator("now")
    @classmethod
    def now_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("trusted current time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def resources_match_principal_and_registry(self) -> "TrustedContext":
        from app.agent2.tool_calling.contracts import ExecutionMode
        from app.agent2.tool_calling.registry import TOOL_REGISTRY

        reports = self.all_reports()
        if any(
            report.tenant_id != self.principal.tenant_id
            or report.owner_user_id != self.principal.user_id
            for report in reports
        ):
            raise ValueError("trusted reports must match the authenticated principal")
        report_ids = [report.report_id for report in reports]
        report_dates = [report.report_date for report in reports]
        if len(report_ids) != len(set(report_ids)):
            raise ValueError("trusted report IDs must be unique")
        if len(report_dates) != len(set(report_dates)):
            raise ValueError("trusted report dates must be unique")
        local_today = self.now.astimezone(ZoneInfo(self.principal.timezone)).date()
        if self.today_report is not None and self.today_report.report_date != local_today:
            raise ValueError("today report date must match authoritative user-local today")
        if not self.allowed_tool_names.issubset(TOOL_REGISTRY):
            raise ValueError("allowed tool names must come from the central registry")
        if not set(self.gate_decisions).issubset(TOOL_REGISTRY):
            raise ValueError("gate decisions must come from the central registry")
        execution_mode = (
            ExecutionMode.CANARY_EXECUTE
            if self.namespace == CANARY_STATE_NAMESPACE
            else ExecutionMode.SHADOW_PROPOSAL
        )
        if any(
            execution_mode not in TOOL_REGISTRY[name].enabled_modes
            for name in (
                self.allowed_tool_names | set(self.gate_decisions)
            )
        ):
            raise ValueError(
                "trusted context cannot expose a tool outside its execution mode"
            )
        operation_keys = [
            (item.source_message_id, item.tool_call_id)
            for item in self.recent_operations
        ]
        if len(operation_keys) != len(set(operation_keys)):
            raise ValueError("trusted recent operations must be unique")
        if any(
            item.tenant_id != self.principal.tenant_id
            or item.user_id != self.principal.user_id
            or item.conversation_id != self.principal.conversation_id
            or item.tool_name not in TOOL_REGISTRY
            for item in self.recent_operations
        ):
            raise ValueError(
                "trusted recent operations must match the principal and registry"
            )
        memory = self.personal_memory
        if memory is not None and any(
            entry.tenant_id != self.principal.tenant_id
            or entry.user_id != self.principal.user_id
            for entry in memory.entries
        ):
            raise ValueError(
                "trusted personal memory must match the authenticated principal"
            )
        periodic = self.current_weekly_report
        if periodic is not None and (
            periodic.tenant_id != self.principal.tenant_id
            or periodic.owner_user_id != self.principal.user_id
            or periodic.report_type != "weekly"
        ):
            raise ValueError(
                "trusted current weekly report must match the authenticated principal"
            )
        if self.weekly_plan is not None and self.weekly_plans and (
            self.weekly_plan != self.weekly_plans[0]
        ):
            raise ValueError(
                "trusted weekly-plan compatibility target must match the first target"
            )
        all_weekly_contexts = self.all_weekly_plans()
        weekly_plan_ids = [item.plan_id for item in all_weekly_contexts]
        if len(weekly_plan_ids) != len(set(weekly_plan_ids)):
            raise ValueError("trusted weekly-plan target IDs must be unique")
        if any(
            weekly_plan.tenant_id != self.principal.tenant_id
            or weekly_plan.owner_user_id != str(self.principal.user_id)
            for weekly_plan in all_weekly_contexts
        ):
            raise ValueError(
                "trusted weekly plans must match the authenticated principal"
            )
        pending = self.active_clear_pending
        if pending is not None and (
            pending.tenant_id != self.principal.tenant_id
            or pending.user_id != self.principal.user_id
            or pending.conversation_id != self.principal.conversation_id
        ):
            raise ValueError("trusted Pending must match principal and conversation")
        retry = self.retryable_daily_write
        if retry is not None:
            local_date = self.now.astimezone(
                ZoneInfo(self.principal.timezone)
            ).date()
            if (
                retry.tenant_id != self.principal.tenant_id
                or retry.user_id != self.principal.user_id
                or retry.conversation_id
                != self.principal.conversation_id
                or retry.failed_local_date != local_date
                or retry.origin_received_at > self.now
                or self.now - retry.origin_received_at
                > timedelta(hours=2)
            ):
                raise ValueError(
                    "trusted Daily retry candidate must match the current scope"
                )
        return self

    def all_reports(self) -> tuple[TrustedReportSnapshot, ...]:
        current = (self.today_report,) if self.today_report is not None else ()
        return (*current, *self.historical_reports)

    def all_weekly_plans(self) -> tuple[TrustedWeeklyPlanContext, ...]:
        if self.weekly_plans:
            return self.weekly_plans
        return (self.weekly_plan,) if self.weekly_plan is not None else ()

    def weekly_plan_by_id(self, plan_id: str) -> TrustedWeeklyPlanContext | None:
        return next(
            (item for item in self.all_weekly_plans() if item.plan_id == plan_id),
            None,
        )

    def report_by_id(self, report_id: UUID) -> TrustedReportSnapshot | None:
        return next((item for item in self.all_reports() if item.report_id == report_id), None)

    def report_by_date(self, report_date: date) -> TrustedReportSnapshot | None:
        return next((item for item in self.all_reports() if item.report_date == report_date), None)

    def model_payload(self) -> dict[str, Any]:
        from app.agent2.tool_calling.reporting_date import (
            MORNING_DAILY_CUTOFF,
            default_daily_write_date,
        )

        pending = self.active_clear_pending
        item_contents = {
            item.item_id: item.content
            for report in self.all_reports()
            for item in report.items
        }
        local_now = self.now.astimezone(ZoneInfo(self.principal.timezone))
        default_report_date = default_daily_write_date(
            now=self.now,
            timezone=self.principal.timezone,
        )
        safe_date_candidates = tuple(
            dict.fromkeys((default_report_date, local_now.date()))
        )
        payload = {
            "current_time": self.now.isoformat(),
            "timezone": self.principal.timezone,
            "daily_reporting_context": {
                "local_date": local_now.date().isoformat(),
                "local_time": local_now.isoformat(),
                "default_report_date": default_report_date.isoformat(),
                "morning_cutoff": MORNING_DAILY_CUTOFF.strftime("%H:%M"),
                "default_is_prior_not_lock": True,
                "safe_semantic_date_candidates": [
                    item.isoformat() for item in safe_date_candidates
                ],
            },
            "today_report": self.today_report.safe_snapshot() if self.today_report else None,
            "historical_reports": [item.safe_snapshot() for item in self.historical_reports],
            "active_clear_pending": (
                {
                    "exists": True,
                    "report_id": str(pending.report_id),
                    "report_version": pending.report_version,
                    "target_date": pending.target_date.isoformat(),
                    "expires_at": pending.expires_at.isoformat(),
                    "provenance": "server_pending",
                }
                if pending is not None
                else None
            ),
            "retryable_daily_write": (
                self.retryable_daily_write.model_payload()
                if self.retryable_daily_write is not None
                else None
            ),
            "recent_messages": [
                item.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude={
                        "source_turn_id",
                        "read_snapshot_verified",
                    },
                )
                for item in self.recent_messages
            ],
            "recent_operations": [
                item.model_payload(item_contents=item_contents)
                for item in self.recent_operations
            ],
            "resource_namespace": self.namespace,
        }
        if self.runtime_identity is not None:
            payload["runtime_identity"] = self.runtime_identity.model_dump(
                mode="json"
            )
        if self.principal.display_name is not None:
            payload["authenticated_user"] = {
                "display_name": self.principal.display_name,
                "provenance": "server_identity",
            }
        if self.personal_memory is not None and self.personal_memory.entries:
            payload["personal_memory"] = self.personal_memory.model_payload()
        if self.current_weekly_report is not None:
            payload["current_weekly_report"] = (
                self.current_weekly_report.safe_snapshot()
            )
        if self.weekly_plan is not None:
            payload["weekly_plan"] = self.weekly_plan.model_payload()
        if self.weekly_plans:
            payload["weekly_plan_targets"] = [
                item.model_payload() for item in self.weekly_plans
            ]
        if self.business_glossary:
            payload["business_glossary"] = dict(
                self.business_glossary
            )
        return payload
