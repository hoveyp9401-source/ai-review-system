from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal


DashboardRole = Literal["team_lead", "legal_head"]


@dataclass(frozen=True)
class DashboardActor:
    """Authenticated identity; authorization is resolved separately on the server."""

    tenant_id: str
    user_id: str


@dataclass(frozen=True)
class DashboardScope:
    """Effective dashboard authorization for one target date."""

    role: DashboardRole
    allowed_team_refs: tuple[str, ...] = ()

    @property
    def can_view_all_teams(self) -> bool:
        return self.role == "legal_head"


@dataclass(frozen=True)
class TeamRecord:
    ref: str
    name: str
    department_name: str = ""
    code: str = ""


@dataclass(frozen=True)
class MemberRecord:
    ref: str
    name: str
    team_ref: str


@dataclass(frozen=True)
class SubmissionObligation:
    member_ref: str
    team_ref: str
    report_date: date
    required: bool
    reason: str
    deadline_at: datetime | None
    data_complete: bool
    source: str = ""


@dataclass(frozen=True)
class DailyReportRecord:
    ref: str
    member_ref: str
    team_ref: str
    report_date: date
    status: str
    confirmation_type: str
    confirmed_by_user: bool
    today_work: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    tomorrow_plan: tuple[str, ...] = ()
    submitted_at: datetime | None = None
    raw_input: str = ""
    input_fragments: tuple[dict[str, object], ...] = ()
    section_status: dict[str, object] | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_date: date
    section: str
    quote: str


@dataclass(frozen=True)
class ReviewSuggestionRecord:
    ref: str
    member_ref: str
    team_ref: str
    report_date: date
    reason_type: str
    reason: str
    evidence: tuple[EvidenceRecord, ...]
    compared_dates: tuple[date, ...]
    confidence: float
    work_item_title: str
    support_needed: str
    owner_level: Literal["team_lead", "legal_head"]
    model_version: str
    active: bool = True
    evaluated_at: datetime | None = None


WorkItemStatus = Literal[
    "normal_progress",
    "no_new_progress",
    "plan_delayed",
    "unresolved_problem",
    "disappeared_without_completion",
    "completed",
    "waiting_external",
    "normal_continuing",
]


@dataclass(frozen=True)
class WorkItemEntryRecord:
    entry_date: date
    member_ref: str
    section: str
    quote: str
    object_text: str
    action: str
    result: str
    next_step: str
    blocker: str


@dataclass(frozen=True)
class WorkItemRecord:
    ref: str
    team_ref: str
    member_refs: tuple[str, ...]
    title: str
    status: WorkItemStatus
    summary: str
    first_seen: date
    last_seen: date
    entries: tuple[WorkItemEntryRecord, ...]
    confidence: float
    model_version: str
    active: bool = True
    evaluated_at: datetime | None = None


DecisionTargetType = Literal["review_suggestion", "work_item"]
ManagerDecision = Literal[
    "normal",
    "waiting_external",
    "followup",
    "completed",
    "system_error",
]


@dataclass(frozen=True)
class ManagementTarget:
    target_type: DecisionTargetType
    target_ref: str
    team_ref: str
    target_date: date
    evidence_snapshot: dict[str, Any]


@dataclass(frozen=True)
class ManagerDecisionRecord:
    ref: str
    tenant_id: str
    target_type: DecisionTargetType
    target_ref: str
    team_ref: str
    decision: ManagerDecision
    note: str
    actor_user_id: str
    actor_role: DashboardRole
    evidence_snapshot: dict[str, Any]
    idempotency_key: str
    created_at: datetime
    actor_name: str = ""


@dataclass(frozen=True)
class DashboardRecords:
    members: tuple[MemberRecord, ...] = ()
    obligations: tuple[SubmissionObligation, ...] = ()
    reports: tuple[DailyReportRecord, ...] = ()
    suggestions: tuple[ReviewSuggestionRecord, ...] = ()
    work_items: tuple[WorkItemRecord, ...] = ()
    decisions: tuple[ManagerDecisionRecord, ...] = ()
