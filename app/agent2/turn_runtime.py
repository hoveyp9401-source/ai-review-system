from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
import hashlib
import logging
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.information_pending_runtime import (
    InformationContinuationPreprocessRequest,
    InformationContinuationPreprocessResult,
)
from app.agent2.selection_pending import answer_may_target_selection
from app.agent2.selection_pending_runtime import (
    SelectionContinuationPreprocessRequest,
    SelectionContinuationPreprocessResult,
)
from app.agent2.typed_daily_executor import TypedDailyExecutionContext
from app.utils.time import now_in_timezone
from app.workflows.intake import IncomingMessageEnvelope


TurnEvaluator = Callable[..., Awaitable[Any]]
AdmissionArtifactPersistenceStatus = Literal[
    "not_applicable",
    "not_configured_shadow",
    "persisted",
    "failed_shadow",
]

logger = logging.getLogger(__name__)


class VerifiedTurnRejected(ValueError):
    """The adapter-supplied identity/source scope is not internally consistent."""


class InformationContinuationBlocked(VerifiedTurnRejected):
    """A typed, zero-write Information Pending continuation block."""

    def __init__(self, result: InformationContinuationPreprocessResult) -> None:
        self.result = result
        super().__init__(f"information_continuation_{result.status}:{result.reason}")


class SelectionContinuationBlocked(VerifiedTurnRejected):
    """A typed, zero-write Selection Pending continuation block."""

    def __init__(self, result: SelectionContinuationPreprocessResult) -> None:
        self.result = result
        super().__init__(f"selection_continuation_{result.status}:{result.reason}")


@dataclass(frozen=True)
class VerifiedTurnRejectionReply:
    """A deterministic, domain-neutral explanation for a zero-write turn."""

    message: str
    reply_kind: str


def verified_turn_rejection_reply(
    error: VerifiedTurnRejected,
) -> VerifiedTurnRejectionReply:
    """Translate a trusted runtime block without exposing contract internals.

    A rejection is a successful fail-closed decision, not an LLM outage.  The
    adapter must therefore explain the safe recovery path and must never imply
    that a report, case, or travel mutation occurred.
    """

    if isinstance(error, SelectionContinuationBlocked):
        return _pending_rejection_reply(
            pending_kind="selection",
            status=str(error.result.status),
        )
    if isinstance(error, InformationContinuationBlocked):
        return _pending_rejection_reply(
            pending_kind="information",
            status=str(error.result.status),
        )
    if str(error) == "pending_context_not_unique":
        return VerifiedTurnRejectionReply(
            message=(
                "我这里同时有不止一个待处理问题，单凭这句话无法确定你指哪一个。"
                "本次没有做任何写入，请把对象和要处理的内容一起说完整。"
            ),
            reply_kind="pending_context_not_unique",
        )
    return VerifiedTurnRejectionReply(
        message=(
            "当前上下文无法安全确认你要处理的对象或操作，"
            "本次没有写入任何业务数据。请把对象和要做的事一起说完整。"
        ),
        reply_kind="verified_turn_rejected",
    )


def _pending_rejection_reply(
    *,
    pending_kind: Literal["selection", "information"],
    status: str,
) -> VerifiedTurnRejectionReply:
    if status == "duplicate_source_message":
        message = "这条回复已经处理过了，本次没有重复写入。"
    elif status in {"already_consumed"}:
        message = (
            "刚才那个待处理问题已经完成，本次没有重复写入。"
            "如果要继续修改，请把对象和新的操作一起说完整。"
        )
    elif status == "expired":
        message = (
            "刚才那个待处理问题已经过期，本次没有写入。"
            "请把要处理的对象和内容重新说一遍。"
        )
    elif status == "permission_revoked":
        message = "当前对象的权限发生了变化，本次没有写入。请重新确认负责人或访问权限。"
    elif status in {"invalidated", "conflicted"}:
        message = (
            "刚才那个待处理对象已经发生变化，本次没有写入。"
            "请重新说明对象和要做的事。"
        )
    elif status == "clarification_required":
        if pending_kind == "selection":
            message = (
                "我还不能唯一确定你选的是哪一项，本次没有写入。"
                "请直接回复序号，或把对象和要处理的内容一起说完整。"
            )
        else:
            message = (
                "我还不能唯一确定你是在补充哪项信息，本次没有写入。"
                "请把对象和补充内容一起说完整。"
            )
    else:
        message = (
            "刚才那个待处理问题现在无法安全继续，本次没有写入。"
            "请把对象和要做的事一起说完整。"
        )
    return VerifiedTurnRejectionReply(
        message=message,
        reply_kind=f"{pending_kind}_{status}",
    )


@dataclass(frozen=True)
class AdmissionArtifactPersistenceRequest:
    """Authoritative issuance input; a sink must not consume any Ticket."""

    session: Any
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    admission_mode: str
    decision: Any
    trace: Any | None
    tickets: tuple[Any, ...]
    information_pendings: tuple[Any, ...]
    review_items: tuple[Any, ...] = ()
    deferred_events: tuple[Any, ...] = ()
    review_capture_enabled: bool = False
    deferred_capture_enabled: bool = False


class AdmissionArtifactSink(Protocol):
    """Persist one Admission decision and its artifacts atomically.

    Implementations issue artifacts only. Ticket validation, locking and
    consumption remain an Executor responsibility in the business-write
    transaction.
    """

    async def persist(
        self, request: AdmissionArtifactPersistenceRequest
    ) -> None: ...


class InformationContinuationPreprocessor(Protocol):
    async def preprocess(
        self,
        request: InformationContinuationPreprocessRequest,
    ) -> InformationContinuationPreprocessResult: ...

    async def settle(
        self,
        result: InformationContinuationPreprocessResult,
        *,
        outcome: Any,
        settled_trace_id: str,
        settled_at: datetime,
        session: Any,
    ) -> Any: ...


