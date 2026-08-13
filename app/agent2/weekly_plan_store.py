from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from threading import RLock
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    and_,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent2.weekly_plan_context_loader import OpenWeeklyPlanBatchRef
from app.agent2.weekly_plan_domain import (
    _command_batch_sha256,
    _stable_id,
    execute_weekly_plan_batch,
    execute_weekly_plan_command,
    weekly_plan_submission_timing,
)
from app.agent2.weekly_plan_domain import _execution as _domain_execution
from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanAuditEvent,
    WeeklyPlanBatch,
    WeeklyPlanCommand,
    WeeklyPlanDay,
    WeeklyPlanExecution,
    WeeklyPlanItem,
    WeeklyPlanMondayDeltaRow,
    WeeklyPlanMondayReconciliation,
    WeeklyPlanMondayRow,
    WeeklyPlanMondaySnapshot,
    WeeklyPlanReceipt,
    WeeklyPlanRosterMember,
)


class InMemoryWeeklyPlanStore:
    """Thread-safe authoritative store for deterministic tests and replay."""

    def __init__(self) -> None:
        self._batches: dict[tuple[str, str], WeeklyPlanBatch] = {}
        self._batch_statuses: dict[tuple[str, str], str] = {}
        self._plans: dict[tuple[str, str], WeeklyPlan] = {}
        self._receipts: dict[tuple[str, str], WeeklyPlanReceipt] = {}
        self._audits: list[WeeklyPlanAuditEvent] = []
        self._monday_snapshots: dict[tuple[str, str], WeeklyPlanMondaySnapshot] = {}
        self._lock = RLock()

    def save_batch(self, batch: WeeklyPlanBatch) -> WeeklyPlanBatch:
        key = (batch.tenant_id, batch.batch_id)
        with self._lock:
            existing = self._batches.get(key)
            if existing is not None and existing != batch:
                raise ValueError("batch_id_collision")
            self._batches[key] = deepcopy(batch)
            self._batch_statuses.setdefault(key, "collecting")
            return deepcopy(batch)

    def load_open_target_week_for_owner(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start,
    ):
        if preferred_target_week_start.weekday() != 0:
            raise ValueError("preferred weekly-plan target must start on Monday")
        with self._lock:
            rows = [
                batch.target_week_start
                for (batch_tenant, _), batch in self._batches.items()
                if batch_tenant == tenant_id
                and batch.target_week_start == preferred_target_week_start
                and self._batch_statuses.get((batch_tenant, batch.batch_id))
                in {"collecting", "snapshotted"}
                and owner_user_id in {member.user_id for member in batch.roster}
            ]
            if len(rows) > 1:
                raise ValueError("multiple_open_weekly_plan_batches")
            return rows[0] if rows else None

    def load_open_batch_ref_for_owner(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start,
    ) -> OpenWeeklyPlanBatchRef | None:
        if preferred_target_week_start.weekday() != 0:
            raise ValueError("preferred weekly-plan target must start on Monday")
        with self._lock:
            rows = [
                OpenWeeklyPlanBatchRef(
                    batch_id=batch.batch_id,
                    target_week_start=batch.target_week_start,
                )
                for (batch_tenant, _), batch in self._batches.items()
                if batch_tenant == tenant_id
                and batch.target_week_start == preferred_target_week_start
                and self._batch_statuses.get((batch_tenant, batch.batch_id))
                in {"collecting", "snapshotted"}
                and owner_user_id in {member.user_id for member in batch.roster}
            ]
            if len(rows) > 1:
                raise ValueError("multiple_open_weekly_plan_batches")
            return rows[0] if rows else None

    def load_plan_by_owner_week(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        target_week_start,
        for_update: bool = False,
    ) -> WeeklyPlan | None:
        del for_update
        with self._lock:
            plans = [
                plan
                for (plan_tenant, _), plan in self._plans.items()
                if plan_tenant == tenant_id
                and plan.owner_user_id == owner_user_id
                and plan.target_week_start == target_week_start
            ]
            if len(plans) > 1:
                raise ValueError("multiple_weekly_plans_for_owner_week")
            return deepcopy(plans[0]) if plans else None

    def save_plan(
        self, plan: WeeklyPlan, *, expected_version: int | None = None
    ) -> WeeklyPlan:
        key = (plan.tenant_id, plan.plan_id)
        with self._lock:
            current = self._plans.get(key)
            current_version = current.version if current is not None else 0
            if expected_version is not None and current_version != expected_version:
                raise ValueError("version_conflict")
            if current is not None and plan.owner_user_id != current.owner_user_id:
                raise ValueError("owner_mismatch")
            if current is not None and plan.batch_id != current.batch_id:
                raise ValueError("batch_mismatch")
            self._plans[key] = deepcopy(plan)
            return deepcopy(plan)

    def load_plan(
        self, *, tenant_id: str, plan_id: str, owner_user_id: str
    ) -> WeeklyPlan | None:
        with self._lock:
            plan = self._plans.get((tenant_id, plan_id))
            if plan is None or plan.owner_user_id != owner_user_id:
                return None
            return deepcopy(plan)

    def executed_idempotency_keys(self, *, tenant_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(
                key
                for (receipt_tenant, key), receipt in self._receipts.items()
                if receipt_tenant == tenant_id and receipt.status == "executed"
            )

    def append_receipt(self, receipt: WeeklyPlanReceipt) -> WeeklyPlanReceipt:
        key = (receipt.tenant_id, receipt.idempotency_key)
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                return deepcopy(existing)
            self._receipts[key] = deepcopy(receipt)
            return deepcopy(receipt)

    def append_audit(self, audit: WeeklyPlanAuditEvent) -> None:
        with self._lock:
            if any(item.audit_id == audit.audit_id for item in self._audits):
                return
            self._audits.append(deepcopy(audit))

    def execute(
        self, command: WeeklyPlanCommand, *, executed_at: datetime
    ) -> WeeklyPlanExecution:
        with self._lock:
            existing = self._receipts.get(
                (command.tenant_id, command.idempotency_key)
            )
            plan = self._plans.get((command.tenant_id, command.plan_id))
            if plan is None:
                raise ValueError("plan_not_found")
            if existing is not None:
                if (
                    existing.command_id != command.command_id
                    or existing.command_type != command.command_type
                    or existing.actor_user_id != command.actor_user_id
                    or existing.plan_id != command.plan_id
                    or existing.source_message_id != command.source_message_id
                    or existing.request_sha256 != _receipt_request_hash(command)
                ):
                    blocked = execute_weekly_plan_command(
                        command,
                        plan=plan,
                        executed_at=executed_at,
                    )
                    receipt = replace(
                        blocked.receipt,
                        status="blocked",
                        reason_code="idempotency_scope_conflict",
                        actual_write=False,
                        after_version=plan.version,
                    )
                    return WeeklyPlanExecution(
                        command=command,
                        before=plan,
                        after=plan,
                        receipt=receipt,
                        audit_event=None,
                    )
                duplicate = execute_weekly_plan_command(
                    command,
                    plan=plan,
                    executed_at=executed_at,
                    executed_idempotency_keys=frozenset({command.idempotency_key}),
                )
                return deepcopy(duplicate)
            execution = execute_weekly_plan_command(
                command,
                plan=plan,
                executed_at=executed_at,
                executed_idempotency_keys=self.executed_idempotency_keys(
                    tenant_id=command.tenant_id
                ),
            )
            if execution.receipt.actual_write:
                self._plans[(command.tenant_id, command.plan_id)] = deepcopy(
                    execution.after
                )
            self._receipts[
                (command.tenant_id, command.idempotency_key)
            ] = deepcopy(execution.receipt)
            if execution.audit_event is not None:
                self._audits.append(deepcopy(execution.audit_event))
            return deepcopy(execution)

    def build_monday_snapshot(
        self,
        *,
        tenant_id: str,
        batch_id: str,
        as_of: datetime,
        deadline_at: datetime,
    ) -> WeeklyPlanMondaySnapshot:
        key = (tenant_id, batch_id)
        with self._lock:
            existing = self._monday_snapshots.get(key)
            if existing is not None:
                return deepcopy(existing)
            batch = self._batches.get(key)
            if batch is None:
                raise ValueError("batch_not_found")
            plans_by_owner = {
                plan.owner_user_id: plan
                for (plan_tenant, _), plan in self._plans.items()
                if plan_tenant == tenant_id and plan.batch_id == batch_id
            }
            rows = []
            for member in batch.roster:
                plan = plans_by_owner.get(member.user_id)
                plan_status = _snapshot_plan_status(plan)
                rows.append(
                    WeeklyPlanMondayRow(
                        user_id=member.user_id,
                        display_name=member.display_name,
                        department_id=member.department_id,
                        department_name=member.department_name,
                        team_id=member.team_id,
                        team_name=member.team_name,
                        plan_id=plan.plan_id if plan else "",
                        plan_version=plan.version if plan else 0,
                        plan_status=plan_status,
                        submitted_at=plan.submitted_at if plan else None,
                        days=_days_payload(plan),
                    )
                )
            submitted = sum(row.plan_status == "submitted" for row in rows)
            draft = sum(
                row.plan_status in {"collecting", "pending_confirmation"}
                for row in rows
            )
            unfilled = sum(row.plan_status == "unfilled" for row in rows)
            snapshot = WeeklyPlanMondaySnapshot(
                snapshot_id=_stable_id(
                    "weekly-plan-monday-snapshot", tenant_id, batch_id
                ),
                batch_id=batch_id,
                tenant_id=tenant_id,
                target_week_start=batch.target_week_start,
                as_of=as_of,
                deadline_at=deadline_at,
                roster_count=len(batch.roster),
                submitted_count=submitted,
                draft_count=draft,
                unfilled_count=unfilled,
                rows=tuple(rows),
            )
            self._monday_snapshots[key] = snapshot
            self._batch_statuses[key] = "snapshotted"
            return deepcopy(snapshot)

    def load_monday_snapshot(
        self, *, tenant_id: str, batch_id: str
    ) -> WeeklyPlanMondaySnapshot | None:
        with self._lock:
            snapshot = self._monday_snapshots.get((tenant_id, batch_id))
            return deepcopy(snapshot) if snapshot is not None else None

    def reconcile_monday_snapshot(
        self, *, tenant_id: str, batch_id: str, as_of: datetime
    ) -> WeeklyPlanMondayReconciliation:
        with self._lock:
            snapshot = self._monday_snapshots.get((tenant_id, batch_id))
            if snapshot is None:
                raise ValueError("monday_snapshot_not_found")
            plans_by_owner = {
                plan.owner_user_id: plan
                for (plan_tenant, _), plan in self._plans.items()
                if plan_tenant == tenant_id and plan.batch_id == batch_id
            }
            rows: list[WeeklyPlanMondayDeltaRow] = []
            for frozen in snapshot.rows:
                current = plans_by_owner.get(frozen.user_id)
                current_status = _snapshot_plan_status(current)
                timing = (
                    weekly_plan_submission_timing(
                        current,
                        deadline_at=snapshot.deadline_at,
                        late_fill_until=_monday_end(snapshot.target_week_start, snapshot.deadline_at),
                    )
                    if current is not None
                    else "not_submitted"
                )
                current_version = current.version if current else 0
                changed = (
                    current_status != frozen.plan_status
                    or current_version != frozen.plan_version
                )
                rows.append(
                    WeeklyPlanMondayDeltaRow(
                        user_id=frozen.user_id,
                        snapshot_plan_status=frozen.plan_status,
                        current_plan_status=current_status,
                        submission_timing=timing,
                        changed_after_snapshot=changed,
                    )
                )
            return _monday_reconciliation(snapshot, tuple(rows), as_of=as_of)


def _days_payload(plan: WeeklyPlan | None) -> tuple[dict[str, object], ...]:
    if plan is None:
        return ()
    return tuple(
        {
            "plan_date": day.plan_date.isoformat(),
            "state": day.state,
            "items": tuple(
                {
                    "item_id": item.item_id,
                    "original_text": item.original_text,
                    "source": item.source,
                }
                for item in day.items
            ),
        }
        for day in plan.days
    )


def _snapshot_plan_status(plan: WeeklyPlan | None) -> str:
    if plan is None:
        return "unfilled"
    if (
        plan.status == "collecting"
        and not plan.suggestions
        and all(day.state == "unfilled" and not day.items for day in plan.days)
    ):
        return "unfilled"
    return plan.status


def _monday_end(target_week_start: date, reference: datetime) -> datetime:
    return datetime.combine(
        target_week_start,
        datetime.max.time(),
        reference.tzinfo,
    )


def _canonical_roster(
    roster: tuple[WeeklyPlanRosterMember, ...],
) -> tuple[tuple[str, str, str, str, str, str], ...]:
    return tuple(
        sorted(
            (
                member.user_id,
                member.display_name,
                member.department_id,
                member.department_name,
                member.team_id,
                member.team_name,
            )
            for member in roster
        )
    )


def _monday_reconciliation(
    snapshot: WeeklyPlanMondaySnapshot,
    rows: tuple[WeeklyPlanMondayDeltaRow, ...],
    *,
    as_of: datetime,
) -> WeeklyPlanMondayReconciliation:
    return WeeklyPlanMondayReconciliation(
        snapshot_id=snapshot.snapshot_id,
        batch_id=snapshot.batch_id,
        tenant_id=snapshot.tenant_id,
        target_week_start=snapshot.target_week_start,
        snapshot_as_of=snapshot.as_of,
        deadline_at=snapshot.deadline_at,
        reconciled_at=as_of,
        roster_count=snapshot.roster_count,
        snapshot_submitted_count=snapshot.submitted_count,
        current_submitted_count=sum(
            row.current_plan_status == "submitted" for row in rows
        ),
        late_submitted_count=sum(row.submission_timing == "late" for row in rows),
        closed_window_submitted_count=sum(
            row.submission_timing == "closed_window" for row in rows
        ),
        changed_after_snapshot_count=sum(row.changed_after_snapshot for row in rows),
        rows=rows,
    )


_metadata = MetaData()
_batches = Table(
    "agent2_weekly_plan_batches",
    _metadata,
    Column("batch_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("target_week_start", Date, nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_roster = Table(
    "agent2_weekly_plan_roster_members",
    _metadata,
    Column("roster_member_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("batch_id", PG_UUID(as_uuid=True), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("display_name", String(256), nullable=False),
    Column("department_id", String(128), nullable=False),
    Column("department_name", String(256), nullable=False),
    Column("team_id", String(128), nullable=False),
    Column("team_name", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_plans = Table(
    "agent2_weekly_plans",
    _metadata,
    Column("plan_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("batch_id", PG_UUID(as_uuid=True), nullable=False),
    Column("owner_user_id", String(128), nullable=False),
    Column("target_week_start", Date, nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("submitted_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_days = Table(
    "agent2_weekly_plan_days",
    _metadata,
    Column("day_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("plan_id", PG_UUID(as_uuid=True), nullable=False),
    Column("plan_date", Date, nullable=False),
    Column("day_index", SmallInteger, nullable=False),
    Column("state", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_items = Table(
    "agent2_weekly_plan_items",
    _metadata,
    Column("item_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("plan_id", PG_UUID(as_uuid=True), nullable=False),
    Column("day_id", PG_UUID(as_uuid=True), nullable=False),
    Column("original_text", Text, nullable=False),
    Column("source", String(64), nullable=False),
    Column("source_ref", String(512), nullable=False),
    Column("position", Integer, nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_suggestions = Table(
    "agent2_weekly_plan_suggestions",
    _metadata,
    Column("suggestion_id", String(80), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("plan_id", PG_UUID(as_uuid=True), nullable=False),
    Column("owner_user_id", String(128), nullable=False),
    Column("target_week_start", Date, nullable=False),
    Column("source_kind", String(64), nullable=False),
    Column("source_ref", String(512), nullable=False),
    Column("source_version", String(128), nullable=False),
    Column("evidence_text", Text, nullable=False),
    Column("evidence_sha256", String(64), nullable=False),
    Column("matter_excerpt", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("decision_ref", String(512)),
    Column("accepted_item_id", PG_UUID(as_uuid=True)),
    Column("decided_at", DateTime(timezone=True)),
    Column("superseded_by_id", String(80)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_receipts = Table(
    "agent2_weekly_plan_command_receipts",
    _metadata,
    Column("receipt_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("command_id", String(256), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("actor_user_id", String(128), nullable=False),
    Column("source_message_id", String(512), nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("plan_id", PG_UUID(as_uuid=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("reason_code", String(128), nullable=False),
    Column("actual_write", Boolean, nullable=False),
    Column("before_version", Integer, nullable=False),
    Column("after_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_audits = Table(
    "agent2_weekly_plan_audit_events",
    _metadata,
    Column("audit_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("receipt_id", PG_UUID(as_uuid=True), nullable=False),
    Column("plan_id", PG_UUID(as_uuid=True), nullable=False),
    Column("actor_user_id", String(128), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("source_message_id", String(512), nullable=False),
    Column("before_json", JSONB, nullable=False),
    Column("after_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_monday_snapshots = Table(
    "agent2_weekly_plan_monday_snapshots",
    _metadata,
    Column("snapshot_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("batch_id", PG_UUID(as_uuid=True), nullable=False),
    Column("target_week_start", Date, nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=False),
    Column("roster_count", Integer, nullable=False),
    Column("submitted_count", Integer, nullable=False),
    Column("draft_count", Integer, nullable=False),
    Column("unfilled_count", Integer, nullable=False),
    Column("rows_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class SqlWeeklyPlanStore:
    """Authoritative async PostgreSQL adapter. The caller owns the transaction."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def open_or_load_batch(self, batch: WeeklyPlanBatch) -> WeeklyPlanBatch:
        """Open one tenant/week batch, then verify its roster is still identical.

        The tenant/week uniqueness constraint is the concurrency gate.  Once a
        batch exists its roster is a frozen fact: a retry may verify it, but it
        must never append newly selected users.
        """

        inserted_id = (
            await self.session.execute(
                pg_insert(_batches)
                .values(
                    batch_id=UUID(batch.batch_id),
                    tenant_id=batch.tenant_id,
                    target_week_start=batch.target_week_start,
                    status="collecting",
                    created_at=batch.created_at,
                    updated_at=batch.created_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[_batches.c.tenant_id, _batches.c.target_week_start]
                )
                .returning(_batches.c.batch_id)
            )
        ).scalar_one_or_none()

        if inserted_id is not None:
            for member in batch.roster:
                await self.session.execute(
                    pg_insert(_roster).values(
                        roster_member_id=UUID(
                            _stable_id(
                                "weekly-plan-roster", batch.batch_id, member.user_id
                            )
                        ),
                        tenant_id=batch.tenant_id,
                        batch_id=UUID(batch.batch_id),
                        user_id=member.user_id,
                        display_name=member.display_name,
                        department_id=member.department_id,
                        department_name=member.department_name,
                        team_id=member.team_id,
                        team_name=member.team_name,
                        created_at=batch.created_at,
                    )
                )

        batch_row = (
            await self.session.execute(
                select(_batches).where(
                    _batches.c.tenant_id == batch.tenant_id,
                    _batches.c.target_week_start == batch.target_week_start,
                )
            )
        ).mappings().one_or_none()
        if batch_row is None:
            raise ValueError("weekly_plan_batch_open_failed")
        persisted_batch_id = str(batch_row["batch_id"])
        roster_rows = (
            await self.session.execute(
                select(_roster)
                .where(
                    _roster.c.tenant_id == batch.tenant_id,
                    _roster.c.batch_id == UUID(persisted_batch_id),
                )
                .order_by(_roster.c.user_id)
            )
        ).mappings().all()
        persisted_roster = tuple(
            WeeklyPlanRosterMember(
                user_id=row["user_id"],
                display_name=row["display_name"],
                department_id=row["department_id"],
                department_name=row["department_name"],
                team_id=row["team_id"],
                team_name=row["team_name"],
            )
            for row in roster_rows
        )
        if _canonical_roster(persisted_roster) != _canonical_roster(batch.roster):
            raise ValueError("weekly_plan_frozen_roster_mismatch")
        return WeeklyPlanBatch(
            batch_id=persisted_batch_id,
            tenant_id=batch_row["tenant_id"],
            target_week_start=batch_row["target_week_start"],
            roster=persisted_roster,
            created_at=batch_row["created_at"],
        )

    async def save_batch(self, batch: WeeklyPlanBatch) -> None:
        await self.session.execute(
            pg_insert(_batches)
            .values(
                batch_id=UUID(batch.batch_id),
                tenant_id=batch.tenant_id,
                target_week_start=batch.target_week_start,
                status="collecting",
                created_at=batch.created_at,
                updated_at=batch.created_at,
            )
            .on_conflict_do_nothing(
                index_elements=[_batches.c.tenant_id, _batches.c.target_week_start]
            )
        )
        for member in batch.roster:
            await self.session.execute(
                pg_insert(_roster)
                .values(
                    roster_member_id=UUID(
                        _stable_id("weekly-plan-roster", batch.batch_id, member.user_id)
                    ),
                    tenant_id=batch.tenant_id,
                    batch_id=UUID(batch.batch_id),
                    user_id=member.user_id,
                    display_name=member.display_name,
                    department_id=member.department_id,
                    department_name=member.department_name,
                    team_id=member.team_id,
                    team_name=member.team_name,
                    created_at=batch.created_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        _roster.c.tenant_id,
                        _roster.c.batch_id,
                        _roster.c.user_id,
                    ]
                )
            )

    async def create_plan(self, plan: WeeklyPlan) -> WeeklyPlan:
        await self.session.execute(
            pg_insert(_plans).values(**_plan_row(plan)).on_conflict_do_nothing(
                index_elements=[
                    _plans.c.tenant_id,
                    _plans.c.owner_user_id,
                    _plans.c.target_week_start,
                ]
            )
        )
        for index, day in enumerate(plan.days, start=1):
            await self.session.execute(
                pg_insert(_days)
                .values(
                    day_id=UUID(day.day_id),
                    tenant_id=plan.tenant_id,
                    plan_id=UUID(plan.plan_id),
                    plan_date=day.plan_date,
                    day_index=index,
                    state=day.state,
                    created_at=plan.created_at,
                    updated_at=plan.updated_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[_days.c.tenant_id, _days.c.plan_id, _days.c.plan_date]
                )
            )
        return plan

    async def load_plan(
        self, *, tenant_id: str, plan_id: str, owner_user_id: str, for_update: bool = False
    ) -> WeeklyPlan | None:
        statement = select(_plans).where(
            _plans.c.tenant_id == tenant_id,
            _plans.c.plan_id == UUID(plan_id),
            _plans.c.owner_user_id == owner_user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        day_rows = (
            await self.session.execute(
                select(_days)
                .where(
                    _days.c.tenant_id == tenant_id,
                    _days.c.plan_id == UUID(plan_id),
                )
                .order_by(_days.c.day_index)
            )
        ).mappings().all()
        item_rows = (
            await self.session.execute(
                select(_items)
                .where(
                    _items.c.tenant_id == tenant_id,
                    _items.c.plan_id == UUID(plan_id),
                    _items.c.deleted_at.is_(None),
                )
                .order_by(_items.c.day_id, _items.c.position)
            )
        ).mappings().all()
        suggestion_rows = (
            await self.session.execute(
                select(_suggestions).where(
                    _suggestions.c.tenant_id == tenant_id,
                    _suggestions.c.plan_id == UUID(plan_id),
                )
            )
        ).mappings().all()
        return _plan_from_rows(row, day_rows, item_rows, suggestion_rows)

    async def load_plan_by_owner_week(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        target_week_start,
        for_update: bool = False,
    ) -> WeeklyPlan | None:
        statement = select(_plans.c.plan_id).where(
            _plans.c.tenant_id == tenant_id,
            _plans.c.owner_user_id == owner_user_id,
            _plans.c.target_week_start == target_week_start,
        )
        if for_update:
            statement = statement.with_for_update()
        plan_id = (await self.session.execute(statement)).scalar_one_or_none()
        if plan_id is None:
            return None
        return await self.load_plan(
            tenant_id=tenant_id,
            plan_id=str(plan_id),
            owner_user_id=owner_user_id,
            for_update=for_update,
        )

    async def load_open_target_week_for_owner(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start,
    ):
        """Return the exact preferred open batch containing this roster member."""

        ref = await self.load_open_batch_ref_for_owner(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            preferred_target_week_start=preferred_target_week_start,
        )
        return ref.target_week_start if ref is not None else None

    async def load_open_batch_ref_for_owner(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start,
    ) -> OpenWeeklyPlanBatchRef | None:
        """Return the authoritative open batch ID for an exact roster member."""

        if preferred_target_week_start.weekday() != 0:
            raise ValueError("preferred weekly-plan target must start on Monday")

        rows = (
            await self.session.execute(
                select(_batches.c.batch_id, _batches.c.target_week_start)
                .select_from(
                    _batches.join(
                        _roster,
                        and_(
                            _roster.c.tenant_id == _batches.c.tenant_id,
                            _roster.c.batch_id == _batches.c.batch_id,
                        ),
                    )
                )
                .where(
                    _batches.c.tenant_id == tenant_id,
                    _roster.c.user_id == owner_user_id,
                    _batches.c.target_week_start == preferred_target_week_start,
                    _batches.c.status.in_(("collecting", "snapshotted")),
                )
                .order_by(_batches.c.target_week_start.desc())
                .limit(2)
            )
        ).mappings().all()
        if len(rows) > 1:
            raise ValueError("multiple_open_weekly_plan_batches")
        if not rows:
            return None
        row = rows[0]
        return OpenWeeklyPlanBatchRef(
            batch_id=str(row["batch_id"]),
            target_week_start=row["target_week_start"],
        )

    async def build_monday_snapshot(
        self,
        *,
        tenant_id: str,
        batch_id: str,
        as_of: datetime,
        deadline_at: datetime,
    ) -> WeeklyPlanMondaySnapshot:
        existing = (
            await self.session.execute(
                select(_monday_snapshots).where(
                    _monday_snapshots.c.tenant_id == tenant_id,
                    _monday_snapshots.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().one_or_none()
        if existing is not None:
            return _monday_snapshot_from_row(existing)
        batch = (
            await self.session.execute(
                select(_batches).where(
                    _batches.c.tenant_id == tenant_id,
                    _batches.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().one_or_none()
        if batch is None:
            raise ValueError("batch_not_found")
        roster_rows = (
            await self.session.execute(
                select(_roster)
                .where(
                    _roster.c.tenant_id == tenant_id,
                    _roster.c.batch_id == UUID(batch_id),
                )
                .order_by(_roster.c.department_name, _roster.c.team_name, _roster.c.display_name)
            )
        ).mappings().all()
        plan_rows = (
            await self.session.execute(
                select(_plans).where(
                    _plans.c.tenant_id == tenant_id,
                    _plans.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().all()
        plans = {row["owner_user_id"]: row for row in plan_rows}
        rows = []
        for member in roster_rows:
            plan = plans.get(member["user_id"])
            days = ()
            if plan is not None:
                loaded = await self.load_plan(
                    tenant_id=tenant_id,
                    plan_id=str(plan["plan_id"]),
                    owner_user_id=member["user_id"],
                )
                days = _days_payload(loaded)
            rows.append(
                WeeklyPlanMondayRow(
                    user_id=member["user_id"],
                    display_name=member["display_name"],
                    department_id=member["department_id"],
                    department_name=member["department_name"],
                    team_id=member["team_id"],
                    team_name=member["team_name"],
                    plan_id=str(plan["plan_id"]) if plan else "",
                    plan_version=int(plan["version"]) if plan else 0,
                    plan_status=(
                        _snapshot_plan_status(loaded)
                        if plan is not None
                        else "unfilled"
                    ),
                    submitted_at=plan["submitted_at"] if plan else None,
                    days=days,
                )
            )
        submitted = sum(row.plan_status == "submitted" for row in rows)
        draft = sum(
            row.plan_status in {"collecting", "pending_confirmation"} for row in rows
        )
        unfilled = sum(row.plan_status == "unfilled" for row in rows)
        snapshot = WeeklyPlanMondaySnapshot(
            snapshot_id=_stable_id("weekly-plan-monday-snapshot", tenant_id, batch_id),
            batch_id=batch_id,
            tenant_id=tenant_id,
            target_week_start=batch["target_week_start"],
            as_of=as_of,
            deadline_at=deadline_at,
            roster_count=len(rows),
            submitted_count=submitted,
            draft_count=draft,
            unfilled_count=unfilled,
            rows=tuple(rows),
        )
        await self.session.execute(
            pg_insert(_monday_snapshots)
            .values(**_monday_snapshot_row(snapshot))
            .on_conflict_do_nothing(
                index_elements=[
                    _monday_snapshots.c.tenant_id,
                    _monday_snapshots.c.batch_id,
                ]
            )
        )
        await self.session.execute(
            update(_batches)
            .where(
                _batches.c.tenant_id == tenant_id,
                _batches.c.batch_id == UUID(batch_id),
                _batches.c.status == "collecting",
            )
            .values(status="snapshotted", updated_at=as_of)
        )
        persisted = (
            await self.session.execute(
                select(_monday_snapshots).where(
                    _monday_snapshots.c.tenant_id == tenant_id,
                    _monday_snapshots.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("monday_snapshot_persist_failed")
        return _monday_snapshot_from_row(persisted)

    async def reconcile_monday_snapshot(
        self, *, tenant_id: str, batch_id: str, as_of: datetime
    ) -> WeeklyPlanMondayReconciliation:
        snapshot_row = (
            await self.session.execute(
                select(_monday_snapshots).where(
                    _monday_snapshots.c.tenant_id == tenant_id,
                    _monday_snapshots.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().one_or_none()
        if snapshot_row is None:
            raise ValueError("monday_snapshot_not_found")
        snapshot = _monday_snapshot_from_row(snapshot_row)
        plan_rows = (
            await self.session.execute(
                select(_plans).where(
                    _plans.c.tenant_id == tenant_id,
                    _plans.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().all()
        current_by_owner = {row["owner_user_id"]: row for row in plan_rows}
        rows: list[WeeklyPlanMondayDeltaRow] = []
        for frozen in snapshot.rows:
            current = current_by_owner.get(frozen.user_id)
            if current is None:
                current_status = "unfilled"
            else:
                loaded_current = await self.load_plan(
                    tenant_id=tenant_id,
                    plan_id=str(current["plan_id"]),
                    owner_user_id=frozen.user_id,
                )
                current_status = _snapshot_plan_status(loaded_current)
            if current is None or current["submitted_at"] is None:
                timing = "not_submitted"
            else:
                submitted_at = current["submitted_at"]
                if submitted_at > _monday_end(
                    snapshot.target_week_start, snapshot.deadline_at
                ):
                    timing = "closed_window"
                elif submitted_at > snapshot.deadline_at:
                    timing = "late"
                else:
                    timing = "on_time"
            changed = (
                current_status != frozen.plan_status
                or (int(current["version"]) if current else 0) != frozen.plan_version
            )
            rows.append(
                WeeklyPlanMondayDeltaRow(
                    user_id=frozen.user_id,
                    snapshot_plan_status=frozen.plan_status,
                    current_plan_status=current_status,
                    submission_timing=timing,
                    changed_after_snapshot=changed,
                )
            )
        return _monday_reconciliation(snapshot, tuple(rows), as_of=as_of)

    async def load_monday_snapshot(
        self, *, tenant_id: str, batch_id: str
    ) -> WeeklyPlanMondaySnapshot | None:
        row = (
            await self.session.execute(
                select(_monday_snapshots).where(
                    _monday_snapshots.c.tenant_id == tenant_id,
                    _monday_snapshots.c.batch_id == UUID(batch_id),
                )
            )
        ).mappings().one_or_none()
        return _monday_snapshot_from_row(row) if row is not None else None

    async def execute(
        self, command: WeeklyPlanCommand, *, executed_at: datetime
    ) -> WeeklyPlanExecution:
        plan = await self.load_plan(
            tenant_id=command.tenant_id,
            plan_id=command.plan_id,
            owner_user_id=command.actor_user_id,
            for_update=True,
        )
        if plan is None:
            raise ValueError("plan_not_found")
        existing = (
            await self.session.execute(
                select(_receipts).where(
                    _receipts.c.tenant_id == command.tenant_id,
                    _receipts.c.idempotency_key == command.idempotency_key,
                )
            )
        ).mappings().one_or_none()
        if existing is not None:
            executed = execute_weekly_plan_command(
                command,
                plan=plan,
                executed_at=executed_at,
                executed_idempotency_keys=frozenset({command.idempotency_key}),
            )
            if not _receipt_scope_matches(existing, command):
                return replace(
                    executed,
                    receipt=replace(
                        executed.receipt,
                        status="blocked",
                        reason_code="idempotency_scope_conflict",
                        actual_write=False,
                        before_version=plan.version,
                        after_version=plan.version,
                    ),
                    audit_event=None,
                )
            return executed
        execution = execute_weekly_plan_command(
            command, plan=plan, executed_at=executed_at
        )
        if execution.receipt.actual_write:
            result = await self.session.execute(
                update(_plans)
                .where(
                    _plans.c.tenant_id == command.tenant_id,
                    _plans.c.plan_id == UUID(command.plan_id),
                    _plans.c.owner_user_id == command.actor_user_id,
                    _plans.c.version == command.expected_version,
                )
                .values(**_plan_update_row(execution.after))
            )
            if result.rowcount != 1:
                raise ValueError("version_conflict")
            await self._replace_children(execution.after)
        await self.session.execute(pg_insert(_receipts).values(**_receipt_row(execution.receipt)))
        if execution.audit_event is not None:
            await self.session.execute(pg_insert(_audits).values(**_audit_row(execution.audit_event)))
        await self.session.flush()
        return execution

    async def execute_batch(
        self,
        commands: tuple[WeeklyPlanCommand, ...],
        *,
        executed_at: datetime,
    ) -> tuple[WeeklyPlanExecution, ...]:
        """Apply one tool-call batch atomically after validating its final state."""

        if not commands:
            raise ValueError("weekly_plan_command_batch_required")
        first = commands[0]
        batch_first = replace(
            first,
            patch={**first.patch, "_batch_sha256": _command_batch_sha256(commands)},
        )
        if any(
            command.tenant_id != first.tenant_id
            or command.actor_user_id != first.actor_user_id
            or command.plan_id != first.plan_id
            for command in commands
        ):
            raise ValueError("weekly_plan_command_batch_scope_mismatch")
        plan = await self.load_plan(
            tenant_id=first.tenant_id,
            plan_id=first.plan_id,
            owner_user_id=first.actor_user_id,
            for_update=True,
        )
        if plan is None:
            raise ValueError("plan_not_found")
        existing = (
            await self.session.execute(
                select(_receipts).where(
                    _receipts.c.tenant_id == first.tenant_id,
                    _receipts.c.idempotency_key == first.idempotency_key,
                )
            )
        ).mappings().one_or_none()
        if existing is not None:
            duplicate = _domain_execution(
                batch_first, plan, plan, executed_at, "duplicate", "duplicate"
            )
            if not _receipt_scope_matches(existing, batch_first):
                return (
                    replace(
                        duplicate,
                        receipt=replace(
                            duplicate.receipt,
                            status="blocked",
                            reason_code="idempotency_scope_conflict",
                        ),
                    ),
                )
            return (duplicate,)
        execution = execute_weekly_plan_batch(
            commands, plan=plan, executed_at=executed_at
        )
        if execution.receipt.status != "executed":
            return (execution,)
        current = execution.after
        result = await self.session.execute(
            update(_plans)
            .where(
                _plans.c.tenant_id == first.tenant_id,
                _plans.c.plan_id == UUID(first.plan_id),
                _plans.c.owner_user_id == first.actor_user_id,
                _plans.c.version == plan.version,
            )
            .values(**_plan_update_row(current))
        )
        if result.rowcount != 1:
            raise ValueError("version_conflict")
        await self._replace_children(current)
        await self.session.execute(
            pg_insert(_receipts).values(**_receipt_row(execution.receipt))
        )
        if execution.audit_event is not None:
            await self.session.execute(
                pg_insert(_audits).values(**_audit_row(execution.audit_event))
            )
        await self.session.flush()
        return (execution,)

    async def _replace_children(self, plan: WeeklyPlan) -> None:
        for day in plan.days:
            await self.session.execute(
                update(_days)
                .where(
                    _days.c.tenant_id == plan.tenant_id,
                    _days.c.day_id == UUID(day.day_id),
                )
                .values(state=day.state, updated_at=plan.updated_at)
            )
        await self.session.execute(
            delete(_suggestions).where(
                _suggestions.c.tenant_id == plan.tenant_id,
                _suggestions.c.plan_id == UUID(plan.plan_id),
            )
        )
        await self.session.execute(
            delete(_items).where(
                _items.c.tenant_id == plan.tenant_id,
                _items.c.plan_id == UUID(plan.plan_id),
            )
        )
        for day in plan.days:
            for position, item in enumerate(day.items):
                await self.session.execute(
                    pg_insert(_items).values(
                        item_id=UUID(item.item_id),
                        tenant_id=plan.tenant_id,
                        plan_id=UUID(plan.plan_id),
                        day_id=UUID(day.day_id),
                        original_text=item.original_text,
                        source=item.source,
                        source_ref=item.source_ref,
                        position=position,
                        deleted_at=None,
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                    )
                )
        for suggestion in plan.suggestions:
            await self.session.execute(
                pg_insert(_suggestions).values(**_suggestion_row(plan, suggestion))
            )


async def weekly_plan_state_payload(
    session: Any,
    *,
    tenant_id: str,
    owner_user_id: str,
) -> list[dict[str, Any]]:
    """Canonical weekly-plan state used by the production receipt verifier.

    This deliberately includes formal plans, items, suggestions, command
    receipts and audits for only the authenticated owner.  A Tool Call cannot
    be reported as successful unless this payload actually changes in the same
    database transaction.
    """

    plan_rows = (
        await session.execute(
            select(_plans)
            .where(
                _plans.c.tenant_id == tenant_id,
                _plans.c.owner_user_id == owner_user_id,
            )
            .order_by(_plans.c.target_week_start, _plans.c.plan_id)
        )
    ).mappings().all()
    result: list[dict[str, Any]] = []
    for row in plan_rows:
        plan_id = row["plan_id"]
        day_rows = (
            await session.execute(
                select(_days)
                .where(
                    _days.c.tenant_id == tenant_id,
                    _days.c.plan_id == plan_id,
                )
                .order_by(_days.c.day_index)
            )
        ).mappings().all()
        item_rows = (
            await session.execute(
                select(_items)
                .where(
                    _items.c.tenant_id == tenant_id,
                    _items.c.plan_id == plan_id,
                )
                .order_by(_items.c.day_id, _items.c.position, _items.c.item_id)
            )
        ).mappings().all()
        suggestion_rows = (
            await session.execute(
                select(_suggestions)
                .where(
                    _suggestions.c.tenant_id == tenant_id,
                    _suggestions.c.plan_id == plan_id,
                    _suggestions.c.owner_user_id == owner_user_id,
                )
                .order_by(_suggestions.c.suggestion_id)
            )
        ).mappings().all()
        receipt_rows = (
            await session.execute(
                select(_receipts)
                .where(
                    _receipts.c.tenant_id == tenant_id,
                    _receipts.c.plan_id == plan_id,
                    _receipts.c.actor_user_id == owner_user_id,
                )
                .order_by(_receipts.c.receipt_id)
            )
        ).mappings().all()
        audit_rows = (
            await session.execute(
                select(_audits)
                .where(
                    _audits.c.tenant_id == tenant_id,
                    _audits.c.plan_id == plan_id,
                    _audits.c.actor_user_id == owner_user_id,
                )
                .order_by(_audits.c.audit_id)
            )
        ).mappings().all()
        result.append(
            {
                "plan_id": str(plan_id),
                "batch_id": str(row["batch_id"]),
                "target_week_start": row["target_week_start"].isoformat(),
                "status": row["status"],
                "version": int(row["version"]),
                "submitted_at": _iso(row["submitted_at"]),
                "days": [
                    {
                        "day_id": str(day["day_id"]),
                        "plan_date": day["plan_date"].isoformat(),
                        "day_index": int(day["day_index"]),
                        "state": day["state"],
                    }
                    for day in day_rows
                ],
                "items": [
                    {
                        "item_id": str(item["item_id"]),
                        "day_id": str(item["day_id"]),
                        "original_text": item["original_text"],
                        "source": item["source"],
                        "source_ref": item["source_ref"],
                        "position": int(item["position"]),
                        "deleted_at": _iso(item["deleted_at"]),
                    }
                    for item in item_rows
                ],
                "suggestions": [
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "status": suggestion["status"],
                        "source_kind": suggestion["source_kind"],
                        "source_ref": suggestion["source_ref"],
                        "source_version": suggestion["source_version"],
                        "evidence_sha256": suggestion["evidence_sha256"],
                        "matter_excerpt": suggestion["matter_excerpt"],
                        "decision_ref": suggestion["decision_ref"],
                        "accepted_item_id": (
                            str(suggestion["accepted_item_id"])
                            if suggestion["accepted_item_id"] is not None
                            else None
                        ),
                    }
                    for suggestion in suggestion_rows
                ],
                "receipts": [
                    {
                        "receipt_id": str(receipt["receipt_id"]),
                        "request_sha256": receipt["request_sha256"],
                        "status": receipt["status"],
                        "reason_code": receipt["reason_code"],
                        "actual_write": bool(receipt["actual_write"]),
                        "before_version": int(receipt["before_version"]),
                        "after_version": int(receipt["after_version"]),
                    }
                    for receipt in receipt_rows
                ],
                "audits": [
                    {
                        "audit_id": str(audit["audit_id"]),
                        "receipt_id": str(audit["receipt_id"]),
                        "command_type": audit["command_type"],
                        "source_message_id": audit["source_message_id"],
                        "before": audit["before_json"],
                        "after": audit["after_json"],
                    }
                    for audit in audit_rows
                ],
            }
        )
    return result


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _plan_row(plan: WeeklyPlan) -> dict[str, Any]:
    return {
        "plan_id": UUID(plan.plan_id),
        "tenant_id": plan.tenant_id,
        "batch_id": UUID(plan.batch_id),
        "owner_user_id": plan.owner_user_id,
        "target_week_start": plan.target_week_start,
        "status": plan.status,
        "version": plan.version,
        "submitted_at": plan.submitted_at,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def _plan_update_row(plan: WeeklyPlan) -> dict[str, Any]:
    return {
        "status": plan.status,
        "version": plan.version,
        "submitted_at": plan.submitted_at,
        "updated_at": plan.updated_at,
    }


def _receipt_row(receipt: WeeklyPlanReceipt) -> dict[str, Any]:
    return {
        **receipt.__dict__,
        "receipt_id": UUID(receipt.receipt_id),
        "plan_id": UUID(receipt.plan_id),
    }


def _audit_row(audit: WeeklyPlanAuditEvent) -> dict[str, Any]:
    return {
        "audit_id": UUID(audit.audit_id),
        "tenant_id": audit.tenant_id,
        "receipt_id": UUID(audit.receipt_id),
        "plan_id": UUID(audit.plan_id),
        "actor_user_id": audit.actor_user_id,
        "command_type": audit.command_type,
        "source_message_id": audit.source_message_id,
        "before_json": audit.before,
        "after_json": audit.after,
        "created_at": audit.created_at,
    }


def _suggestion_row(plan: WeeklyPlan, suggestion) -> dict[str, Any]:
    return {
        "suggestion_id": suggestion.suggestion_id,
        "tenant_id": plan.tenant_id,
        "plan_id": UUID(plan.plan_id),
        "owner_user_id": suggestion.owner_user_id,
        "target_week_start": suggestion.target_week_start,
        "source_kind": suggestion.source_kind.value,
        "source_ref": suggestion.source_ref,
        "source_version": suggestion.source_version,
        "evidence_text": suggestion.evidence_text,
        "evidence_sha256": suggestion.evidence_sha256,
        "matter_excerpt": suggestion.matter_excerpt,
        "expires_at": suggestion.expires_at,
        "status": suggestion.status.value,
        "decision_ref": suggestion.decision_ref,
        "accepted_item_id": UUID(suggestion.accepted_item_id) if suggestion.accepted_item_id else None,
        "decided_at": suggestion.decided_at,
        "superseded_by_id": suggestion.superseded_by_id,
        "created_at": suggestion.created_at,
    }


def _receipt_scope_matches(row, command: WeeklyPlanCommand) -> bool:
    return (
        str(row["request_sha256"]) == _receipt_request_hash(command)
        and
        str(row["command_id"]) == command.command_id
        and str(row["command_type"]) == command.command_type
        and str(row["actor_user_id"]) == command.actor_user_id
        and str(row["plan_id"]) == command.plan_id
        and str(row["source_message_id"]) == command.source_message_id
    )


def _receipt_request_hash(command: WeeklyPlanCommand) -> str:
    from app.agent2.weekly_plan_domain import _command_request_sha256

    return _command_request_sha256(command)


def _plan_from_rows(plan_row, day_rows, item_rows, suggestion_rows) -> WeeklyPlan:
    from app.agent2.weekly_plan_suggestions import (
        SuggestionStatus,
        TrustedSourceKind,
        TrustedSuggestionEvidence,
        WeeklyPlanSuggestion,
    )

    items_by_day: dict[str, list[WeeklyPlanItem]] = {}
    for row in item_rows:
        items_by_day.setdefault(str(row["day_id"]), []).append(
            WeeklyPlanItem(
                item_id=str(row["item_id"]),
                original_text=row["original_text"],
                source=row["source"],
                source_ref=row["source_ref"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
    days = tuple(
        WeeklyPlanDay(
            day_id=str(row["day_id"]),
            plan_date=row["plan_date"],
            state=row["state"],
            items=tuple(items_by_day.get(str(row["day_id"]), ())),
        )
        for row in day_rows
    )
    suggestions = tuple(
        WeeklyPlanSuggestion(
            suggestion_id=row["suggestion_id"],
            owner_user_id=row["owner_user_id"],
            target_week_start=row["target_week_start"],
            evidence=TrustedSuggestionEvidence(
                owner_user_id=row["owner_user_id"],
                source_kind=TrustedSourceKind(row["source_kind"]),
                source_ref=row["source_ref"],
                source_version=row["source_version"],
                evidence_text=row["evidence_text"],
                evidence_sha256=row["evidence_sha256"],
            ),
            matter_excerpt=row["matter_excerpt"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            status=SuggestionStatus(row["status"]),
            decision_ref=row["decision_ref"],
            decided_at=row["decided_at"],
            superseded_by_id=row["superseded_by_id"],
            accepted_item_id=(str(row["accepted_item_id"]) if row["accepted_item_id"] else None),
        )
        for row in suggestion_rows
    )
    return WeeklyPlan(
        plan_id=str(plan_row["plan_id"]),
        batch_id=str(plan_row["batch_id"]),
        tenant_id=plan_row["tenant_id"],
        owner_user_id=plan_row["owner_user_id"],
        target_week_start=plan_row["target_week_start"],
        status=plan_row["status"],
        version=plan_row["version"],
        days=days,
        suggestions=suggestions,
        submitted_at=plan_row["submitted_at"],
        created_at=plan_row["created_at"],
        updated_at=plan_row["updated_at"],
    )


def _monday_snapshot_row(snapshot: WeeklyPlanMondaySnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": UUID(snapshot.snapshot_id),
        "tenant_id": snapshot.tenant_id,
        "batch_id": UUID(snapshot.batch_id),
        "target_week_start": snapshot.target_week_start,
        "as_of": snapshot.as_of,
        "deadline_at": snapshot.deadline_at,
        "roster_count": snapshot.roster_count,
        "submitted_count": snapshot.submitted_count,
        "draft_count": snapshot.draft_count,
        "unfilled_count": snapshot.unfilled_count,
        "rows_json": [
            {
                **row.__dict__,
                "submitted_at": _iso(row.submitted_at),
                "days": [
                    {
                        **dict(day),
                        "items": [dict(item) for item in day.get("items", ())],
                    }
                    for day in row.days
                ],
            }
            for row in snapshot.rows
        ],
        "created_at": snapshot.as_of,
    }


def _monday_snapshot_from_row(row) -> WeeklyPlanMondaySnapshot:
    return WeeklyPlanMondaySnapshot(
        snapshot_id=str(row["snapshot_id"]),
        batch_id=str(row["batch_id"]),
        tenant_id=row["tenant_id"],
        target_week_start=row["target_week_start"],
        as_of=row["as_of"],
        deadline_at=row["deadline_at"],
        roster_count=row["roster_count"],
        submitted_count=row["submitted_count"],
        draft_count=row["draft_count"],
        unfilled_count=row["unfilled_count"],
        rows=tuple(
            WeeklyPlanMondayRow(
                **{
                    **item,
                    "submitted_at": (
                        datetime.fromisoformat(item["submitted_at"])
                        if item.get("submitted_at")
                        else None
                    ),
                    "days": tuple(
                        {
                            **day,
                            "items": tuple(dict(value) for value in day.get("items", ())),
                        }
                        for day in item.get("days", ())
                    ),
                }
            )
            for item in row["rows_json"]
        ),
    )
