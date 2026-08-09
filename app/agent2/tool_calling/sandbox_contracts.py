from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import hmac
import json
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    StrictContract,
    ToolReceipt,
)


SANDBOX_STATE_NAMESPACE = "agent2.tool_calling.sandbox.v1"
SANDBOX_SNAPSHOT_TABLES = (
    "daily_reports",
    "daily_items",
    "sandbox_pending",
    "personal_memory",
    "personal_memory_audit",
    "receipt",
)
SANDBOX_STATE_TABLES = SANDBOX_SNAPSHOT_TABLES[:-1]


class SandboxCapabilityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SandboxDatabaseConfig(StrictContract):
    sandbox_database_url: SecretStr
    production_database_url: SecretStr


class SandboxExecutionContext(StrictContract):
    namespace: Literal["agent2.tool_calling.sandbox.v1"] = SANDBOX_STATE_NAMESPACE
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    now: datetime
    conversation_id: str = Field(
        default="sandbox-foundation-conversation",
        min_length=1,
        max_length=256,
    )
    source_message_id: str = Field(
        default="sandbox-foundation-message",
        min_length=1,
        max_length=256,
    )
    turn_id: str = Field(
        default="sandbox-foundation-turn",
        min_length=1,
        max_length=256,
    )
    timezone: str = Field(default="UTC", min_length=1, max_length=128)

    @field_validator("now")
    @classmethod
    def now_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sandbox current time must be timezone-aware")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_an_iana_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("sandbox timezone must be a valid IANA zone") from exc
        return value


class SandboxIdentityScope(StrictContract):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    production_user_ids: frozenset[UUID] = Field(min_length=1)

    @field_validator("tenant_id")
    @classmethod
    def tenant_must_be_explicitly_marked_for_sandbox(cls, value: str) -> str:
        if "sandbox" not in value.casefold():
            raise ValueError("sandbox tenant must be explicitly marked")
        return value

    @model_validator(mode="after")
    def user_must_not_be_a_known_production_user(self) -> "SandboxIdentityScope":
        if self.user_id in self.production_user_ids:
            raise ValueError("sandbox identity cannot be a production user")
        return self


class SandboxExecutionCapability(StrictContract):
    sandbox_run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    database_fingerprint: str = Field(min_length=64, max_length=64)
    schema_name: str = Field(min_length=1, max_length=63)
    registry_digest: str = Field(min_length=64, max_length=64)
    expiry_time: datetime
    execution_mode: Literal["sandbox_execute"] = ExecutionMode.SANDBOX_EXECUTE
    conversation_id: str = Field(
        default="sandbox-foundation-conversation",
        min_length=1,
        max_length=256,
    )
    source_message_id: str = Field(
        default="sandbox-foundation-message",
        min_length=1,
        max_length=256,
    )
    turn_id: str = Field(
        default="sandbox-foundation-turn",
        min_length=1,
        max_length=256,
    )
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    allowed_tool_names: frozenset[str] = Field(default_factory=frozenset)
    write_gate_tool_names: frozenset[str] = Field(default_factory=frozenset)
    messages_disabled: Literal[True] = True
    production_route_disabled: Literal[True] = True
    server_signature: SecretStr

    @field_validator("expiry_time")
    @classmethod
    def expiry_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sandbox capability expiry must be timezone-aware")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_an_iana_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("sandbox timezone must be a valid IANA zone") from exc
        return value

    @model_validator(mode="after")
    def execution_scope_is_fail_closed(self) -> "SandboxExecutionCapability":
        if not self.write_gate_tool_names.issubset(self.allowed_tool_names):
            raise ValueError("sandbox write gate must be a subset of allowed tools")
        return self


class SandboxSchemaLease(StrictContract):
    sandbox_run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    database_fingerprint: str = Field(min_length=64, max_length=64)
    schema_name: str = Field(min_length=1, max_length=63)
    registry_digest: str = Field(min_length=64, max_length=64)
    expiry_time: datetime
    ownership_token: SecretStr = Field(min_length=32)

    @field_validator("expiry_time")
    @classmethod
    def expiry_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sandbox cleanup lease expiry must be timezone-aware")
        return value