class SelectionContinuationPreprocessor(Protocol):
    async def preprocess(
        self,
        request: SelectionContinuationPreprocessRequest,
        *,
        session: Any,
        business_context: BusinessCommandContext,
    ) -> SelectionContinuationPreprocessResult: ...


@dataclass(frozen=True)
class VerifiedTurnRequest:
    """Trusted adapter input for one Agent2 cognitive turn.

    The request contains server-resolved identity and permission scope.  It is
    validated again by ``Agent2TurnRuntime`` before the semantic interpreter is
    invoked; message text never supplies any field in ``business_context``.
    """

    session: Any
    user: Any
    envelope: IncomingMessageEnvelope
    llm_client: Any
    daily_report: Any | None
    report_date: date
    settings: Any
    business_context: BusinessCommandContext | None = None


@dataclass(frozen=True)
class Agent2TurnRuntimeResult:
    orchestration: Any
    business_execution_context: BusinessCommandContext | None
    admission_tickets: tuple[Any, ...]
    information_pendings: tuple[Any, ...]
    admission_trace: Any | None
    admission_artifact_persistence_status: AdmissionArtifactPersistenceStatus
    admission_artifact_persistence_error: str
    report_date: date
    source: str
    source_text_hash: str
    tenant_id: str
    conversation_id: str
    source_turn_id: str
    occurred_at: datetime
    execution_started_at: datetime
    conversation_state_version: int
    admission_mode: str
    information_continuation: InformationContinuationPreprocessResult | None = None
    selection_continuation: SelectionContinuationPreprocessResult | None = None
    admission_review_items: tuple[Any, ...] = ()
    admission_deferred_events: tuple[Any, ...] = ()
    admission_review_capture_enabled: bool = False
    admission_deferred_capture_enabled: bool = False

    @property
    def mutation_execution_authority(self) -> str:
        if self.admission_mode == "enforced":
            return "semantic_ticket"
        if self.admission_mode in {"shadow", "disabled"}:
            return "legacy_user_compatibility"
        raise ValueError("untrusted admission mode")

    def daily_execution_context(self) -> TypedDailyExecutionContext:
        """Return the report executor scope from the same admitted snapshot."""

        business_context = self.business_execution_context
        return TypedDailyExecutionContext(
            report_date=self.report_date,
            source=f"agent2_v3_{self.source or 'unknown'}",
            source_text_hash=self.source_text_hash,
            tenant_id=self.tenant_id,
            conversation_id=self.conversation_id,
            source_turn_id=self.source_turn_id,
            occurred_at=self.occurred_at,
            execution_started_at=self.execution_started_at,
            conversation_state_version=self.conversation_state_version,
            company_id=(business_context.company_id if business_context else ""),
            department_id=(
                business_context.department_id if business_context else ""
            ),
            team_id=(business_context.team_id if business_context else ""),
            actor_role_ids=(
                tuple(business_context.actor_role_ids) if business_context else ()
            ),
            allowed_case_ids=(
                tuple(business_context.allowed_case_ids) if business_context else ()
            ),
        )


