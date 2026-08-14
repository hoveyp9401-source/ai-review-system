"""Production adapter for the independent Agent2 weekly-plan domain.

The model decides what the user means and supplies typed operations.  This module
only enforces authenticated scope, current-turn evidence, exact dates/versions,
transaction-ready commands, and safe facts for the model's final reply.  It never
reads or writes a daily report.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    ApplyNextWeeklyPlanArgs,
    QueryNextWeeklyPlanArgs,
    ReceiptStatus,
    SubmitNextWeeklyPlanArgs,
)
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.production_daily_executor import (
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.validation import BoundCall
from app.agent2.weekly_plan_context import TrustedWeeklyPlanContext
from app.agent2.weekly_plan_domain import (
    create_weekly_plan,
    create_weekly_plan_batch,
    execute_weekly_plan_batch,
)
from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanBatch,
    WeeklyPlanCommand,
    WeeklyPlanExecution,
    WeeklyPlanRosterMember,
)
from app.agent2.weekly_plan_store import SqlWeeklyPlanStore
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    render_suggestion_prompt,
)


class WeeklyPlanStorePort(Protocol):
    async def load_plan_by_owner_week(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        target_week_start: date,
        for_update: bool = False,
    ) -> WeeklyPlan | None: ...

    async def open_or_load_batch(self, batch: WeeklyPlanBatch) -> WeeklyPlanBatch: ...

    async def create_plan(self, plan: WeeklyPlan) -> WeeklyPlan: ...

    async def execute_batch(
        self,
        commands: tuple[WeeklyPlanCommand, ...],
        *,
        executed_at,
    ) -> tuple[WeeklyPlanExecution, ...]: ...

    async def execute(
        self,
        command: WeeklyPlanCommand,
        *,
        executed_at,
    ) -> WeeklyPlanExecution: ...


class ProductionWeeklyPlanExecutor:
    """Execute one authenticated person's exact Monday-to-Saturday plan."""

    def __init__(
        self,
        *,
        session: Any,
        user: Any,
        context: TrustedContext,
        bound_calls: dict[str, BoundCall],
        current_turn_source: CurrentTurnSource | None,
        store: WeeklyPlanStorePort | None = None,
        settings: object | None = None,
        authoritative_roster_member: WeeklyPlanRosterMember | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._user = user
        self._context = context
        self._bound_calls = bound_calls
        self._current_turn_source = current_turn_source
        self._store = store or SqlWeeklyPlanStore(session)
        self._settings = settings
        self._authoritative_roster_member = authoritative_roster_member
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    async def query_next_weekly_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        self._arguments(request, QueryNextWeeklyPlanArgs)
        weekly = self._weekly_target(request)
        virtual = self._trusted_virtual_plan(weekly)
        live = await self._load_live(weekly, for_update=False)
        if live is None:
            self._require_empty_virtual_context(virtual, weekly)
            plan = virtual
        else:
            self._require_live_matches_context(live, weekly)
            plan = live
        self._require_week_shape(plan)
        return self._outcome(
            plan=plan,
            before_version=plan.version,
            actual_write=False,
            idempotency_key=None,
        )

    async def apply_next_weekly_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, ApplyNextWeeklyPlanArgs)
        self._validate_write_evidence(request, arguments)
        weekly = self._weekly_target(request)
        executed_at = self._require_write_window(weekly)
        virtual = self._trusted_virtual_plan(weekly)
        self._require_argument_binding(
            weekly,
            plan_id=arguments.plan_id,
            expected_version=arguments.expected_version,
        )
        live = await self._load_live(weekly, for_update=True)
        is_first_write = live is None
        if is_first_write:
            self._require_empty_virtual_context(virtual, weekly)
            before = virtual
        else:
            self._require_live_matches_context(live, weekly)
            before = live
        self._require_week_shape(before)
        commands = self._apply_commands(request, arguments, before)

        # Simulate every operation before creating first-use rows.  This prevents
        # an invalid tool call from leaving an empty batch/plan behind.
        self._preflight_batch(commands, plan=before)

        if is_first_write:
            _batch, initial = await self._new_batch_and_plan(weekly)
            await self._store.create_plan(initial)
            created = await self._load_live(weekly, for_update=True)
            if created is None:
                raise ProductionExecutionError("WEEKLY_PLAN_CREATE_NOT_VISIBLE")
            self._require_plan_identity(created, expected=initial)
            if created.version != arguments.expected_version:
                raise ProductionExecutionError("WEEKLY_PLAN_VERSION_MISMATCH")
            self._require_empty_created_plan(created)

        try:
            executions = await self._store.execute_batch(
                commands,
                executed_at=executed_at,
            )
        except ValueError as exc:
            raise ProductionExecutionError(
                _store_error_code(exc)
            ) from exc
        if not executions:
            raise ProductionExecutionError("WEEKLY_PLAN_EXECUTION_MISSING")
        execution = executions[-1]
        after = execution.after
        self._require_plan_identity(after, expected=before)
        if after.version != before.version + 1:
            raise ProductionExecutionError("WEEKLY_PLAN_VERSION_RESULT_INVALID")
        return self._outcome(
            plan=after,
            before_version=before.version,
            actual_write=True,
            idempotency_key=self._tool_idempotency_key(request, arguments),
            affected_item_ids=_affected_item_ids(before, after),
        )

    async def submit_next_weekly_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, SubmitNextWeeklyPlanArgs)
        self._validate_write_evidence(request, arguments)
        weekly = self._weekly_target(request)
        executed_at = self._require_write_window(weekly)
        self._require_argument_binding(
            weekly,
            plan_id=arguments.plan_id,
            expected_version=arguments.expected_version,
        )
        live = await self._load_live(weekly, for_update=True)
        if live is None:
            raise ProductionExecutionError("WEEKLY_PLAN_NOT_CREATED")
        self._require_live_matches_context(live, weekly)
        self._require_week_shape(live)
        unresolved = tuple(
            day.plan_date.isoformat()
            for day in live.days
            if day.state == "unfilled"
        )
        if unresolved:
            return self._outcome(
                plan=live,
                before_version=live.version,
                actual_write=False,
                idempotency_key=self._tool_idempotency_key(
                    request, arguments
                ),
                status_if_unchanged=ReceiptStatus.BLOCKED,
                error_code="WEEKLY_PLAN_UNRESOLVED_DAYS",
                unresolved_dates=unresolved,
            )
        if live.status == "submitted":
            return self._outcome(
                plan=live,
                before_version=live.version,
                actual_write=False,
                idempotency_key=self._tool_idempotency_key(
                    request, arguments
                ),
            )
        command = self._command(
            request=request,
            operation_id="submit",
            command_type="submit_plan",
            expected_version=live.version,
            patch={},
            ordinal=1,
            arguments=arguments,
        )
        try:
            execution = await self._store.execute(
                command,
                executed_at=executed_at,
            )
        except ValueError as exc:
            raise ProductionExecutionError(
                _store_error_code(exc)
            ) from exc
        if execution.receipt.status == "blocked":
            return self._outcome(
                plan=live,
                before_version=live.version,
                actual_write=False,
                idempotency_key=self._tool_idempotency_key(
                    request, arguments
                ),
                status_if_unchanged=ReceiptStatus.BLOCKED,
                error_code=_domain_error_code(
                    execution.receipt.reason_code
                ),
            )
        after = execution.after
        self._require_plan_identity(after, expected=live)
        if after.status != "submitted" or after.version != live.version + 1:
            raise ProductionExecutionError("WEEKLY_PLAN_SUBMIT_RESULT_INVALID")
        return self._outcome(
            plan=after,
            before_version=live.version,
            actual_write=True,
            idempotency_key=self._tool_idempotency_key(request, arguments),
        )

    def _trusted_virtual_plan(
        self,
        weekly: TrustedWeeklyPlanContext,
    ) -> WeeklyPlan:
        self._require_authenticated_scope(weekly)
        batch, expected = self._virtual_batch_and_plan(weekly)
        if weekly.plan_id != expected.plan_id or weekly.batch_id != batch.batch_id:
            raise ProductionExecutionError("WEEKLY_PLAN_CONTEXT_ID_INVALID")
        return WeeklyPlan(
            plan_id=weekly.plan_id,
            batch_id=weekly.batch_id,
            tenant_id=weekly.tenant_id,
            owner_user_id=weekly.owner_user_id,
            target_week_start=weekly.target_week_start,
            status=("collecting" if weekly.status == "draft" else weekly.status),
            version=weekly.version,
            days=tuple(
                replace(
                    expected.days[index],
                    state=day.state,
                    # The real live plan, not model context, is authoritative
                    # for writes. Context items are only reconstructed here to
                    # validate whether an absent row can be treated as virtual.
                    items=(),
                )
                for index, day in enumerate(weekly.days)
            ),
            suggestions=(),
            created_at=self._context.now,
            updated_at=self._context.now,
        )

    def _virtual_batch_and_plan(
        self,
        weekly: TrustedWeeklyPlanContext,
    ) -> tuple[WeeklyPlanBatch, WeeklyPlan]:
        """Reconstruct deterministic IDs without opening or freezing a batch."""

        member = WeeklyPlanRosterMember(
            user_id=str(self._context.principal.user_id),
            display_name=(
                self._context.principal.display_name
                or str(getattr(self._user, "name", "")).strip()
                or str(self._context.principal.user_id)
            ),
        )
        batch = create_weekly_plan_batch(
            tenant_id=self._context.principal.tenant_id,
            target_week_start=weekly.target_week_start,
            roster=(member,),
            created_at=self._context.now,
        )
        return batch, create_weekly_plan(
            batch=batch,
            owner_user_id=str(self._context.principal.user_id),
            created_at=self._context.now,
        )

    async def _new_batch_and_plan(
        self,
        weekly: TrustedWeeklyPlanContext,
    ) -> tuple[WeeklyPlanBatch, WeeklyPlan]:
        roster = await self._load_authoritative_roster()
        requested_batch = create_weekly_plan_batch(
            tenant_id=self._context.principal.tenant_id,
            target_week_start=weekly.target_week_start,
            roster=roster,
            created_at=self._context.now,
        )
        try:
            batch = await self._store.open_or_load_batch(requested_batch)
        except (TypeError, ValueError) as exc:
            raise ProductionExecutionError(_store_error_code(exc)) from exc
        if (
            batch.tenant_id != requested_batch.tenant_id
            or batch.target_week_start != requested_batch.target_week_start
            or batch.roster != requested_batch.roster
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_FROZEN_ROSTER_MISMATCH")
        return batch, create_weekly_plan(
            batch=batch,
            owner_user_id=str(self._context.principal.user_id),
            created_at=self._context.now,
        )

    async def _load_authoritative_roster(
        self,
    ) -> tuple[WeeklyPlanRosterMember, ...]:
        principal = self._context.principal
        injected_member = self._authoritative_roster_member
        if injected_member is not None:
            members = (injected_member,)
        else:
            user_ids = _configured_canary_roster_user_ids(
                self._settings,
                tenant_id=principal.tenant_id,
            )
            if (
                self._session is None
                or user_ids is None
                or str(principal.user_id) not in user_ids
            ):
                raise ProductionExecutionError(
                    "WEEKLY_PLAN_IDENTITY_BINDING_REQUIRED"
                )
            bindings = tuple(
                (
                    await self._session.scalars(
                        select(Agent2IdentityBinding).where(
                            Agent2IdentityBinding.tenant_id
                            == principal.tenant_id,
                            Agent2IdentityBinding.user_id.in_(user_ids),
                            Agent2IdentityBinding.active.is_(True),
                        )
                    )
                ).all()
            )
            by_user_id = {str(binding.user_id): binding for binding in bindings}
            if set(by_user_id) != set(user_ids):
                raise ProductionExecutionError(
                    "WEEKLY_PLAN_IDENTITY_BINDING_REQUIRED"
                )
            members = tuple(
                WeeklyPlanRosterMember(
                    user_id=user_id,
                    display_name=str(by_user_id[user_id].display_name),
                    department_id=str(by_user_id[user_id].department_id),
                    department_name="",
                    team_id=str(by_user_id[user_id].team_id),
                    team_name="",
                )
                for user_id in user_ids
            )
        canonical = tuple(sorted(members, key=lambda item: item.user_id))
        if (
            not canonical
            or len(canonical) > 2
            or len({member.user_id for member in canonical}) != len(canonical)
            or str(principal.user_id)
            not in {member.user_id for member in canonical}
            or any(
                not member.display_name.strip()
                or not member.department_id.strip()
                or not member.team_id.strip()
                for member in canonical
            )
        ):
            raise ProductionExecutionError(
                "WEEKLY_PLAN_IDENTITY_BINDING_INVALID"
            )
        return canonical

    async def _load_live(
        self,
        weekly: TrustedWeeklyPlanContext,
        *,
        for_update: bool,
    ) -> WeeklyPlan | None:
        try:
            return await self._store.load_plan_by_owner_week(
                tenant_id=self._context.principal.tenant_id,
                owner_user_id=str(self._context.principal.user_id),
                target_week_start=weekly.target_week_start,
                for_update=for_update,
            )
        except (TypeError, ValueError) as exc:
            raise ProductionExecutionError(
                _store_error_code(exc)
            ) from exc

    def _require_authenticated_scope(
        self,
        weekly: TrustedWeeklyPlanContext,
    ) -> None:
        principal = self._context.principal
        if principal.conversation_kind != "direct":
            raise ProductionExecutionError("WEEKLY_PLAN_DIRECT_CHAT_REQUIRED")
        if str(getattr(self._user, "id", "")) != str(principal.user_id):
            raise ProductionExecutionError("WEEKLY_PLAN_OWNER_MISMATCH")
        if (
            weekly.tenant_id != principal.tenant_id
            or weekly.owner_user_id != str(principal.user_id)
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_SCOPE_MISMATCH")

    def _require_argument_binding(
        self,
        weekly: TrustedWeeklyPlanContext,
        *,
        plan_id: UUID,
        expected_version: int,
    ) -> None:
        self._require_authenticated_scope(weekly)
        if str(plan_id) != weekly.plan_id:
            raise ProductionExecutionError("WEEKLY_PLAN_BINDING_MISMATCH")
        if expected_version != weekly.version:
            raise ProductionExecutionError("WEEKLY_PLAN_VERSION_MISMATCH")

    def _require_write_window(
        self,
        weekly: TrustedWeeklyPlanContext,
    ) -> datetime:
        """Allow the target week itself only through its Monday late-fill day."""

        server_now = self._now_provider()
        if server_now.tzinfo is None or server_now.utcoffset() is None:
            raise ProductionExecutionError("WEEKLY_PLAN_SERVER_TIME_INVALID")
        # This collection is a centre-wide business process, so its cutoff is
        # always Beijing time and cannot be changed by a personal preference.
        local_date = server_now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if local_date > weekly.target_week_start:
            raise ProductionExecutionError(
                "WEEKLY_PLAN_LATE_FILL_WINDOW_CLOSED"
            )
        return server_now

    def _require_live_matches_context(
        self,
        live: WeeklyPlan,
        weekly: TrustedWeeklyPlanContext,
    ) -> None:
        self._require_plan_identity(
            live,
            expected=self._trusted_identity(weekly),
        )
        if live.version != weekly.version:
            raise ProductionExecutionError("WEEKLY_PLAN_VERSION_MISMATCH")

    def _trusted_identity(
        self,
        weekly: TrustedWeeklyPlanContext,
    ) -> WeeklyPlan:
        return WeeklyPlan(
            plan_id=weekly.plan_id,
            batch_id=weekly.batch_id,
            tenant_id=weekly.tenant_id,
            owner_user_id=weekly.owner_user_id,
            target_week_start=weekly.target_week_start,
            status="collecting",
            version=weekly.version,
            days=(),
        )

    @staticmethod
    def _require_plan_identity(
        plan: WeeklyPlan,
        *,
        expected: WeeklyPlan,
    ) -> None:
        if (
            plan.plan_id != expected.plan_id
            or plan.batch_id != expected.batch_id
            or plan.tenant_id != expected.tenant_id
            or plan.owner_user_id != expected.owner_user_id
            or plan.target_week_start != expected.target_week_start
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_SCOPE_MISMATCH")

    def _require_empty_virtual_context(
        self,
        plan: WeeklyPlan,
        weekly: TrustedWeeklyPlanContext,
    ) -> None:
        if (
            plan.version != 0
            or plan.status != "collecting"
            or weekly.suggestions
            or any(day.state != "unfilled" or day.items for day in weekly.days)
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_CONTEXT_STALE")

    def _require_empty_created_plan(self, plan: WeeklyPlan) -> None:
        self._require_week_shape(plan)
        if (
            plan.version != 0
            or plan.status != "collecting"
            or plan.suggestions
            or any(day.state != "unfilled" or day.items for day in plan.days)
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_CREATE_CONFLICT")

    @staticmethod
    def _require_week_shape(plan: WeeklyPlan) -> None:
        expected_dates = tuple(
            plan.target_week_start + timedelta(days=offset)
            for offset in range(6)
        )
        if (
            len(plan.days) != 6
            or tuple(day.plan_date for day in plan.days) != expected_dates
            or len({day.day_id for day in plan.days}) != 6
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_WEEK_SHAPE_INVALID")
        item_ids = [
            item.item_id for day in plan.days for item in day.items
        ]
        if len(item_ids) != len(set(item_ids)):
            raise ProductionExecutionError("WEEKLY_PLAN_WEEK_SHAPE_INVALID")
        for day in plan.days:
            if (
                (day.state == "planned" and not day.items)
                or (day.state != "planned" and bool(day.items))
            ):
                raise ProductionExecutionError("WEEKLY_PLAN_WEEK_SHAPE_INVALID")

    def _apply_commands(
        self,
        request: ProductionHandlerRequest,
        arguments: ApplyNextWeeklyPlanArgs,
        plan: WeeklyPlan,
    ) -> tuple[WeeklyPlanCommand, ...]:
        valid_dates = {day.plan_date for day in plan.days}
        item_ids = {item.item_id for day in plan.days for item in day.items}
        suggestion_ids = {
            item.suggestion_id
            for item in plan.suggestions
            if item.status is SuggestionStatus.AVAILABLE
        }
        commands: list[WeeklyPlanCommand] = []
        for ordinal, operation in enumerate(arguments.operations, start=1):
            operation_name = operation.operation
            patch: dict[str, Any]
            command_type: str
            if operation_name == "add":
                if operation.plan_date not in valid_dates:
                    raise ProductionExecutionError(
                        "WEEKLY_PLAN_DATE_OUT_OF_SCOPE"
                    )
                command_type = "add_item"
                patch = {
                    "plan_date": operation.plan_date.isoformat(),
                    "original_text": operation.content,
                    "source": "manual",
                }
            elif operation_name == "edit":
                if operation.item_id not in item_ids:
                    raise ProductionExecutionError("WEEKLY_PLAN_ITEM_NOT_FOUND")
                command_type = "edit_item"
                patch = {
                    "item_id": operation.item_id,
                    "original_text": operation.content,
                }
            elif operation_name == "move":
                if operation.item_id not in item_ids:
                    raise ProductionExecutionError("WEEKLY_PLAN_ITEM_NOT_FOUND")
                if operation.target_plan_date not in valid_dates:
                    raise ProductionExecutionError(
                        "WEEKLY_PLAN_DATE_OUT_OF_SCOPE"
                    )
                command_type = "move_item"
                patch = {
                    "item_id": operation.item_id,
                    "plan_date": operation.target_plan_date.isoformat(),
                }
            elif operation_name == "delete":
                if operation.item_id not in item_ids:
                    raise ProductionExecutionError("WEEKLY_PLAN_ITEM_NOT_FOUND")
                command_type = "delete_item"
                patch = {"item_id": operation.item_id}
            elif operation_name == "set_day_empty":
                if operation.plan_date not in valid_dates:
                    raise ProductionExecutionError(
                        "WEEKLY_PLAN_DATE_OUT_OF_SCOPE"
                    )
                command_type = "set_day_empty"
                patch = {"plan_date": operation.plan_date.isoformat()}
            elif operation_name == "accept_suggestion":
                if operation.suggestion_id not in suggestion_ids:
                    raise ProductionExecutionError(
                        "WEEKLY_PLAN_SUGGESTION_NOT_AVAILABLE"
                    )
                if operation.plan_date not in valid_dates:
                    raise ProductionExecutionError(
                        "WEEKLY_PLAN_DATE_OUT_OF_SCOPE"
                    )
                command_type = "accept_suggestion"
                patch = {
                    "suggestion_id": operation.suggestion_id,
                    "plan_date": operation.plan_date.isoformat(),
                }
            elif operation_name == "reject_suggestion":
                if operation.suggestion_id not in suggestion_ids:
                    raise ProductionExecutionError(
                        "WEEKLY_PLAN_SUGGESTION_NOT_AVAILABLE"
                    )
                command_type = "reject_suggestion"
                patch = {"suggestion_id": operation.suggestion_id}
            elif operation_name == "capture_suggestion":
                if self._current_turn_source is None:
                    raise ProductionExecutionError(
                        "CURRENT_TURN_SOURCE_REQUIRED"
                    )
                source_index = operation.source_evidence.source_message_index - 1
                try:
                    evidence_text = self._current_turn_source.messages[source_index]
                except IndexError as exc:  # also checked by CurrentTurnSource
                    raise ProductionExecutionError(
                        "CURRENT_MESSAGE_EVIDENCE_MISMATCH"
                    ) from exc
                expiry = datetime.combine(
                    plan.target_week_start + timedelta(days=7),
                    time.min,
                    tzinfo=self._context.now.tzinfo,
                )
                command_type = "capture_suggestion"
                patch = {
                    "matter_excerpt": operation.content,
                    "evidence_text": evidence_text,
                    "source_version": hashlib.sha256(
                        evidence_text.encode("utf-8")
                    ).hexdigest(),
                    "expires_at": expiry.isoformat(),
                }
            else:  # Pydantic's discriminator should make this unreachable.
                raise ProductionExecutionError(
                    "WEEKLY_PLAN_OPERATION_UNSUPPORTED"
                )
            commands.append(
                self._command(
                    request=request,
                    operation_id=operation.operation_id,
                    command_type=command_type,
                    expected_version=arguments.expected_version,
                    patch=patch,
                    ordinal=ordinal,
                    arguments=arguments,
                )
            )
        return tuple(commands)

    def _preflight_batch(
        self,
        commands: tuple[WeeklyPlanCommand, ...],
        *,
        plan: WeeklyPlan,
    ) -> None:
        execution = execute_weekly_plan_batch(
            commands,
            plan=plan,
            executed_at=self._context.now,
        )
        if execution.receipt.status != "executed":
            raise ProductionExecutionError(
                _domain_error_code(execution.receipt.reason_code)
            )

    def _command(
        self,
        *,
        request: ProductionHandlerRequest,
        operation_id: str,
        command_type: str,
        expected_version: int,
        patch: dict[str, Any],
        ordinal: int,
        arguments: ApplyNextWeeklyPlanArgs | SubmitNextWeeklyPlanArgs,
    ) -> WeeklyPlanCommand:
        principal = self._context.principal
        identity = ":".join(
            (
                principal.tenant_id,
                principal.source_message_id,
                request.tool_call_id,
                str(ordinal),
                operation_id,
            )
        )
        return WeeklyPlanCommand(
            command_id=str(uuid5(NAMESPACE_URL, f"{identity}:weekly-command")),
            command_type=command_type,
            tenant_id=principal.tenant_id,
            actor_user_id=str(principal.user_id),
            plan_id=str(arguments.plan_id),
            expected_version=expected_version,
            idempotency_key=(
                f"{self._tool_idempotency_key(request, arguments)}:weekly:{ordinal}"
            ),
            source_message_id=principal.source_message_id,
            patch=patch,
        )

    def _validate_write_evidence(
        self,
        request: ProductionHandlerRequest,
        arguments: ApplyNextWeeklyPlanArgs | SubmitNextWeeklyPlanArgs,
    ) -> None:
        self._bound(request)
        if self._current_turn_source is None:
            raise ProductionExecutionError("CURRENT_TURN_SOURCE_REQUIRED")
        try:
            self._current_turn_source.validate_tool_arguments(
                request.tool_name,
                arguments.model_dump(mode="json"),
            )
        except CurrentTurnSourceEvidenceError as exc:
            raise ProductionExecutionError(exc.code) from exc

    def _bound(self, request: ProductionHandlerRequest) -> BoundCall:
        bound = self._bound_calls.get(request.tool_call_id)
        if bound is None or bound.call.tool_name != request.tool_name:
            raise ProductionExecutionError("BOUND_TOOL_CALL_REQUIRED")
        dumped = request.arguments.model_dump(mode="json")
        if bound.arguments != dumped:
            raise ProductionExecutionError("BOUND_TOOL_ARGUMENTS_CHANGED")
        return bound

    def _weekly_target(
        self,
        request: ProductionHandlerRequest,
    ) -> TrustedWeeklyPlanContext:
        bound = self._bound(request)
        weekly = bound.weekly_plan
        if weekly is None:
            raise ProductionExecutionError("BOUND_WEEKLY_PLAN_REQUIRED")
        if self._context.weekly_plan_by_id(weekly.plan_id) != weekly:
            raise ProductionExecutionError("BOUND_WEEKLY_PLAN_UNTRUSTED")
        return weekly

    @staticmethod
    def _arguments(request: ProductionHandlerRequest, model_type):
        if not isinstance(request.arguments, model_type):
            raise ProductionExecutionError("TYPED_ARGUMENTS_REQUIRED")
        return request.arguments

    def _tool_idempotency_key(
        self,
        request: ProductionHandlerRequest,
        arguments: ApplyNextWeeklyPlanArgs | SubmitNextWeeklyPlanArgs,
    ) -> str:
        principal = self._context.principal
        return build_write_idempotency_key(
            tenant_id=principal.tenant_id,
            user_id=str(principal.user_id),
            conversation_id=principal.conversation_id,
            source_message_id=principal.source_message_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            canonical_arguments=arguments.model_dump(mode="json"),
            target_object=f"weekly_plan:{arguments.plan_id}",
            expected_version=arguments.expected_version,
        )

    def _outcome(
        self,
        *,
        plan: WeeklyPlan,
        before_version: int,
        actual_write: bool,
        idempotency_key: str | None,
        affected_item_ids: tuple[str, ...] = (),
        status_if_unchanged: ReceiptStatus = ReceiptStatus.NO_OP,
        error_code: str | None = None,
        unresolved_dates: tuple[str, ...] = (),
    ) -> ProductionHandlerOutcome:
        facts = {
            "actual_write": actual_write,
            "formal_plan": _formal_plan_preview(
                plan,
                unresolved_dates=unresolved_dates,
            ),
            "suggestion_zone": _suggestion_zone(
                plan,
                as_of=self._context.now,
            ),
        }
        return ProductionHandlerOutcome(
            target_type="weekly_plan",
            target_id=plan.plan_id,
            before_report=None,
            after_report=None,
            idempotency_key=idempotency_key,
            affected_item_ids=affected_item_ids,
            safe_user_facts=facts,
            status_if_unchanged=status_if_unchanged,
            before_version=before_version,
            after_version=plan.version,
            error_code=error_code,
        )


def _configured_canary_roster_user_ids(
    settings: object | None,
    *,
    tenant_id: str,
) -> tuple[str, ...] | None:
    if settings is None:
        return None
    tenant_raw = getattr(
        settings,
        "agent2_weekly_plan_tenant_allowlist",
        "",
    )
    user_raw = getattr(
        settings,
        "agent2_weekly_plan_user_allowlist",
        "",
    )
    if not isinstance(tenant_raw, str) or tenant_raw != tenant_id:
        return None
    if not isinstance(user_raw, str) or not user_raw:
        return None
    parts = user_raw.split(",")
    if not 1 <= len(parts) <= 2 or len(parts) != len(set(parts)):
        return None
    if any(
        item != item.strip()
        or not item
        or not item.isascii()
        or any(character.isspace() for character in item)
        for item in parts
    ):
        return None
    return tuple(sorted(parts))


def _formal_plan_preview(
    plan: WeeklyPlan,
    *,
    unresolved_dates: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "target_week_start": plan.target_week_start.isoformat(),
        "version": plan.version,
        "status": plan.status,
        "days": [
            {
                "day_id": day.day_id,
                "plan_date": day.plan_date.isoformat(),
                "state": day.state,
                "items": [
                    {
                        "item_id": item.item_id,
                        "content": item.original_text,
                        "source": item.source,
                        "source_ref": item.source_ref,
                    }
                    for item in day.items
                ],
            }
            for day in plan.days
        ],
        "unresolved_dates": list(unresolved_dates),
        "provenance": "server_weekly_plan",
    }


def _suggestion_zone(
    plan: WeeklyPlan,
    *,
    as_of: datetime,
) -> list[dict[str, Any]]:
    return [
        {
            "suggestion_id": suggestion.suggestion_id,
            "prompt": render_suggestion_prompt(suggestion),
            "evidence_excerpt": suggestion.matter_excerpt,
            "source_kind": suggestion.source_kind.value,
            "source_ref": suggestion.source_ref,
            "source_version": suggestion.source_version,
            "is_formal_plan_item": False,
        }
        for suggestion in plan.suggestions
        if suggestion.status is SuggestionStatus.AVAILABLE
        and as_of < suggestion.expires_at
    ]


def _affected_item_ids(
    before: WeeklyPlan,
    after: WeeklyPlan,
) -> tuple[str, ...]:
    before_items = {
        item.item_id: (day.plan_date, item.original_text)
        for day in before.days
        for item in day.items
    }
    after_items = {
        item.item_id: (day.plan_date, item.original_text)
        for day in after.days
        for item in day.items
    }
    return tuple(
        sorted(
            item_id
            for item_id in before_items.keys() | after_items.keys()
            if before_items.get(item_id) != after_items.get(item_id)
        )
    )


def _domain_error_code(reason: str) -> str:
    return f"WEEKLY_PLAN_{str(reason).strip().upper()}"


def _store_error_code(exc: BaseException) -> str:
    value = str(exc).strip()
    return _domain_error_code(value or "STORE_ERROR")


__all__ = ["ProductionWeeklyPlanExecutor", "WeeklyPlanStorePort"]
