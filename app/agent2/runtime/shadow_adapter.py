from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import asyncio
import hashlib
import time
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from app.agent2.cognitive_core_v3 import SemanticInterpreter
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot

from .composition import compose_phase1_runtime
from .contracts import (
    InMemoryRuntimeAuditSink,
    RuntimeActor,
    RuntimeFailureOutcome,
    RuntimeTurnOutcome,
    RuntimeTurnRequest,
)
from .domains import InMemoryDailyDomainExecutor


@dataclass(frozen=True)
class ShadowAdapterConfig:
    enabled: bool = False
    kill_switch: bool = True
    sampling_rate: float = 1.0
    timeout_seconds: float = 2.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 60.0
    pii_hash_salt: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.sampling_rate <= 1.0:
            raise ValueError("Shadow sampling rate must be between zero and one")
        if self.timeout_seconds <= 0:
            raise ValueError("Shadow timeout must be positive")
        if self.circuit_failure_threshold < 1 or self.circuit_cooldown_seconds < 0:
            raise ValueError("Shadow circuit breaker configuration is invalid")
        if not self.pii_hash_salt:
            raise ValueError("Shadow PII hash salt is required")


@dataclass(frozen=True)
class ShadowRuntimeInput:
    """Read-only production input copy; it grants no production capability."""

    tenant_id: str
    actor_id: UUID
    conversation_id: str
    message_id: str
    text: str
    occurred_at: datetime
    channel: str
    trace_id: str
    daily_snapshot: DailyReportMutationSnapshot
    daily_policy: Mapping[str, object] | None = None
    active_tasks: Sequence[Mapping[str, object]] = ()

    def __post_init__(self) -> None:
        required = (
            self.tenant_id,
            str(self.actor_id),
            self.conversation_id,
            self.message_id,
            self.text,
            self.channel,
            self.trace_id,
        )
        if not all(required):
            raise ValueError("Shadow Runtime input requires identity, text, channel, and trace")
        if self.occurred_at.tzinfo is None:
            raise ValueError("Shadow Runtime input timestamp must be timezone-aware")
        if not isinstance(self.actor_id, UUID):
            raise TypeError("Shadow Runtime actor_id must be a UUID")
        if self.daily_snapshot.owner_user_id != self.actor_id:
            raise ValueError("Shadow snapshot owner must match the copied actor")


@dataclass(frozen=True)
class ShadowObservation:
    status: str
    reason: str
    trace_id: str
    input_text_hash: str
    runtime_status: str | None = None
    would_write: bool = False
    actual_write: bool = False
    legacy_fallback_used: bool = False
    duration_ms: int = 0

    def as_mapping(self) -> dict[str, Any]:
        # Deliberately excludes input text, identities, and user-facing reply.
        return {
            "status": self.status,
            "reason": self.reason,
            "trace_id": self.trace_id,
            "input_text_hash": self.input_text_hash,
            "runtime_status": self.runtime_status,
            "would_write": self.would_write,
            "actual_write": self.actual_write,
            "legacy_fallback_used": self.legacy_fallback_used,
            "duration_ms": self.duration_ms,
        }


class ShadowLogSink(Protocol):
    def record(self, event: Mapping[str, Any]) -> None: ...


class InMemoryShadowLogSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))


class OfflineShadowEvaluator:
    """Trusted evaluator closed over repository-owned ephemeral simulators."""

    def __init__(self, interpreter: SemanticInterpreter) -> None:
        self._interpreter = interpreter

    async def evaluate(
        self,
        copied_input: ShadowRuntimeInput,
    ) -> RuntimeTurnOutcome | RuntimeFailureOutcome:
        state_store = InMemoryConversationStateStore()
        daily = InMemoryDailyDomainExecutor(copied_input.daily_snapshot)
        runtime = compose_phase1_runtime(
            mode="shadow",
            interpreter=self._interpreter,
            state_store=state_store,
            daily=daily,
            daily_policy=copied_input.daily_policy,
            active_tasks=copied_input.active_tasks,
            audit_sink=InMemoryRuntimeAuditSink(),
        )
        return await runtime.handle(
            RuntimeTurnRequest(
                tenant_id=copied_input.tenant_id,
                actor=RuntimeActor(actor_id=copied_input.actor_id),
                conversation_id=copied_input.conversation_id,
                message_id=copied_input.message_id,
                text=copied_input.text,
                occurred_at=copied_input.occurred_at,
                channel=copied_input.channel,
                request_metadata={
                    "source": "readonly_shadow_copy",
                    "trace_id": copied_input.trace_id,
                },
            )
        )