class Agent2TurnRuntime:
    """Single production seam for cognition and verified execution binding."""

    def __init__(
        self,
        *,
        evaluator: TurnEvaluator | None = None,
        admission_artifact_sink: AdmissionArtifactSink | None = None,
        information_continuation_preprocessor: InformationContinuationPreprocessor
        | None = None,
        selection_continuation_preprocessor: SelectionContinuationPreprocessor
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._evaluator = evaluator or _evaluate_cognitive_turn
        self._admission_artifact_sink = admission_artifact_sink
        self._information_continuation_preprocessor = (
            information_continuation_preprocessor
        )
        self._selection_continuation_preprocessor = (
            selection_continuation_preprocessor
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(self, request: VerifiedTurnRequest) -> Agent2TurnRuntimeResult:
        envelope, business_context, occurred_at = _verify_request(request)
        execution_started_at = self._clock()
        if execution_started_at.tzinfo is None:
            raise VerifiedTurnRejected("execution_clock_must_be_timezone_aware")

        capture_policy = _trusted_admission_capture_policy(
            request.settings,
            business_context=business_context,
            user_id=str(getattr(request.user, "id", "") or ""),
        )
        expected_admission_mode = capture_policy.mode
        information_continuation = await self._preprocess_information_continuation(
            request=request,
            envelope=envelope,
            business_context=business_context,
            occurred_at=occurred_at,
            admission_mode=expected_admission_mode,
        )
        selection_continuation = await self._preprocess_selection_continuation(
            request=request,
            envelope=envelope,
            business_context=business_context,
            occurred_at=occurred_at,
            admission_mode=expected_admission_mode,
        )
        if (
            information_continuation is not None
            and information_continuation.handled
            and selection_continuation is not None
            and selection_continuation.handled
        ):
            await _persist_pending_context_conflict(
                session=request.session,
                business_context=business_context,
                selection_continuation=selection_continuation,
                information_continuation=information_continuation,
                source_message_id=envelope.message_id,
                source_text=envelope.raw_text,
                occurred_at=occurred_at,
            )
            raise VerifiedTurnRejected("pending_context_not_unique")
        if (
            information_continuation is not None
            and information_continuation.handled
            and information_continuation.status != "ready_for_fresh_admission"
        ):
            raise InformationContinuationBlocked(information_continuation)
        if (
            selection_continuation is not None
            and selection_continuation.handled
            and selection_continuation.status != "ready_for_fresh_admission"
        ):
            if selection_continuation.status in {
                "expired",
                "invalidated",
                "permission_revoked",
            }:
                await _persist_terminal_selection_resolution(
                    session=request.session,
                    business_context=business_context,
                    conversation_id=envelope.conversation_id,
                    continuation=selection_continuation,
                )
            else:
                await _persist_selection_preprocess_resolution(
                    session=request.session,
                    business_context=business_context,
                    continuation=selection_continuation,
                    source_message_id=envelope.message_id,
                    source_text=envelope.raw_text,
                    occurred_at=occurred_at,
                )
            raise SelectionContinuationBlocked(selection_continuation)
        if (
            selection_continuation is not None
            and selection_continuation.handled
            and selection_continuation.status == "ready_for_fresh_admission"
        ):
            await _persist_selection_preprocess_resolution(
                session=request.session,
                business_context=business_context,
                continuation=selection_continuation,
                source_message_id=envelope.message_id,
                source_text=envelope.raw_text,
                occurred_at=occurred_at,
            )

        evaluator_kwargs = dict(
            session=request.session,
            user=request.user,
            envelope=envelope,
            llm_client=request.llm_client,
            daily_report=request.daily_report,
            report_date=request.report_date,
            settings=request.settings,
            business_context=business_context,
        )
        if (
            information_continuation is not None
            and information_continuation.status == "ready_for_fresh_admission"
        ):
            evaluator_kwargs["information_continuation"] = information_continuation
        if (
            selection_continuation is not None
            and selection_continuation.status == "ready_for_fresh_admission"
        ):
            evaluator_kwargs["selection_continuation"] = selection_continuation
        orchestration = await self._evaluator(**evaluator_kwargs)
        source_text_hash = hashlib.sha256(
            str(envelope.raw_text or "").encode("utf-8")
        ).hexdigest()
        decision_hash = str(
            getattr(getattr(orchestration, "decision", None), "source_text_hash", "")
            or ""
        )
        if decision_hash != source_text_hash:
            raise VerifiedTurnRejected("semantic_result_source_hash_mismatch")
        state_version = getattr(
            getattr(orchestration, "base_state", None), "version", None
        )
        if not isinstance(state_version, int) or state_version < 0:
            raise VerifiedTurnRejected("semantic_result_state_version_invalid")

        decision = orchestration.decision
        admission_mode = str(getattr(decision, "admission_mode", "disabled") or "")
        if admission_mode not in {"disabled", "shadow", "enforced"}:
            raise VerifiedTurnRejected("semantic_result_admission_mode_invalid")
        if admission_mode != expected_admission_mode:
            raise VerifiedTurnRejected("semantic_result_admission_mode_mismatch")
        tickets = tuple(getattr(decision, "admission_tickets", ()) or ())
        information_pendings = tuple(
            getattr(decision, "admission_information_pendings", ()) or ()
        )
        selection_requests = tuple(
            getattr(decision, "admission_selection_requests", ()) or ()
        )
        review_items = tuple(
            getattr(decision, "admission_review_items", ()) or ()
        )
        deferred_events = tuple(
            getattr(decision, "admission_deferred_events", ()) or ()
        )
        if review_items and not capture_policy.review_enabled:
            raise VerifiedTurnRejected("semantic_review_capture_not_enabled")
        if deferred_events and not capture_policy.deferred_enabled:
            raise VerifiedTurnRejected("semantic_deferred_capture_not_enabled")
        admission_trace = getattr(decision, "admission_trace", None)
        _validate_fresh_information_continuation(
            continuation=information_continuation,
            admission_mode=admission_mode,
            base_state_version=state_version,
            tickets=tickets,
            trace=admission_trace,
            source_message_id=envelope.message_id,
        )
        _validate_fresh_selection_continuation(
            continuation=selection_continuation,
            admission_mode=admission_mode,
            base_state_version=state_version,
            tickets=tickets,
            trace=admission_trace,
            source_message_id=envelope.message_id,
        )
        _validate_enforced_admission_lifecycle(
            admission_mode=admission_mode,
            orchestration=orchestration,
            base_state_version=state_version,
            business_context=business_context,
            envelope=envelope,
            information_pendings=information_pendings,
            selection_requests=selection_requests,
            admission_trace=admission_trace,
        )
        persistence_status, persistence_error = await self._persist_admission_artifacts(
            request=request,
            envelope=envelope,
            business_context=business_context,
            admission_mode=admission_mode,
            decision=decision,
            trace=admission_trace,
            tickets=tickets,
            information_pendings=information_pendings,
            review_items=review_items,
            deferred_events=deferred_events,
            review_capture_enabled=capture_policy.review_enabled,
            deferred_capture_enabled=capture_policy.deferred_enabled,
        )

        execution_context = (
            replace(
                business_context,
                conversation_state_version=state_version,
                execution_started_at=execution_started_at,
            )
            if business_context is not None
            else None
        )
        return Agent2TurnRuntimeResult(
            orchestration=orchestration,
            business_execution_context=execution_context,
            admission_tickets=tickets,
            information_pendings=information_pendings,
            admission_trace=admission_trace,
            admission_artifact_persistence_status=persistence_status,
            admission_artifact_persistence_error=persistence_error,
            report_date=request.report_date,
            source=envelope.source,
            source_text_hash=source_text_hash,
            tenant_id=(
                business_context.tenant_id
                if business_context is not None
                else "agent2-daily"
            ),
            conversation_id=envelope.conversation_id,
            source_turn_id=envelope.message_id,
            occurred_at=occurred_at,
            execution_started_at=execution_started_at,
            conversation_state_version=state_version,
            admission_mode=admission_mode,
            information_continuation=information_continuation,
            selection_continuation=selection_continuation,
            admission_review_items=review_items,
            admission_deferred_events=deferred_events,
            admission_review_capture_enabled=capture_policy.review_enabled,
            admission_deferred_capture_enabled=capture_policy.deferred_enabled,
        )

    async def settle_information_continuation(
        self,
        result: Agent2TurnRuntimeResult,
        *,
        outcome: Any,
        settled_trace_id: str,
        settled_at: datetime,
        session: Any,
    ) -> Any:
        """Consume a Pending only after its freshly admitted command receipt."""

        continuation = result.information_continuation
        if continuation is None or continuation.status != "ready_for_fresh_admission":
            raise ValueError("runtime result has no ready information continuation")
        preprocessor = self._information_continuation_preprocessor
        if preprocessor is None:
            raise ValueError("information continuation preprocessor is not configured")
        return await preprocessor.settle(
            continuation,
            outcome=outcome,
            settled_trace_id=settled_trace_id,
            settled_at=settled_at,
            session=session,
        )

    async def _preprocess_information_continuation(
        self,
        *,
        request: VerifiedTurnRequest,
        envelope: IncomingMessageEnvelope,
        business_context: BusinessCommandContext | None,
        occurred_at: datetime,
        admission_mode: str,
    ) -> InformationContinuationPreprocessResult | None:
        if admission_mode != "enforced" or business_context is None:
            return None
        preprocessor = self._information_continuation_preprocessor
        if preprocessor is None:
            # Existing turns remain supported, but a persisted Pending can only
            # be loaded by the production factory which always wires this seam.
            return None
        state_version = await _load_verified_conversation_state_version(
            session=request.session,
            tenant_id=business_context.tenant_id,
            user_id=business_context.actor_user_id,
            conversation_id=envelope.conversation_id,
        )
        return await preprocessor.preprocess(
            InformationContinuationPreprocessRequest(
                session=request.session,
                tenant_id=business_context.tenant_id,
                user_id=business_context.actor_user_id,
                conversation_id=envelope.conversation_id,
                conversation_state_user_key=(
                    f"{business_context.tenant_id}:{business_context.actor_user_id}"
                ),
                conversation_state_version=state_version,
                source_message_id=envelope.message_id,
                source_text=str(envelope.raw_text or ""),
                occurred_at=occurred_at,
                timezone_name=str(
                    getattr(request.user, "timezone", None)
                    or getattr(request.settings, "timezone", "Asia/Shanghai")
                ),
            )
        )

    async def _preprocess_selection_continuation(
        self,
        *,
        request: VerifiedTurnRequest,
        envelope: IncomingMessageEnvelope,
        business_context: BusinessCommandContext | None,
        occurred_at: datetime,
        admission_mode: str,
    ) -> SelectionContinuationPreprocessResult | None:
        if admission_mode != "enforced" or business_context is None:
            return None
        preprocessor = self._selection_continuation_preprocessor
        if preprocessor is None:
            return None
        state = await _load_verified_conversation_state(
            session=request.session,
            tenant_id=business_context.tenant_id,
            user_id=business_context.actor_user_id,
            conversation_id=envelope.conversation_id,
        )
        pendings = tuple(state.selection_pending or ())
        source_text = str(envelope.raw_text or "")
        if not pendings or not answer_may_target_selection(pendings, source_text):
            return None
        return await preprocessor.preprocess(
            SelectionContinuationPreprocessRequest(
                tenant_id=business_context.tenant_id,
                user_id=business_context.actor_user_id,
                conversation_id=envelope.conversation_id,
                conversation_state_version=state.version,
                source_message_id=envelope.message_id,
                source_text=source_text,
                occurred_at=occurred_at,
                pendings=pendings,
            ),
            session=request.session,
            business_context=business_context,
        )

    async def _persist_admission_artifacts(
        self,
        *,
        request: VerifiedTurnRequest,
        envelope: IncomingMessageEnvelope,
        business_context: BusinessCommandContext | None,
        admission_mode: str,
        decision: Any,
        trace: Any | None,
        tickets: tuple[Any, ...],
        information_pendings: tuple[Any, ...],
        review_items: tuple[Any, ...],
        deferred_events: tuple[Any, ...],
        review_capture_enabled: bool,
        deferred_capture_enabled: bool,
    ) -> tuple[AdmissionArtifactPersistenceStatus, str]:
        if admission_mode == "disabled":
            return "not_applicable", ""
        if business_context is None:
            # Enforced allowlisted turns are rejected earlier; this also keeps
            # an accidentally constructed Shadow request tenant-less.
            if admission_mode == "enforced":
                raise VerifiedTurnRejected("verified_business_context_required")
            return "not_configured_shadow", ""
        sink = self._admission_artifact_sink
        if sink is None:
            if admission_mode == "enforced":
                raise VerifiedTurnRejected("admission_artifact_sink_required")
            return "not_configured_shadow", ""
        persistence_request = AdmissionArtifactPersistenceRequest(
            session=request.session,
            tenant_id=business_context.tenant_id,
            user_id=business_context.actor_user_id,
            conversation_id=envelope.conversation_id,
            source_message_id=envelope.message_id,
            admission_mode=admission_mode,
            decision=decision,
            trace=trace,
            tickets=tickets,
            information_pendings=information_pendings,
            review_items=review_items,
            deferred_events=deferred_events,
            review_capture_enabled=review_capture_enabled,
            deferred_capture_enabled=deferred_capture_enabled,
        )
        try:
            await sink.persist(persistence_request)
        except Exception as exc:
            error_code = str(
                getattr(exc, "code", "") or type(exc).__name__
            )
            if admission_mode == "enforced":
                raise VerifiedTurnRejected(
                    "admission_artifact_persistence_failed"
                ) from exc
            logger.warning(
                "Agent2 Shadow Admission artifact sink failed "
                "tenant_id=%s user_id=%s conversation_id=%s source_message_id=%s "
                "error_code=%s",
                business_context.tenant_id,
                business_context.actor_user_id,
                envelope.conversation_id,
                envelope.message_id,
                error_code,
            )
            return "failed_shadow", error_code
        return "persisted", ""


def _verify_request(
    request: VerifiedTurnRequest,
) -> tuple[IncomingMessageEnvelope, BusinessCommandContext | None, datetime]:
    envelope = request.envelope
    user_id = str(getattr(request.user, "id", "") or "").strip()
    if not user_id or str(envelope.sender_id or "").strip() != user_id:
        raise VerifiedTurnRejected("verified_actor_mismatch")
    message_id = str(envelope.message_id or "").strip()
    if not message_id:
        raise VerifiedTurnRejected("verified_source_message_id_required")
    if request.llm_client is None:
        raise VerifiedTurnRejected("semantic_interpreter_required")

    context = request.business_context
    if context is None and _allowlisted_admission_user(request.settings, user_id):
        raise VerifiedTurnRejected("verified_business_context_required")
    if context is not None:
        if context.actor_user_id != user_id:
            raise VerifiedTurnRejected("verified_business_actor_mismatch")
        if context.source_message_id != message_id:
            raise VerifiedTurnRejected("verified_source_message_mismatch")

    envelope_conversation = str(envelope.conversation_id or "").strip()
    context_conversation = (
        str(context.conversation_id or "").strip() if context is not None else ""
    )
    if (
        envelope_conversation
        and context_conversation
        and envelope_conversation != context_conversation
    ):
        raise VerifiedTurnRejected("verified_conversation_mismatch")
    conversation_id = (
        envelope_conversation
        or context_conversation
        or f"agent2-direct:{user_id}"
    )
    occurred_at = (
        envelope.received_at
        or (context.occurred_at if context is not None else None)
        or now_in_timezone(
            getattr(request.user, "timezone", None)
            or getattr(request.settings, "timezone", "Asia/Shanghai")
        )
    )
    if occurred_at.tzinfo is None:
        raise VerifiedTurnRejected("verified_occurred_at_must_be_timezone_aware")
    verified_envelope = replace(
        envelope,
        conversation_id=conversation_id,
        received_at=occurred_at,
    )
    verified_context = (
        replace(
            context,
            conversation_id=conversation_id,
            occurred_at=occurred_at,
        )
        if context is not None
        else None
    )
    return verified_envelope, verified_context, occurred_at


def _allowlisted_admission_user(settings: Any, user_id: str) -> bool:
    if not bool(getattr(settings, "agent2_semantic_admission_enabled", False)):
        return False
    users = _csv_allowlist(
        getattr(settings, "agent2_semantic_admission_user_allowlist", "")
    )
    tenants = _csv_allowlist(
        getattr(settings, "agent2_semantic_admission_tenant_allowlist", "")
    )
    return bool(tenants) and user_id in users


def _trusted_admission_mode(
    settings: Any,
    *,
    business_context: BusinessCommandContext | None,
    user_id: str,
) -> str:
    return _trusted_admission_capture_policy(
        settings,
        business_context=business_context,
        user_id=user_id,
    ).mode


def _trusted_admission_capture_policy(
    settings: Any,
    *,
    business_context: BusinessCommandContext | None,
    user_id: str,
) -> Any:
    from app.agent2.domain_admission import AdmissionCapturePolicy

    if business_context is None:
        return AdmissionCapturePolicy()
    from app.agent2.cognitive_runtime_v3 import semantic_admission_capture_policy

    return semantic_admission_capture_policy(
        settings,
        tenant_id=business_context.tenant_id,
        user_id=user_id,
    )


def _csv_allowlist(raw: object) -> frozenset[str]:
    return frozenset(
        value.strip()
        for value in str(raw or "").split(",")
        if value.strip()
    )


def _validate_enforced_admission_lifecycle(
    *,
    admission_mode: str,
    orchestration: Any,
    base_state_version: int,
    business_context: BusinessCommandContext | None,
    envelope: IncomingMessageEnvelope,
    information_pendings: tuple[Any, ...],
    selection_requests: tuple[Any, ...],
    admission_trace: Any | None,
) -> None:
    """Fence non-writing Admission artifacts to the evaluated base snapshot."""

    if admission_mode != "enforced":
        return
    if business_context is None:
        raise VerifiedTurnRejected("verified_business_context_required")

    expected_scope = {
        "tenant_id": business_context.tenant_id,
        "user_id": business_context.actor_user_id,
        "conversation_id": envelope.conversation_id,
        "source_turn_id": envelope.message_id,
        "source_message_id": envelope.message_id,
    }
    for pending in information_pendings:
        pending_version = getattr(
            pending,
            "expected_conversation_state_version",
            None,
        )
        if (
            not isinstance(pending_version, int)
            or isinstance(pending_version, bool)
            or pending_version != base_state_version
        ):
            raise VerifiedTurnRejected(
                "information_pending_state_version_mismatch"
            )
        for field, expected_value in expected_scope.items():
            if str(getattr(pending, field, "") or "") != str(expected_value):
                raise VerifiedTurnRejected(
                    f"information_pending_{field}_mismatch"
                )
        if str(getattr(pending, "pending_status", "") or "") != "active":
            raise VerifiedTurnRejected("information_pending_not_active_at_issuance")
        if bool(getattr(pending, "business_write_allowed", True)):
            raise VerifiedTurnRejected("information_pending_cannot_authorize_write")

    _validate_enforced_selection_requests(
        requests=selection_requests,
        trace=admission_trace,
        business_context=business_context,
        envelope=envelope,
        base_state_version=base_state_version,
    )

    nonadvancing_statuses = {
        "blocked",
        "information_required",
        "review_only",
        "deferred_audit_only",
    }
    has_nonadvancing_decision = bool(information_pendings or selection_requests) or any(
        str(getattr(item, "status", "") or "") in nonadvancing_statuses
        for item in tuple(getattr(admission_trace, "decisions", ()) or ())
    )
    if has_nonadvancing_decision and bool(
        getattr(orchestration, "state_persisted", False)
    ):
        raise VerifiedTurnRejected(
            "admission_nonadvancing_turn_persisted_state"
        )


def _validate_enforced_selection_requests(
    *,
    requests: tuple[Any, ...],
    trace: Any | None,
    business_context: BusinessCommandContext,
    envelope: IncomingMessageEnvelope,
    base_state_version: int,
) -> None:
    if not requests:
        return
    trace_id = str(getattr(trace, "trace_id", "") or "")
    if not trace_id:
        raise VerifiedTurnRejected("selection_request_trace_missing")
    trace_decisions = tuple(getattr(trace, "decisions", ()) or ())
    expected_scope = {
        "tenant_id": business_context.tenant_id,
        "user_id": business_context.actor_user_id,
        "conversation_id": envelope.conversation_id,
        "source_turn_id": envelope.message_id,
        "source_message_id": envelope.message_id,
    }
    allowed_case_ids = frozenset(str(item) for item in business_context.allowed_case_ids)
    seen_request_ids: set[str] = set()
    source_text = str(envelope.raw_text or "")
    occurred_at = envelope.received_at

    for request in requests:
        request_id = str(getattr(request, "selection_request_id", "") or "")
        if not request_id or request_id in seen_request_ids:
            raise VerifiedTurnRejected("selection_request_identity_invalid")
        seen_request_ids.add(request_id)
        for field, expected_value in expected_scope.items():
            if str(getattr(request, field, "") or "") != str(expected_value):
                raise VerifiedTurnRejected(f"selection_request_{field}_mismatch")
        if (
            str(getattr(request, "domain", "") or "") != "case"
            or str(getattr(request, "operation", "") or "")
            != "record_case_progress"
        ):
            raise VerifiedTurnRejected("selection_request_contract_invalid")
        if bool(getattr(request, "business_write_allowed", True)):
            raise VerifiedTurnRejected("selection_request_cannot_authorize_write")

        pending_version = getattr(
            request, "expected_conversation_state_version", None
        )
        if (
            not isinstance(pending_version, int)
            or isinstance(pending_version, bool)
            or pending_version != base_state_version + 1
        ):
            raise VerifiedTurnRejected("selection_request_state_version_mismatch")

        created_at = getattr(request, "created_at", None)
        expires_at = getattr(request, "expires_at", None)
        if not all(
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
            for value in (created_at, expires_at, occurred_at)
        ):
            raise VerifiedTurnRejected("selection_request_time_invalid")
        if not (created_at <= occurred_at < expires_at):
            raise VerifiedTurnRejected("selection_request_not_active_at_issuance")

        start = getattr(request, "segment_start_offset", None)
        end = getattr(request, "segment_end_offset", None)
        expected_segment_hash = str(
            getattr(request, "segment_text_sha256", "") or ""
        )
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(source_text)
            or hashlib.sha256(source_text[start:end].encode("utf-8")).hexdigest()
            != expected_segment_hash
        ):
            raise VerifiedTurnRejected("selection_request_segment_mismatch")

        candidates = tuple(getattr(request, "candidates", ()) or ())
        candidate_ids = tuple(
            str(getattr(candidate, "stable_id", "") or "")
            for candidate in candidates
        )
        if (
            len(candidates) < 2
            or any(not candidate_id for candidate_id in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or not set(candidate_ids).issubset(allowed_case_ids)
        ):
            raise VerifiedTurnRejected("selection_request_candidate_scope_invalid")
        for candidate in candidates:
            candidate_version = getattr(candidate, "version", None)
            if (
                not isinstance(candidate_version, int)
                or isinstance(candidate_version, bool)
                or candidate_version < 0
                or not str(getattr(candidate, "label", "") or "").strip()
            ):
                raise VerifiedTurnRejected("selection_request_candidate_invalid")
        answer_forms = getattr(request, "acceptable_answer_forms", None)
        if not isinstance(answer_forms, Mapping) or not answer_forms:
            raise VerifiedTurnRejected("selection_request_answer_forms_invalid")
        answer_values = {
            str(value or "") for value in answer_forms.values() if str(value or "")
        }
        if answer_values != set(candidate_ids) or any(
            not str(key or "").strip() for key in answer_forms
        ):
            raise VerifiedTurnRejected("selection_request_answer_forms_invalid")

        decision_id = str(getattr(request, "decision_id", "") or "")
        matching_decisions = tuple(
            item
            for item in trace_decisions
            if str(getattr(item, "decision_id", "") or "") == decision_id
        )
        if (
            str(getattr(request, "trace_id", "") or "") != trace_id
            or len(matching_decisions) != 1
        ):
            raise VerifiedTurnRejected("selection_request_trace_binding_mismatch")
        decision = matching_decisions[0]
        evidence_refs = tuple(getattr(decision, "evidence_refs", ()) or ())
        if (
            str(getattr(decision, "status", "") or "") != "blocked"
            or str(getattr(decision, "reason_code", "") or "")
            != "case_reference_ambiguous"
            or str(getattr(decision, "action_id", "") or "")
            != str(getattr(request, "action_id", "") or "")
            or str(getattr(decision, "segment_id", "") or "")
            != str(getattr(request, "segment_id", "") or "")
            or f"selection_request:{request_id}" not in evidence_refs
        ):
            raise VerifiedTurnRejected("selection_request_decision_binding_mismatch")


def _validate_fresh_information_continuation(
    *,
    continuation: InformationContinuationPreprocessResult | None,
    admission_mode: str,
    base_state_version: int,
    tickets: tuple[Any, ...],
    trace: Any | None,
    source_message_id: str,
) -> None:
    if continuation is None or not continuation.handled:
        return
    if continuation.status != "ready_for_fresh_admission":
        raise InformationContinuationBlocked(continuation)
    if admission_mode != "enforced":
        raise VerifiedTurnRejected("information_continuation_requires_enforced_admission")
    pending = continuation.pending
    fresh = continuation.fresh_admission_request
    if pending is None or fresh is None:
        raise VerifiedTurnRejected("information_continuation_contract_missing")
    if base_state_version != pending.expected_conversation_state_version:
        raise VerifiedTurnRejected("information_continuation_state_version_drift")
    if len(tickets) != 1:
        raise VerifiedTurnRejected("information_continuation_single_fresh_ticket_required")
    ticket = tickets[0]
    authority = getattr(ticket, "authority_scope", None)
    if not isinstance(authority, Mapping):
        raise VerifiedTurnRejected("information_continuation_ticket_authority_missing")
    if str(getattr(trace, "trace_id", "") or "") in {"", pending.trace_id}:
        raise VerifiedTurnRejected("information_continuation_fresh_trace_required")
    if (
        str(getattr(ticket, "source_message_id", "") or "") != source_message_id
        or str(getattr(ticket, "source_turn_id", "") or "") != source_message_id
        or str(getattr(ticket, "domain", "") or "") != pending.domain
        or str(getattr(ticket, "operation", "") or "") != pending.operation
        or dict(getattr(ticket, "object_ref", {}) or {})
        != dict(pending.object_ref or {})
        or int(getattr(ticket, "expected_conversation_state_version", -1))
        != pending.expected_conversation_state_version
    ):
        raise VerifiedTurnRejected("information_continuation_ticket_scope_mismatch")
    if (
        str(authority.get("information_pending_id") or "") != pending.pending_id
        or str(authority.get("continuation_source_message_id") or "")
        != source_message_id
        or dict(authority.get("continuation_field_values") or {})
        != dict(fresh.field_values)
        or dict(authority.get("continuation_raw_values") or {})
        != dict(fresh.raw_values)
    ):
        raise VerifiedTurnRejected("information_continuation_ticket_claims_mismatch")


def _validate_fresh_selection_continuation(
    *,
    continuation: SelectionContinuationPreprocessResult | None,
    admission_mode: str,
    base_state_version: int,
    tickets: tuple[Any, ...],
    trace: Any | None,
    source_message_id: str,
) -> None:
    if continuation is None or not continuation.handled:
        return
    if continuation.status != "ready_for_fresh_admission":
        raise SelectionContinuationBlocked(continuation)
    if admission_mode != "enforced":
        raise VerifiedTurnRejected("selection_continuation_requires_enforced_admission")
    pending = continuation.pending
    candidate = continuation.candidate
    fresh = continuation.fresh_admission_request
    if pending is None or candidate is None or fresh is None:
        raise VerifiedTurnRejected("selection_continuation_contract_missing")
    if (
        base_state_version != pending.expected_conversation_state_version
        or fresh.pending_id != pending.pending_id
        or fresh.candidate_stable_id != candidate.stable_id
        or fresh.candidate_version != candidate.version
    ):
        raise VerifiedTurnRejected("selection_continuation_state_version_drift")
    if len(tickets) != 1:
        raise VerifiedTurnRejected("selection_continuation_single_fresh_ticket_required")
    ticket = tickets[0]
    authority = getattr(ticket, "authority_scope", None)
    if not isinstance(authority, Mapping):
        raise VerifiedTurnRejected("selection_continuation_ticket_authority_missing")
    if not str(getattr(trace, "trace_id", "") or "").strip():
        raise VerifiedTurnRejected("selection_continuation_fresh_trace_required")
    expected_object_ref = {
        "object_type": "case",
        "stable_id": candidate.stable_id,
        "version": candidate.version,
    }
    evidence = fresh.evidence.as_dict(
        segment_id=str(getattr(ticket, "segment_id", "") or "")
    )
    if (
        str(getattr(ticket, "source_message_id", "") or "")
        != source_message_id
        or str(getattr(ticket, "source_turn_id", "") or "")
        != source_message_id
        or str(getattr(ticket, "domain", "") or "") != fresh.domain
        or str(getattr(ticket, "operation", "") or "") != fresh.operation
        or dict(getattr(ticket, "object_ref", {}) or {}) != expected_object_ref
        or int(getattr(ticket, "expected_conversation_state_version", -1))
        != pending.expected_conversation_state_version
    ):
        raise VerifiedTurnRejected("selection_continuation_ticket_scope_mismatch")
    if (
        str(authority.get("selection_pending_id") or "") != pending.pending_id
        or str(authority.get("selection_pending_source_turn_id") or "")
        != pending.source_turn_id
        or str(authority.get("candidate_stable_id") or "") != candidate.stable_id
        or int(authority.get("candidate_version", -1)) != candidate.version
        or str(authority.get("original_source_digest") or "")
        != fresh.original_source_digest
        or str(authority.get("original_continuation_sha256") or "")
        != fresh.original_continuation_sha256
        or dict(authority.get("selection_evidence") or {}) != evidence
    ):
        raise VerifiedTurnRejected("selection_continuation_ticket_claims_mismatch")


async def _load_verified_conversation_state_version(
    *,
    session: Any,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
) -> int:
    state = await _load_verified_conversation_state(
        session=session,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    version = getattr(state, "version", None)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise VerifiedTurnRejected("information_continuation_state_version_invalid")
    return version


async def _load_verified_conversation_state(
    *,
    session: Any,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
) -> Any:
    from app.agent2.conversation_state_store import SQLAlchemyConversationStateStore

    return await SQLAlchemyConversationStateStore(session).load(
        user_id=f"{tenant_id}:{user_id}",
        conversation_id=conversation_id,
    )


async def _persist_terminal_selection_resolution(
    *,
    session: Any,
    business_context: BusinessCommandContext | None,
    conversation_id: str,
    continuation: SelectionContinuationPreprocessResult,
) -> None:
    """CAS one terminal Selection resolution before the safe reply is sent."""

    if continuation.status not in {"expired", "invalidated", "permission_revoked"}:
        return
    if business_context is None:
        raise VerifiedTurnRejected("selection_terminal_business_context_required")
    pending = continuation.pending
    resolution = continuation.resolution
    pending_after = getattr(resolution, "pending_after", None)
    if (
        pending is None
        or resolution is None
        or pending_after is None
        or pending_after.pending_id != pending.pending_id
        or pending_after.status not in {"expired", "invalidated"}
    ):
        raise VerifiedTurnRejected("selection_terminal_resolution_invalid")

    from app.agent2.conversation_state_store import (
        ConversationStateVersionConflict,
        SQLAlchemyConversationStateStore,
    )

    store = SQLAlchemyConversationStateStore(session)
    user_key = f"{business_context.tenant_id}:{business_context.actor_user_id}"
    current = await store.load(user_id=user_key, conversation_id=conversation_id)
    if (
        current.version != pending.expected_conversation_state_version
        or current.user_id != user_key
        or current.conversation_id != conversation_id
    ):
        raise VerifiedTurnRejected("selection_terminal_state_version_conflict")
    matching = tuple(
        item
        for item in current.selection_pending
        if item.pending_id == pending.pending_id
    )
    if len(matching) != 1 or matching[0] != pending or matching[0].status != "active":
        raise VerifiedTurnRejected("selection_terminal_pending_snapshot_changed")
    next_pendings = tuple(
        pending_after if item.pending_id == pending.pending_id else item
        for item in current.selection_pending
    )
    proposed = replace(
        current,
        version=current.version + 1,
        selection_pending=next_pendings,
    )
    from app.agent2.workflow_audit import persist_selection_terminal_audit

    try:
        async with session.begin_nested():
            await persist_selection_terminal_audit(
                session=session,
                pending=pending,
                resolution=resolution,
                business_context=business_context,
                source_message_id=business_context.source_message_id,
                occurred_at=business_context.occurred_at,
            )
            await store.save(proposed, expected_version=current.version)
    except ConversationStateVersionConflict as exc:
        raise VerifiedTurnRejected("selection_terminal_state_cas_conflict") from exc


async def _persist_selection_preprocess_resolution(
    *,
    session: Any,
    business_context: BusinessCommandContext | None,
    continuation: SelectionContinuationPreprocessResult,
    source_message_id: str,
    source_text: str,
    occurred_at: datetime,
) -> None:
    """Persist a zero-write Selection result without changing Pending state."""

    if business_context is None:
        raise VerifiedTurnRejected("selection_preprocess_business_context_required")
    from app.agent2.workflow_audit import persist_selection_preprocess_audit

    try:
        async with session.begin_nested():
            await persist_selection_preprocess_audit(
                session=session,
                continuation=continuation,
                business_context=business_context,
                source_message_id=source_message_id,
                source_text=source_text,
                occurred_at=occurred_at,
            )
    except Exception as exc:
        raise VerifiedTurnRejected("selection_preprocess_audit_failed") from exc


async def _persist_pending_context_conflict(
    *,
    session: Any,
    business_context: BusinessCommandContext | None,
    selection_continuation: SelectionContinuationPreprocessResult,
    information_continuation: InformationContinuationPreprocessResult,
    source_message_id: str,
    source_text: str,
    occurred_at: datetime,
) -> None:
    if business_context is None:
        raise VerifiedTurnRejected("pending_conflict_business_context_required")
    from app.agent2.workflow_audit import persist_pending_context_conflict_audit

    try:
        async with session.begin_nested():
            await persist_pending_context_conflict_audit(
                session=session,
                selection_continuation=selection_continuation,
                information_continuation=information_continuation,
                business_context=business_context,
                source_message_id=source_message_id,
                source_text=source_text,
                occurred_at=occurred_at,
            )
    except Exception as exc:
        raise VerifiedTurnRejected("pending_context_conflict_audit_failed") from exc


def production_agent2_turn_runtime(
    *,
    evaluator: TurnEvaluator | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Agent2TurnRuntime:
    """Build the one production Runtime seam shared by all three adapters."""

    from app.agent2.admission_artifact_sink_sql import SqlAdmissionArtifactSink
    from app.agent2.information_pending_sql import (
        SqlInformationPendingContinuationAdapter,
    )
    from app.agent2.selection_pending_sql import (
        SqlSelectionPendingContinuationAdapter,
    )

    return Agent2TurnRuntime(
        evaluator=evaluator,
        admission_artifact_sink=SqlAdmissionArtifactSink(),
        information_continuation_preprocessor=(
            SqlInformationPendingContinuationAdapter()
        ),
        selection_continuation_preprocessor=(
            SqlSelectionPendingContinuationAdapter()
        ),
        clock=clock,
    )


async def _evaluate_cognitive_turn(**kwargs: Any) -> Any:
    # Local import avoids making the compatibility runtime import this module.
    from app.agent2.cognitive_runtime_v3 import evaluate_cognitive_core_v3

    return await evaluate_cognitive_core_v3(**kwargs)