class SandboxSnapshot(StrictContract):
    schema_name: str = Field(min_length=1, max_length=63)
    canonical_json: str = Field(min_length=1)
    canonical_hash: str = Field(min_length=64, max_length=64)
    state_hash: str = Field(min_length=64, max_length=64)

    @classmethod
    def capture(
        cls,
        *,
        schema_name: str,
        rows: Mapping[str, object],
    ) -> "SandboxSnapshot":
        if set(rows) != set(SANDBOX_SNAPSHOT_TABLES):
            raise ValueError("sandbox snapshot requires the exact table set")
        normalized_tables: dict[str, list[dict[str, Any]]] = {}
        for table_name in SANDBOX_SNAPSHOT_TABLES:
            raw_rows = rows[table_name]
            if not isinstance(raw_rows, (list, tuple)):
                raise TypeError("sandbox snapshot table rows must be a sequence")
            normalized = [_normalized_row(item) for item in raw_rows]
            normalized_tables[table_name] = sorted(
                normalized,
                key=_canonical_json,
            )
        payload = {
            "schema_name": schema_name,
            "tables": normalized_tables,
        }
        canonical_json = _canonical_json(payload)
        state_json = _canonical_json(
            {
                "schema_name": schema_name,
                "tables": {
                    table_name: normalized_tables[table_name]
                    for table_name in SANDBOX_STATE_TABLES
                },
            }
        )
        return cls(
            schema_name=schema_name,
            canonical_json=canonical_json,
            canonical_hash=_sha256(canonical_json),
            state_hash=_sha256(state_json),
        )

    @model_validator(mode="after")
    def hashes_must_match_canonical_payload(self) -> "SandboxSnapshot":
        payload = json.loads(self.canonical_json)
        if payload.get("schema_name") != self.schema_name:
            raise ValueError("sandbox snapshot schema does not match its payload")
        if set(payload.get("tables", {})) != set(SANDBOX_SNAPSHOT_TABLES):
            raise ValueError("sandbox snapshot payload has an invalid table set")
        if _canonical_json(payload) != self.canonical_json:
            raise ValueError("sandbox snapshot payload is not canonical")
        if _sha256(self.canonical_json) != self.canonical_hash:
            raise ValueError("sandbox snapshot canonical hash mismatch")
        state_json = _canonical_json(
            {
                "schema_name": self.schema_name,
                "tables": {
                    table_name: payload["tables"][table_name]
                    for table_name in SANDBOX_STATE_TABLES
                },
            }
        )
        if _sha256(state_json) != self.state_hash:
            raise ValueError("sandbox snapshot state hash mismatch")
        return self

    def table_rows(self, table_name: str) -> tuple[dict[str, Any], ...]:
        if table_name not in SANDBOX_SNAPSHOT_TABLES:
            raise KeyError(table_name)
        payload = json.loads(self.canonical_json)
        return tuple(payload["tables"][table_name])


class SandboxTransactionOutcome(StrictContract):
    atomic_group_id: str = Field(min_length=1, max_length=256)
    before: SandboxSnapshot
    attempted_after: SandboxSnapshot
    after: SandboxSnapshot
    committed: bool
    rolled_back: bool
    error_code: str | None = None

    @model_validator(mode="after")
    def transaction_has_one_terminal_state(self) -> "SandboxTransactionOutcome":
        if self.committed == self.rolled_back:
            raise ValueError("sandbox transaction must commit or roll back exactly once")
        return self