class ProductionShadowCandidateAdapter:
    """Offline-verifiable boundary; this module does not connect or deploy it."""

    def __init__(
        self,
        *,
        evaluator: OfflineShadowEvaluator,
        config: ShadowAdapterConfig,
        log_sink: ShadowLogSink,
    ) -> None:
        if type(evaluator) is not OfflineShadowEvaluator:
            raise TypeError("Shadow Candidate requires the trusted offline evaluator")
        self._evaluator = evaluator
        self._config = config
        self._log_sink = log_sink
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None

    async def observe(self, copied_input: ShadowRuntimeInput) -> ShadowObservation:
        started = time.monotonic()
        if not self._config.enabled:
            return self._skipped(copied_input, "disabled", started)
        if self._config.kill_switch:
            return self._skipped(copied_input, "kill_switch", started)
        if self._circuit_is_open():
            return self._skipped(copied_input, "circuit_open", started)
        if not _sampled(copied_input.trace_id, self._config.sampling_rate):
            return self._skipped(copied_input, "not_sampled", started)

        try:
            outcome = await asyncio.wait_for(
                self._evaluator.evaluate(copied_input),
                timeout=self._config.timeout_seconds,
            )
            if outcome.actual_write or any(result.actual_write for result in outcome.domain_results):
                raise RuntimeError("offline Shadow evaluator reported an actual write")
        except TimeoutError:
            return self._failed(copied_input, "timeout", started)
        except Exception:
            return self._failed(copied_input, "runtime_failure", started)

        if isinstance(outcome, RuntimeFailureOutcome) or outcome.status == "failed_closed":
            return self._failed(copied_input, "runtime_failed_closed", started)

        observation = ShadowObservation(
            status="observed",
            reason="completed",
            trace_id=copied_input.trace_id,
            input_text_hash=_hash(copied_input.text),
            runtime_status=outcome.status,
            would_write=outcome.would_write,
            actual_write=False,
            legacy_fallback_used=outcome.legacy_fallback_used,
            duration_ms=_duration_ms(started),
        )
        if not self._record_safely(copied_input, observation):
            return self._log_failure(copied_input, started)
        self._consecutive_failures = 0
        self._circuit_opened_at = None
        return observation

    def _skipped(
        self,
        copied_input: ShadowRuntimeInput,
        reason: str,
        started: float,
    ) -> ShadowObservation:
        observation = ShadowObservation(
            status="skipped",
            reason=reason,
            trace_id=copied_input.trace_id,
            input_text_hash=_hash(copied_input.text),
            duration_ms=_duration_ms(started),
        )
        self._record_safely(copied_input, observation)
        return observation

    def _failed(
        self,
        copied_input: ShadowRuntimeInput,
        reason: str,
        started: float,
    ) -> ShadowObservation:
        self._register_failure()
        observation = ShadowObservation(
            status="failed",
            reason=reason,
            trace_id=copied_input.trace_id,
            input_text_hash=_hash(copied_input.text),
            duration_ms=_duration_ms(started),
        )
        self._record_safely(copied_input, observation)
        return observation

    def _log_failure(
        self,
        copied_input: ShadowRuntimeInput,
        started: float,
    ) -> ShadowObservation:
        self._register_failure()
        return ShadowObservation(
            status="failed",
            reason="log_failure",
            trace_id=copied_input.trace_id,
            input_text_hash=_hash(copied_input.text),
            duration_ms=_duration_ms(started),
        )

    def _register_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._config.circuit_failure_threshold:
            self._circuit_opened_at = time.monotonic()

    def _circuit_is_open(self) -> bool:
        if self._circuit_opened_at is None:
            return False
        if time.monotonic() - self._circuit_opened_at < self._config.circuit_cooldown_seconds:
            return True
        self._circuit_opened_at = None
        self._consecutive_failures = 0
        return False

    def _record(
        self,
        copied_input: ShadowRuntimeInput,
        observation: ShadowObservation,
    ) -> None:
        salt = self._config.pii_hash_salt
        self._log_sink.record(
            {
                **observation.as_mapping(),
                "tenant_hash": _salted_hash(salt, copied_input.tenant_id),
                "actor_hash": _salted_hash(salt, copied_input.actor_id),
                "conversation_hash": _salted_hash(salt, copied_input.conversation_id),
                "message_hash": _salted_hash(salt, copied_input.message_id),
                "input_length": len(copied_input.text),
            }
        )

    def _record_safely(
        self,
        copied_input: ShadowRuntimeInput,
        observation: ShadowObservation,
    ) -> bool:
        try:
            self._record(copied_input, observation)
        except Exception:
            return False
        return True


def _sampled(trace_id: str, sampling_rate: float) -> bool:
    if sampling_rate <= 0:
        return False
    if sampling_rate >= 1:
        return True
    bucket = int(_hash(trace_id)[:13], 16) / float(0xFFFFFFFFFFFFF)
    return bucket < sampling_rate


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _salted_hash(salt: str, value: object) -> str:
    return _hash(f"{salt}:{value}")


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
