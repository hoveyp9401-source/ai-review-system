from __future__ import annotations

from typing import Any

from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.repositories import CaseSqlRepository
from app.agent2.selection_pending import (
    SelectionCandidate,
    SelectionContext,
    SelectionPending,
    SelectionValidation,
)
from app.agent2.selection_pending_runtime import (
    SelectionContinuationCoordinator,
    SelectionContinuationPreprocessRequest,
    SelectionContinuationPreprocessResult,
)


_metadata = MetaData()
_admission_traces = Table(
    "agent2_semantic_admission_traces",
    _metadata,
    Column("trace_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
)


class SqlSelectionSourceLedger:
    """Detect a repeated selection answer from committed Admission evidence."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def source_message_processed(self, context: SelectionContext) -> bool:
        result = await self._session.execute(
            select(_admission_traces.c.trace_id)
            .where(
                _admission_traces.c.tenant_id == context.tenant_id,
                _admission_traces.c.user_id == context.user_id,
                _admission_traces.c.conversation_id == context.conversation_id,
                _admission_traces.c.source_message_id == context.source_turn_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


class SqlSelectionCandidateValidator:
    """Revalidate the selected Case against the current permission-scoped view."""

    def __init__(
        self,
        session: Any,
        *,
        business_context: BusinessCommandContext,
    ) -> None:
        self._repository = CaseSqlRepository(session)
        self._context = business_context

    async def validate(
        self,
        pending: SelectionPending,
        candidate: SelectionCandidate,
        context: SelectionContext,
    ) -> SelectionValidation:
        if (
            pending.domain != "case"
            or pending.operation != "record_case_progress"
        ):
            return SelectionValidation("illegal", "unsupported_selection_domain")
        if (
            context.tenant_id != self._context.tenant_id
            or context.user_id != self._context.actor_user_id
            or context.conversation_id != self._context.conversation_id
        ):
            return SelectionValidation("forbidden", "selection_scope_changed")
        visible = await self._repository.list_visible(self._context)
        current = next(
            (item for item in visible if item.case_id == candidate.stable_id),
            None,
        )
        if current is None:
            return SelectionValidation(
                "not_found", "candidate_missing_or_forbidden"
            )
        if current.version != candidate.version:
            return SelectionValidation(
                "version_conflict", "candidate_version_changed"
            )
        return SelectionValidation.valid()


class SqlSelectionPendingContinuationAdapter:
    """SQL-backed read/revalidation seam; it never executes a business write."""

    def __init__(
        self,
        *,
        coordinator: SelectionContinuationCoordinator | None = None,
    ) -> None:
        self._coordinator = coordinator or SelectionContinuationCoordinator()

    async def preprocess(
        self,
        request: SelectionContinuationPreprocessRequest,
        *,
        session: Any,
        business_context: BusinessCommandContext,
    ) -> SelectionContinuationPreprocessResult:
        return await self._coordinator.preprocess(
            request,
            validator=SqlSelectionCandidateValidator(
                session,
                business_context=business_context,
            ),
            source_ledger=SqlSelectionSourceLedger(session),
        )