class SandboxReceiptFacts(StrictContract):
    receipt_id: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(
        default="sandbox-foundation-tenant",
        min_length=1,
        max_length=128,
    )
    user_id: str = Field(
        default="sandbox-foundation-user",
        min_length=1,
        max_length=128,
    )
    conversation_id: str = Field(
        default="sandbox-foundation-conversation",
        min_length=1,
        max_length=256,
    )
    source_message_id: str = Field(
        default="sandbox-foundation-message",
        min_length=1,
        max_length=256,
    )
    status: ReceiptStatus
    changed: bool
    target_type: str = Field(
        default="sandbox_foundation",
        min_length=1,
        max_length=128,
    )
    target_id: str = Field(default="foundation", min_length=1, max_length=256)
    before_version: int | None = Field(default=None, ge=0)
    after_version: int | None = Field(default=None, ge=0)
    affected_item_ids: tuple[str, ...] = ()
    error_code: str | None = None
    safe_user_facts: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    request_fingerprint: str = Field(default="0" * 64, min_length=64, max_length=64)
    operation_fingerprint: str = Field(
        default="0" * 64,
        min_length=64,
        max_length=64,
    )
    canonical_arguments_hash: str = Field(
        default="0" * 64,
        min_length=64,
        max_length=64,
    )
    before_state_hash: str = Field(min_length=64, max_length=64)
    after_state_hash: str = Field(min_length=64, max_length=64)
    execution_mode: Literal["sandbox_execute"] = ExecutionMode.SANDBOX_EXECUTE

    @model_validator(mode="after")
    def facts_are_internally_consistent(self) -> "SandboxReceiptFacts":
        from app.agent2.tool_calling.registry import TOOL_REGISTRY

        if self.tool_name not in TOOL_REGISTRY:
            raise ValueError("sandbox receipt tool must come from the Registry")
        if len(self.affected_item_ids) != len(set(self.affected_item_ids)):
            raise ValueError("sandbox receipt affected item IDs must be unique")
        if self.changed:
            if self.status != ReceiptStatus.SUCCESS:
                raise ValueError("only a successful sandbox receipt may claim a change")
            if self.target_type == "daily_report" and (
                self.before_version is None
                or self.after_version is None
                or self.after_version <= self.before_version
            ):
                raise ValueError(
                    "changed daily-report receipt must advance its version"
                )
            if self.target_type == "personal_memory" and (
                self.before_version is None
                or self.after_version is None
                or self.after_version != self.before_version + 1
            ):
                raise ValueError(
                    "changed personal-memory receipt must advance one version"
                )
            if self.before_state_hash == self.after_state_hash:
                raise ValueError("changed sandbox receipt must change state evidence")
        elif (
            self.before_version is not None
            and self.after_version is not None
            and self.before_version != self.after_version
        ):
            raise ValueError("unchanged sandbox receipt cannot advance its version")
        return self

    def canonical_row(self) -> dict[str, Any]:
        return _normalized_row(self.model_dump(mode="python"))


class SandboxExecutorEvidence(StrictContract):
    before: SandboxSnapshot
    after: SandboxSnapshot
    receipt: SandboxReceiptFacts


class SandboxReceiptEvidenceValidation(StrictContract):
    accepted: bool
    error_code: str | None = None

    @model_validator(mode="after")
    def rejection_has_an_error_code(self) -> "SandboxReceiptEvidenceValidation":
        if self.accepted == (self.error_code is not None):
            raise ValueError("sandbox receipt validation status and error code disagree")
        return self


