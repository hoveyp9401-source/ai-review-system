from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from app.agent2.weekly_plan_suggestions import WeeklyPlanSuggestion

WeeklyPlanDayState = Literal["unfilled", "explicitly_empty", "planned"]
WeeklyPlanStatus = Literal[
    "collecting", "pending_confirmation", "submitted", "cancelled"
]


@dataclass(frozen=True)
class WeeklyPlanRosterMember:
    user_id: str
    display_name: str
    department_id: str = ""
    department_name: str = ""
    team_id: str = ""
    team_name: str = ""


@dataclass(frozen=True)
class WeeklyPlanBatch:
    batch_id: str
    tenant_id: str
    target_week_start: date
    roster: tuple[WeeklyPlanRosterMember, ...]
    created_at: datetime


@dataclass(frozen=True)
class WeeklyPlanItem:
    item_id: str
    original_text: str
    source: str
    created_at: datetime
    updated_at: datetime
    source_ref: str = ""


@dataclass(frozen=True)
class WeeklyPlanDay:
    day_id: str
    plan_date: date
    state: WeeklyPlanDayState = "unfilled"
    items: tuple[WeeklyPlanItem, ...] = ()


@dataclass(frozen=True)
class WeeklyPlan:
    plan_id: str
    batch_id: str
    tenant_id: str
    owner_user_id: str
    target_week_start: date
    status: WeeklyPlanStatus
    version: int
    days: tuple[WeeklyPlanDay, ...]
    suggestions: tuple[WeeklyPlanSuggestion, ...] = ()
    submitted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class WeeklyPlanCommand:
    command_id: str
    command_type: str
    tenant_id: str
    actor_user_id: str
    plan_id: str
    expected_version: int
    idempotency_key: str
    source_message_id: str
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WeeklyPlanReceipt:
    receipt_id: str
    tenant_id: str
    idempotency_key: str
    command_id: str
    command_type: str
    actor_user_id: str
    source_message_id: str
    request_sha256: str
    status: Literal["executed", "duplicate", "blocked"]
    reason_code: str
    plan_id: str
    actual_write: bool
    before_version: int
    after_version: int
    created_at: datetime


@dataclass(frozen=True)
class WeeklyPlanAuditEvent:
    audit_id: str
    tenant_id: str
    receipt_id: str
    plan_id: str
    actor_user_id: str
    command_type: str
    source_message_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class WeeklyPlanExecution:
    command: WeeklyPlanCommand
    before: WeeklyPlan
    after: WeeklyPlan
    receipt: WeeklyPlanReceipt
    audit_event: WeeklyPlanAuditEvent | None


@dataclass(frozen=True)
class WeeklyPlanMondayRow:
    user_id: str
    display_name: str
    department_id: str
    department_name: str
    team_id: str
    team_name: str
    plan_id: str
    plan_version: int
    plan_status: str
    days: tuple[dict[str, Any], ...]
    submitted_at: datetime | None = None


@dataclass(frozen=True)
class WeeklyPlanMondaySnapshot:
    snapshot_id: str
    batch_id: str
    tenant_id: str
    target_week_start: date
    as_of: datetime
    deadline_at: datetime
    roster_count: int
    submitted_count: int
    draft_count: int
    unfilled_count: int
    rows: tuple[WeeklyPlanMondayRow, ...]


@dataclass(frozen=True)
class WeeklyPlanMondayDeltaRow:
    user_id: str
    snapshot_plan_status: str
    current_plan_status: str
    submission_timing: Literal[
        "not_submitted", "on_time", "late", "closed_window"
    ]
    changed_after_snapshot: bool


@dataclass(frozen=True)
class WeeklyPlanMondayReconciliation:
    snapshot_id: str
    batch_id: str
    tenant_id: str
    target_week_start: date
    snapshot_as_of: datetime
    deadline_at: datetime
    reconciled_at: datetime
    roster_count: int
    snapshot_submitted_count: int
    current_submitted_count: int
    late_submitted_count: int
    closed_window_submitted_count: int
    changed_after_snapshot_count: int
    rows: tuple[WeeklyPlanMondayDeltaRow, ...]