class SandboxRuntimeResult(StrictContract):
    status: Literal["foundation_ready", "success", "blocked", "failed"]
    error_code: str | None = None
    transaction_opened: bool = False
    committed: bool = False
    rolled_back: bool = False
    before_snapshot_hash: str | None = None
    attempted_snapshot_hash: str | None = None
    after_snapshot_hash: str | None = None
    rollback_probe_staged: bool = False
    receipts: tuple[ToolReceipt, ...] = ()
    actual_write: bool = False
    handler_call_count: int = Field(default=0, ge=0)
    business_write_count: int = Field(default=0, ge=0)
    pending_write_count: int = Field(default=0, ge=0)
    memory_write_count: int = Field(default=0, ge=0)
    audit_write_count: int = Field(default=0, ge=0)
    conversation_state_write_count: Literal[0] = 0
    message_send_count: Literal[0] = 0

    @model_validator(mode="after")
    def result_matches_transaction_state(self) -> "SandboxRuntimeResult":
        if not self.transaction_opened and (self.committed or self.rolled_back):
            raise ValueError("unopened sandbox transaction cannot terminate")
        if self.status == "foundation_ready" and (
            not self.transaction_opened
            or self.committed
            or not self.rolled_back
            or not self.rollback_probe_staged
            or self.attempted_snapshot_hash is None
            or self.attempted_snapshot_hash == self.before_snapshot_hash
            or self.after_snapshot_hash != self.before_snapshot_hash
            or self.error_code is not None
        ):
            raise ValueError(
                "sandbox foundation success must prove a staged and reverted probe"
            )
        if self.status == "success" and (
            not self.transaction_opened
            or not self.committed
            or self.rolled_back
            or self.error_code is not None
            or not self.receipts
        ):
            raise ValueError(
                "sandbox execution success requires committed receipt evidence"
            )
        if self.status != "foundation_ready" and self.error_code is None:
            if self.status != "success":
                raise ValueError(
                    "blocked or failed sandbox result requires an error code"
                )
        if self.actual_write != (
            self.business_write_count > 0
            or self.pending_write_count > 0
            or self.memory_write_count > 0
            or self.audit_write_count > 0
        ):
            raise ValueError("sandbox write counters and actual-write flag disagree")
        return self


def validate_sandbox_receipt_evidence(
    executor_evidence: SandboxExecutorEvidence,
    *,
    database_after_flush: SandboxSnapshot,
    persisted_receipt_row: Mapping[str, object],
) -> SandboxReceiptEvidenceValidation:
    expected_row = executor_evidence.receipt.canonical_row()
    actual_row = _normalized_row(persisted_receipt_row)
    before = executor_evidence.before
    after = executor_evidence.after
    facts = executor_evidence.receipt
    schemas_match = (
        before.schema_name == after.schema_name == database_after_flush.schema_name
    )
    database_row_matches = any(
        _canonical_json(row) == _canonical_json(expected_row)
        for row in database_after_flush.table_rows("receipt")
    )
    evidence_matches = all(
        (
            schemas_match,
            facts.before_state_hash == before.state_hash,
            facts.after_state_hash == after.state_hash,
            after.state_hash == database_after_flush.state_hash,
            actual_row == expected_row,
            database_row_matches,
            facts.changed == (before.state_hash != after.state_hash),
        )
    )
    if not evidence_matches:
        return SandboxReceiptEvidenceValidation(
            accepted=False,
            error_code="RECEIPT_EVIDENCE_MISMATCH",
        )
    return SandboxReceiptEvidenceValidation(accepted=True)


class SandboxCapabilityAuthority:
    def __init__(
        self,
        signing_secret: bytes,
        *,
        identity_scope: SandboxIdentityScope,
    ) -> None:
        if len(signing_secret) < 32:
            raise ValueError("sandbox capability signing secret must be at least 32 bytes")
        self._signing_secret = bytes(signing_secret)
        self._identity_scope = identity_scope

    def issue(
        self,
        *,
        sandbox_run_id: str,
        tenant_id: str,
        user_id: UUID,
        database_fingerprint: str,
        schema_name: str,
        registry_digest: str,
        expiry_time: datetime,
        conversation_id: str = "sandbox-foundation-conversation",
        source_message_id: str = "sandbox-foundation-message",
        turn_id: str = "sandbox-foundation-turn",
        timezone: str = "UTC",
        allowed_tool_names: frozenset[str] = frozenset(),
        write_gate_tool_names: frozenset[str] = frozenset(),
    ) -> SandboxExecutionCapability:
        if (
            tenant_id != self._identity_scope.tenant_id
            or user_id != self._identity_scope.user_id
        ):
            raise SandboxCapabilityError("SANDBOX_IDENTITY_NOT_ALLOWLISTED")
        from app.agent2.tool_calling.registry import TOOL_REGISTRY

        unknown_tools = allowed_tool_names - frozenset(TOOL_REGISTRY)
        unknown_gates = write_gate_tool_names - frozenset(TOOL_REGISTRY)
        if unknown_tools or unknown_gates:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_UNKNOWN_TOOL")
        if not write_gate_tool_names.issubset(allowed_tool_names):
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_GATE_SCOPE_INVALID")
        if any(
            TOOL_REGISTRY[name].read_or_write != "write"
            for name in write_gate_tool_names
        ):
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_GATE_SCOPE_INVALID")
        payload = {
            "sandbox_run_id": sandbox_run_id,
            "tenant_id": tenant_id,
            "user_id": str(user_id),
            "database_fingerprint": database_fingerprint,
            "schema_name": schema_name,
            "registry_digest": registry_digest,
            "expiry_time": _canonical_time(expiry_time),
            "execution_mode": ExecutionMode.SANDBOX_EXECUTE.value,
            "conversation_id": conversation_id,
            "source_message_id": source_message_id,
            "turn_id": turn_id,
            "timezone": timezone,
            "allowed_tool_names": sorted(allowed_tool_names),
            "write_gate_tool_names": sorted(write_gate_tool_names),
            "messages_disabled": True,
            "production_route_disabled": True,
        }
        return SandboxExecutionCapability(
            **payload,
            server_signature=self._sign(payload),
        )

    def validate(
        self,
        capability: SandboxExecutionCapability,
        *,
        context: SandboxExecutionContext,
        database_fingerprint: str,
        schema_name: str,
        registry_digest: str,
        now: datetime,
    ) -> None:
        self._validate_binding(
            capability,
            context=context,
            database_fingerprint=database_fingerprint,
            schema_name=schema_name,
            registry_digest=registry_digest,
        )
        if capability.expiry_time <= now:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_EXPIRED")

    def issue_schema_lease(
        self,
        capability: SandboxExecutionCapability,
        *,
        expiry_time: datetime,
    ) -> SandboxSchemaLease:
        self._validate_signature_and_identity(capability)
        payload = {
            "sandbox_run_id": capability.sandbox_run_id,
            "tenant_id": capability.tenant_id,
            "user_id": str(capability.user_id),
            "database_fingerprint": capability.database_fingerprint,
            "schema_name": capability.schema_name,
            "registry_digest": capability.registry_digest,
            "expiry_time": _canonical_time(expiry_time),
        }
        return SandboxSchemaLease(
            **payload,
            ownership_token=self._sign(
                {
                    "purpose": "sandbox_schema_cleanup",
                    **payload,
                }
            ),
        )

    def validate_schema_cleanup(
        self,
        capability: SandboxExecutionCapability,
        lease: SandboxSchemaLease,
        *,
        context: SandboxExecutionContext,
        database_fingerprint: str,
        schema_name: str,
        registry_digest: str,
        now: datetime,
    ) -> None:
        self._validate_binding(
            capability,
            context=context,
            database_fingerprint=database_fingerprint,
            schema_name=schema_name,
            registry_digest=registry_digest,
        )
        if not isinstance(lease, SandboxSchemaLease):
            raise SandboxCapabilityError("SANDBOX_SCHEMA_LEASE_REQUIRED")
        lease_payload = {
            "sandbox_run_id": lease.sandbox_run_id,
            "tenant_id": lease.tenant_id,
            "user_id": str(lease.user_id),
            "database_fingerprint": lease.database_fingerprint,
            "schema_name": lease.schema_name,
            "registry_digest": lease.registry_digest,
            "expiry_time": _canonical_time(lease.expiry_time),
        }
        expected_binding = {
            "sandbox_run_id": capability.sandbox_run_id,
            "tenant_id": capability.tenant_id,
            "user_id": str(capability.user_id),
            "database_fingerprint": capability.database_fingerprint,
            "schema_name": capability.schema_name,
            "registry_digest": capability.registry_digest,
        }
        if any(
            lease_payload[field_name] != expected_value
            for field_name, expected_value in expected_binding.items()
        ):
            raise SandboxCapabilityError("SANDBOX_SCHEMA_LEASE_SCOPE_MISMATCH")
        if lease.expiry_time <= now:
            raise SandboxCapabilityError("SANDBOX_SCHEMA_LEASE_EXPIRED")
        if not hmac.compare_digest(
            lease.ownership_token.get_secret_value(),
            self._sign(
                {
                    "purpose": "sandbox_schema_cleanup",
                    **lease_payload,
                }
            ),
        ):
            raise SandboxCapabilityError("SANDBOX_SCHEMA_LEASE_INVALID")

    def _validate_binding(
        self,
        capability: SandboxExecutionCapability,
        *,
        context: SandboxExecutionContext,
        database_fingerprint: str,
        schema_name: str,
        registry_digest: str,
    ) -> None:
        self._validate_signature_and_identity(capability)
        if capability.tenant_id != context.tenant_id:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_TENANT_MISMATCH")
        if capability.user_id != context.user_id:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_USER_MISMATCH")
        if capability.database_fingerprint != database_fingerprint:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_DATABASE_MISMATCH")
        if capability.schema_name != schema_name:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_SCHEMA_MISMATCH")
        if capability.registry_digest != registry_digest:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_REGISTRY_MISMATCH")
        if capability.conversation_id != context.conversation_id:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_CONVERSATION_MISMATCH")
        if capability.source_message_id != context.source_message_id:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_MESSAGE_MISMATCH")
        if capability.turn_id != context.turn_id:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_TURN_MISMATCH")
        if capability.timezone != context.timezone:
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_TIMEZONE_MISMATCH")

    def _validate_signature_and_identity(
        self,
        capability: SandboxExecutionCapability,
    ) -> None:
        if not isinstance(capability, SandboxExecutionCapability):
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_OBJECT_REQUIRED")
        payload = _capability_payload(capability)
        if not hmac.compare_digest(
            capability.server_signature.get_secret_value(),
            self._sign(payload),
        ):
            raise SandboxCapabilityError("SANDBOX_CAPABILITY_SIGNATURE_INVALID")
        if (
            capability.tenant_id != self._identity_scope.tenant_id
            or capability.user_id != self._identity_scope.user_id
        ):
            raise SandboxCapabilityError("SANDBOX_IDENTITY_NOT_ALLOWLISTED")

    def _sign(self, payload: dict[str, object]) -> str:
        message = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._signing_secret, message, hashlib.sha256).hexdigest()


def _capability_payload(capability: SandboxExecutionCapability) -> dict[str, object]:
    return {
        "sandbox_run_id": capability.sandbox_run_id,
        "tenant_id": capability.tenant_id,
        "user_id": str(capability.user_id),
        "database_fingerprint": capability.database_fingerprint,
        "schema_name": capability.schema_name,
        "registry_digest": capability.registry_digest,
        "expiry_time": _canonical_time(capability.expiry_time),
        "execution_mode": capability.execution_mode,
        "conversation_id": capability.conversation_id,
        "source_message_id": capability.source_message_id,
        "turn_id": capability.turn_id,
        "timezone": capability.timezone,
        "allowed_tool_names": sorted(capability.allowed_tool_names),
        "write_gate_tool_names": sorted(capability.write_gate_tool_names),
        "messages_disabled": capability.messages_disabled,
        "production_route_disabled": capability.production_route_disabled,
    }


def _canonical_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("sandbox timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _normalized_row(value: object) -> dict[str, Any]:
    normalized = _normalized_value(value)
    if not isinstance(normalized, dict):
        raise TypeError("sandbox snapshot rows must be mappings")
    return normalized


def _normalized_value(value: object) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("sandbox snapshot datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Enum):
        return _normalized_value(value.value)
    if isinstance(value, BaseModel):
        return _normalized_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _normalized_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalized_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalized_value(item) for item in value]
        return sorted(normalized, key=_canonical_json)
    raise TypeError(f"unsupported sandbox snapshot value type: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
