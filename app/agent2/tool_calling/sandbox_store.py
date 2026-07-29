from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.agent2.tool_calling.registry as tool_registry
from app.agent2.tool_calling.sandbox_contracts import (
    SandboxCapabilityAuthority,
    SandboxCapabilityError,
    SandboxDatabaseConfig,
    SandboxExecutionCapability,
    SandboxExecutionContext,
    SandboxExecutorEvidence,
    SandboxReceiptFacts,
    SANDBOX_SNAPSHOT_TABLES,
    SANDBOX_STATE_TABLES,
    SandboxSchemaLease,
    SandboxSnapshot,
    SandboxTransactionOutcome,
    validate_sandbox_receipt_evidence,
)


class SandboxIsolationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        transaction_opened: bool = False,
        rolled_back: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.transaction_opened = transaction_opened
        self.rolled_back = rolled_back


class SandboxBackend(Protocol):
    @property
    def database_fingerprint(self) -> str: ...

    async def attest_database_identity(
        self,
        *,
        expected_database_name: str,
        expected_role_name: str,
    ) -> str: ...

    async def create_schema(self, schema_name: str) -> None: ...

    async def drop_schema(self, schema_name: str) -> None: ...

    async def schema_exists(self, schema_name: str) -> bool: ...

    async def snapshot_rows(self, schema_name: str) -> Any: ...

    async def begin_transaction(
        self,
        schema_name: str,
    ) -> "SandboxBackendTransaction": ...

    async def close(self) -> None: ...


class SandboxTransactionWork(Protocol):
    async def snapshot_rows(self) -> Any: ...

    async def stage_rollback_probe(self, probe_id: str) -> None: ...

    async def acquire_execution_lock(self, lock_id: str) -> None: ...

    async def read_table_rows(
        self,
        table_name: str,
    ) -> tuple[dict[str, object], ...]: ...

    async def read_record(
        self,
        table_name: str,
        record_id: str,
    ) -> dict[str, object] | None: ...

    async def upsert_record(
        self,
        table_name: str,
        record_id: str,
        payload: dict[str, object],
        *,
        create_only: bool = False,
    ) -> None: ...

    async def delete_record(self, table_name: str, record_id: str) -> bool: ...


class SandboxBackendTransaction(SandboxTransactionWork, Protocol):
    async def persist_receipt_row(self, row: dict[str, object]) -> None: ...

    async def read_receipt_row(self, receipt_id: str) -> dict[str, object] | None: ...

    async def commit(self) -> str | None: ...

    async def rollback(self) -> str | None: ...


@dataclass(frozen=True)
class SandboxBatchTransactionOutcome:
    atomic_group_id: str
    before: SandboxSnapshot
    attempted_after: SandboxSnapshot
    after: SandboxSnapshot
    receipts: tuple[SandboxReceiptFacts, ...]
    committed: bool
    rolled_back: bool
    error_code: str | None = None


class SandboxExecutionSession:
    """Restricted state session; receipt persistence stays behind the evidence gate."""

    def __init__(
        self,
        transaction: SandboxBackendTransaction,
        *,
        schema_name: str,
    ) -> None:
        self._transaction = transaction
        self._schema_name = schema_name
        self._receipts: list[SandboxReceiptFacts] = []

    @property
    def receipts(self) -> tuple[SandboxReceiptFacts, ...]:
        return tuple(self._receipts)

    async def snapshot(self) -> SandboxSnapshot:
        return SandboxSnapshot.capture(
            schema_name=self._schema_name,
            rows=await self._transaction.snapshot_rows(),
        )

    async def read_table_rows(
        self,
        table_name: str,
    ) -> tuple[dict[str, object], ...]:
        _assert_state_table(table_name)
        return await self._transaction.read_table_rows(table_name)

    async def read_record(
        self,
        table_name: str,
        record_id: str,
    ) -> dict[str, object] | None:
        _assert_state_table(table_name)
        return await self._transaction.read_record(table_name, record_id)

    async def upsert_record(
        self,
        table_name: str,
        record_id: str,
        payload: dict[str, object],
        *,
        create_only: bool = False,
    ) -> None:
        _assert_state_table(table_name)
        if payload.get(_record_id_field(table_name)) != record_id:
            raise SandboxIsolationError("SANDBOX_RECORD_ID_MISMATCH")
        await self._transaction.upsert_record(
            table_name,
            record_id,
            payload,
            create_only=create_only,
        )

    async def delete_record(self, table_name: str, record_id: str) -> bool:
        _assert_state_table(table_name)
        return await self._transaction.delete_record(table_name, record_id)

    async def find_receipt_by_call_identity(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        source_message_id: str,
        tool_call_id: str,
    ) -> SandboxReceiptFacts | None:
        rows = await self._transaction.snapshot_rows()
        for row in rows["receipt"]:
            if (
                row.get("tenant_id") == tenant_id
                and row.get("user_id") == user_id
                and row.get("conversation_id") == conversation_id
                and row.get("source_message_id") == source_message_id
                and row.get("tool_call_id") == tool_call_id
            ):
                return SandboxReceiptFacts.model_validate(row)
        return None

    async def find_receipt_by_operation_fingerprint(
        self,
        operation_fingerprint: str,
    ) -> SandboxReceiptFacts | None:
        rows = await self._transaction.snapshot_rows()
        for row in rows["receipt"]:
            if row.get("operation_fingerprint") == operation_fingerprint:
                return SandboxReceiptFacts.model_validate(row)
        return None

    async def seal_receipt(
        self,
        *,
        executor_before: SandboxSnapshot,
        executor_after: SandboxSnapshot,
        receipt: SandboxReceiptFacts,
    ) -> None:
        if (
            executor_before.table_rows("receipt")
            != executor_after.table_rows("receipt")
        ):
            raise SandboxIsolationError("RECEIPT_WRITE_OUTSIDE_EVIDENCE_GATE")
        persisted = await self._transaction.read_receipt_row(receipt.receipt_id)
        if persisted is None:
            await self._transaction.persist_receipt_row(receipt.canonical_row())
        database_after_flush = await self.snapshot()
        persisted = await self._transaction.read_receipt_row(receipt.receipt_id)
        validation = validate_sandbox_receipt_evidence(
            SandboxExecutorEvidence(
                before=executor_before,
                after=executor_after,
                receipt=receipt,
            ),
            database_after_flush=database_after_flush,
            persisted_receipt_row=persisted or {},
        )
        if not validation.accepted:
            raise SandboxIsolationError(
                validation.error_code or "RECEIPT_EVIDENCE_MISMATCH"
            )
        self._receipts.append(receipt)


class SandboxStore:
    def __init__(
        self,
        config: SandboxDatabaseConfig,
        *,
        capability_authority: SandboxCapabilityAuthority,
        execution_context: SandboxExecutionContext,
        now_provider: Callable[[], datetime],
        backend: SandboxBackend | None = None,
    ) -> None:
        sandbox_database_url = config.sandbox_database_url.get_secret_value()
        sandbox_url = _postgres_url(
            sandbox_database_url,
            role="sandbox",
        )
        production_url = _postgres_url(
            config.production_database_url.get_secret_value(),
            role="production",
        )
        if _database_resource(sandbox_url) == _database_resource(production_url):
            raise SandboxIsolationError("SANDBOX_DATABASE_EQUALS_PRODUCTION")
        database_name = sandbox_url.database or ""
        username = sandbox_url.username or ""
        if "sandbox" not in database_name.casefold():
            raise SandboxIsolationError("SANDBOX_DATABASE_NAME_NOT_MARKED")
        if "sandbox" not in username.casefold():
            raise SandboxIsolationError("SANDBOX_DATABASE_ROLE_NOT_MARKED")
        self._database_name = database_name
        self._database_role = username
        self._database_fingerprint = _fingerprint(sandbox_url)
        self._uses_real_postgresql = backend is None
        self._observed_database_fingerprint: str | None = None
        self._backend = backend or PostgresSandboxBackend(sandbox_database_url)
        if self._backend.database_fingerprint != self._database_fingerprint:
            raise SandboxIsolationError("SANDBOX_BACKEND_FINGERPRINT_MISMATCH")
        self._capability_authority = capability_authority
        self._execution_context = execution_context
        self._now_provider = now_provider

    @property
    def database_fingerprint(self) -> str:
        return self._database_fingerprint

    @property
    def database_name(self) -> str:
        return self._database_name

    @property
    def database_attestation_verified(self) -> bool:
        return (
            self._uses_real_postgresql
            and self._observed_database_fingerprint is not None
        )

    @property
    def observed_database_fingerprint(self) -> str | None:
        return self._observed_database_fingerprint

    async def initialize(
        self,
        capability: SandboxExecutionCapability,
    ) -> SandboxSchemaLease:
        self._assert_scope(capability)
        if self._uses_real_postgresql:
            try:
                observed_fingerprint = (
                    await self._backend.attest_database_identity(
                        expected_database_name=self._database_name,
                        expected_role_name=self._database_role,
                    )
                )
            except SandboxIsolationError:
                raise
            except Exception as exc:
                raise SandboxIsolationError(
                    "SANDBOX_DATABASE_ATTESTATION_FAILED"
                ) from exc
            if not isinstance(observed_fingerprint, str) or len(
                observed_fingerprint
            ) != 64:
                raise SandboxIsolationError(
                    "SANDBOX_DATABASE_ATTESTATION_INVALID"
                )
            self._observed_database_fingerprint = observed_fingerprint
        if await self._backend.schema_exists(capability.schema_name):
            raise SandboxIsolationError("SANDBOX_SCHEMA_ALREADY_EXISTS")
        lease = self._capability_authority.issue_schema_lease(
            capability,
            expiry_time=self._now_provider() + timedelta(hours=24),
        )
        await self._backend.create_schema(capability.schema_name)
        return lease

    async def snapshot(self, capability: SandboxExecutionCapability) -> SandboxSnapshot:
        self._assert_scope(capability)
        if not await self._backend.schema_exists(capability.schema_name):
            raise SandboxIsolationError("SANDBOX_SCHEMA_NOT_INITIALIZED")
        rows = await self._backend.snapshot_rows(capability.schema_name)
        return SandboxSnapshot.capture(schema_name=capability.schema_name, rows=rows)

    async def cleanup(
        self,
        capability: SandboxExecutionCapability,
        lease: SandboxSchemaLease,
    ) -> None:
        self._assert_cleanup_scope(capability, lease)
        await self._backend.drop_schema(capability.schema_name)
        if await self._backend.schema_exists(capability.schema_name):
            raise SandboxIsolationError("SANDBOX_SCHEMA_CLEANUP_INCOMPLETE")

    async def schema_exists(self, capability: SandboxExecutionCapability) -> bool:
        self._assert_scope(capability)
        return await self._backend.schema_exists(capability.schema_name)

    async def close(self) -> None:
        await self._backend.close()

    def transaction_manager(
        self,
        capability: SandboxExecutionCapability,
    ) -> "SandboxTransactionManager":
        self._assert_scope(capability)
        return SandboxTransactionManager(self, capability)

    def _assert_scope(self, capability: SandboxExecutionCapability) -> None:
        if not isinstance(capability, SandboxExecutionCapability):
            raise SandboxIsolationError("SANDBOX_CAPABILITY_OBJECT_REQUIRED")
        try:
            self._capability_authority.validate(
                capability,
                context=self._context_for_capability(capability),
                database_fingerprint=self.database_fingerprint,
                schema_name=capability.schema_name,
                registry_digest=tool_registry.registry_contract_digest(),
                now=self._now_provider(),
            )
        except SandboxCapabilityError as exc:
            raise SandboxIsolationError(exc.code) from exc
        if not _valid_schema_name(capability.schema_name):
            raise SandboxIsolationError("SANDBOX_SCHEMA_NAME_INVALID")

    def _assert_cleanup_scope(
        self,
        capability: SandboxExecutionCapability,
        lease: SandboxSchemaLease,
    ) -> None:
        try:
            self._capability_authority.validate_schema_cleanup(
                capability,
                lease,
                context=self._context_for_capability(capability),
                database_fingerprint=self.database_fingerprint,
                schema_name=capability.schema_name,
                registry_digest=capability.registry_digest,
                now=self._now_provider(),
            )
        except SandboxCapabilityError as exc:
            raise SandboxIsolationError(exc.code) from exc

    def _context_for_capability(
        self,
        capability: SandboxExecutionCapability,
    ) -> SandboxExecutionContext:
        return self._execution_context.model_copy(
            update={
                "now": self._now_provider(),
                "conversation_id": capability.conversation_id,
                "source_message_id": capability.source_message_id,
                "turn_id": capability.turn_id,
                "timezone": capability.timezone,
            }
        )


class SandboxTransactionManager:
    def __init__(
        self,
        store: SandboxStore,
        capability: SandboxExecutionCapability,
    ) -> None:
        self._store = store
        self._capability = capability

    async def execute(
        self,
        *,
        atomic_group_id: str,
        work: Callable[
            [SandboxTransactionWork],
            Awaitable[SandboxReceiptFacts | None],
        ],
        commit: bool,
    ) -> SandboxTransactionOutcome:
        if not atomic_group_id:
            raise ValueError("sandbox atomic group ID is required")
        capability = self._capability
        if not await self._store.schema_exists(capability):
            raise SandboxIsolationError("SANDBOX_SCHEMA_NOT_INITIALIZED")
        try:
            transaction = await self._store._backend.begin_transaction(
                capability.schema_name
            )
        except Exception as exc:
            raise SandboxIsolationError("SANDBOX_TRANSACTION_OPEN_FAILED") from exc
        try:
            before = SandboxSnapshot.capture(
                schema_name=capability.schema_name,
                rows=await transaction.snapshot_rows(),
            )
        except Exception as exc:
            try:
                connection_cleanup_error = await transaction.rollback()
            except Exception as rollback_exc:
                raise SandboxIsolationError(
                    "SANDBOX_TRANSACTION_ROLLBACK_FAILED"
                ) from rollback_exc
            raise SandboxIsolationError(
                connection_cleanup_error
                or "SANDBOX_TRANSACTION_INITIAL_SNAPSHOT_FAILED",
                transaction_opened=True,
                rolled_back=True,
            ) from exc

        try:
            receipt = await work(transaction)
            executor_after = SandboxSnapshot.capture(
                schema_name=capability.schema_name,
                rows=await transaction.snapshot_rows(),
            )
        except Exception:
            return await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=before,
                error_code="ATOMIC_GROUP_FAILED",
            )

        if not commit:
            return await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=executor_after,
                error_code=None,
            )

        if executor_after.table_rows("receipt") != before.table_rows("receipt"):
            return await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=executor_after,
                error_code="RECEIPT_WRITE_OUTSIDE_EVIDENCE_GATE",
            )
        if not isinstance(receipt, SandboxReceiptFacts):
            return await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=executor_after,
                error_code="RECEIPT_EVIDENCE_REQUIRED",
            )

        try:
            await transaction.persist_receipt_row(receipt.canonical_row())
            database_after_flush = SandboxSnapshot.capture(
                schema_name=capability.schema_name,
                rows=await transaction.snapshot_rows(),
            )
            persisted_receipt_row = await transaction.read_receipt_row(
                receipt.receipt_id
            )
            validation = validate_sandbox_receipt_evidence(
                SandboxExecutorEvidence(
                    before=before,
                    after=executor_after,
                    receipt=receipt,
                ),
                database_after_flush=database_after_flush,
                persisted_receipt_row=persisted_receipt_row or {},
            )
        except Exception:
            return await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=executor_after,
                error_code="RECEIPT_EVIDENCE_MISMATCH",
            )
        if not validation.accepted:
            return await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=database_after_flush,
                error_code=validation.error_code,
            )

        try:
            connection_cleanup_error = await transaction.commit()
        except Exception as exc:
            try:
                connection_cleanup_error = await transaction.rollback()
            except Exception:
                raise SandboxIsolationError(
                    "SANDBOX_TRANSACTION_COMMIT_AND_ROLLBACK_FAILED",
                    transaction_opened=True,
                ) from exc
            error_code = (
                connection_cleanup_error
                or "SANDBOX_TRANSACTION_COMMIT_FAILED"
            )
            raise SandboxIsolationError(
                error_code,
                transaction_opened=True,
                rolled_back=True,
            ) from exc
        return SandboxTransactionOutcome(
            atomic_group_id=atomic_group_id,
            before=before,
            attempted_after=database_after_flush,
            after=database_after_flush,
            committed=True,
            rolled_back=False,
            error_code=connection_cleanup_error,
        )

    async def execute_registry_batch(
        self,
        *,
        atomic_group_id: str,
        work: Callable[[SandboxExecutionSession], Awaitable[None]],
    ) -> SandboxBatchTransactionOutcome:
        if not atomic_group_id:
            raise ValueError("sandbox atomic group ID is required")
        capability = self._capability
        if not await self._store.schema_exists(capability):
            raise SandboxIsolationError("SANDBOX_SCHEMA_NOT_INITIALIZED")
        try:
            transaction = await self._store._backend.begin_transaction(
                capability.schema_name
            )
        except Exception as exc:
            raise SandboxIsolationError("SANDBOX_TRANSACTION_OPEN_FAILED") from exc
        try:
            await transaction.acquire_execution_lock(capability.schema_name)
            before = SandboxSnapshot.capture(
                schema_name=capability.schema_name,
                rows=await transaction.snapshot_rows(),
            )
        except Exception as exc:
            try:
                cleanup_error = await transaction.rollback()
            except Exception as rollback_exc:
                raise SandboxIsolationError(
                    "SANDBOX_TRANSACTION_ROLLBACK_FAILED"
                ) from rollback_exc
            raise SandboxIsolationError(
                cleanup_error or "SANDBOX_TRANSACTION_INITIAL_SNAPSHOT_FAILED",
                transaction_opened=True,
                rolled_back=True,
            ) from exc

        session = SandboxExecutionSession(
            transaction,
            schema_name=capability.schema_name,
        )
        try:
            await work(session)
            attempted_after = await session.snapshot()
            if not session.receipts:
                raise SandboxIsolationError("RECEIPT_EVIDENCE_REQUIRED")
        except Exception as exc:
            try:
                attempted_after = await session.snapshot()
            except Exception:
                attempted_after = before
            error_code = getattr(exc, "code", None) or "ATOMIC_GROUP_FAILED"
            rollback = await self._rollback_outcome(
                transaction=transaction,
                atomic_group_id=atomic_group_id,
                before=before,
                attempted_after=attempted_after,
                error_code=error_code,
            )
            return SandboxBatchTransactionOutcome(
                atomic_group_id=atomic_group_id,
                before=rollback.before,
                attempted_after=rollback.attempted_after,
                after=rollback.after,
                receipts=(),
                committed=False,
                rolled_back=True,
                error_code=rollback.error_code,
            )

        try:
            cleanup_error = await transaction.commit()
        except Exception as exc:
            try:
                rollback_cleanup_error = await transaction.rollback()
            except Exception:
                raise SandboxIsolationError(
                    "SANDBOX_TRANSACTION_COMMIT_AND_ROLLBACK_FAILED",
                    transaction_opened=True,
                ) from exc
            raise SandboxIsolationError(
                rollback_cleanup_error or "SANDBOX_TRANSACTION_COMMIT_FAILED",
                transaction_opened=True,
                rolled_back=True,
            ) from exc
        return SandboxBatchTransactionOutcome(
            atomic_group_id=atomic_group_id,
            before=before,
            attempted_after=attempted_after,
            after=attempted_after,
            receipts=session.receipts,
            committed=True,
            rolled_back=False,
            error_code=cleanup_error,
        )

    async def _rollback_outcome(
        self,
        *,
        transaction: SandboxBackendTransaction,
        atomic_group_id: str,
        before: SandboxSnapshot,
        attempted_after: SandboxSnapshot,
        error_code: str | None,
    ) -> SandboxTransactionOutcome:
        try:
            connection_cleanup_error = await transaction.rollback()
        except Exception as exc:
            raise SandboxIsolationError(
                "SANDBOX_TRANSACTION_ROLLBACK_FAILED"
            ) from exc
        try:
            after = await self._store.snapshot(self._capability)
        except Exception as exc:
            raise SandboxIsolationError(
                "SANDBOX_TRANSACTION_ROLLBACK_EVIDENCE_UNAVAILABLE",
                transaction_opened=True,
                rolled_back=True,
            ) from exc
        if not _rollback_changes_reverted(before, attempted_after, after):
            raise SandboxIsolationError(
                "TRANSACTION_ROLLBACK_EVIDENCE_MISMATCH",
                transaction_opened=True,
                rolled_back=True,
            )
        return SandboxTransactionOutcome(
            atomic_group_id=atomic_group_id,
            before=before,
            attempted_after=attempted_after,
            after=after,
            committed=False,
            rolled_back=True,
            error_code=connection_cleanup_error or error_code,
        )


class PostgresSandboxBackend:
    """Dedicated lazy PostgreSQL adapter; it never imports the production DB module."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._database_fingerprint = _fingerprint(
            _postgres_url(database_url, role="sandbox")
        )
        self._engine: AsyncEngine | None = None

    @property
    def database_fingerprint(self) -> str:
        return self._database_fingerprint

    async def attest_database_identity(
        self,
        *,
        expected_database_name: str,
        expected_role_name: str,
    ) -> str:
        async with self._engine_for_use().connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT current_database(), current_user, "
                    "COALESCE(inet_server_addr()::text, 'local'), "
                    "COALESCE(inet_server_port(), 0)"
                )
            )
            database_name, role_name, server_address, server_port = result.one()
        if database_name != expected_database_name:
            raise SandboxIsolationError(
                "SANDBOX_CONNECTED_DATABASE_NAME_MISMATCH"
            )
        if role_name != expected_role_name:
            raise SandboxIsolationError(
                "SANDBOX_CONNECTED_DATABASE_ROLE_MISMATCH"
            )
        if "sandbox" not in str(database_name).casefold():
            raise SandboxIsolationError(
                "SANDBOX_CONNECTED_DATABASE_NOT_MARKED"
            )
        if "sandbox" not in str(role_name).casefold():
            raise SandboxIsolationError("SANDBOX_CONNECTED_ROLE_NOT_MARKED")
        identity = "|".join(
            (
                "postgresql",
                str(server_address).casefold(),
                str(server_port),
                str(database_name).casefold(),
                str(role_name).casefold(),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def create_schema(self, schema_name: str) -> None:
        schema = _quoted_schema(schema_name)
        async with self._engine_for_use().begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            for statement in _foundation_table_ddl(schema):
                await connection.execute(text(statement))

    async def drop_schema(self, schema_name: str) -> None:
        schema = _quoted_schema(schema_name)
        async with self._engine_for_use().begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))

    async def schema_exists(self, schema_name: str) -> bool:
        _quoted_schema(schema_name)
        async with self._engine_for_use().connect() as connection:
            result = await connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_namespace WHERE nspname = :schema_name"
                    ")"
                ),
                {"schema_name": schema_name},
            )
            return bool(result)

    async def snapshot_rows(
        self,
        schema_name: str,
    ) -> dict[str, tuple[dict[str, object], ...]]:
        async with self._engine_for_use().connect() as connection:
            return await _postgres_snapshot_rows(connection, schema_name)

    async def begin_transaction(
        self,
        schema_name: str,
    ) -> "PostgresSandboxTransaction":
        _quoted_schema(schema_name)
        connection = await self._engine_for_use().connect()
        try:
            transaction = await connection.begin()
        except Exception:
            await connection.close()
            raise
        return PostgresSandboxTransaction(connection, transaction, schema_name)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def _engine_for_use(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._database_url,
                poolclass=NullPool,
                pool_pre_ping=True,
            )
        return self._engine


class PostgresSandboxTransaction:
    def __init__(
        self,
        connection: AsyncConnection,
        transaction: Any,
        schema_name: str,
    ) -> None:
        self._connection = connection
        self._transaction = transaction
        self._schema_name = schema_name
        self._closed = False

    async def snapshot_rows(self) -> dict[str, tuple[dict[str, object], ...]]:
        self._ensure_open()
        return await _postgres_snapshot_rows(self._connection, self._schema_name)

    async def acquire_execution_lock(self, lock_id: str) -> None:
        self._ensure_open()
        await self._connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_id))"),
            {"lock_id": lock_id},
        )

    async def read_table_rows(
        self,
        table_name: str,
    ) -> tuple[dict[str, object], ...]:
        self._ensure_open()
        _assert_state_table(table_name)
        schema = _quoted_schema(self._schema_name)
        result = await self._connection.execute(
            text(
                f'SELECT payload FROM {schema}."{table_name}" '
                "ORDER BY record_id"
            )
        )
        return tuple(dict(row) for row in result.scalars())

    async def read_record(
        self,
        table_name: str,
        record_id: str,
    ) -> dict[str, object] | None:
        self._ensure_open()
        _assert_state_table(table_name)
        schema = _quoted_schema(self._schema_name)
        result = await self._connection.scalar(
            text(
                f'SELECT payload FROM {schema}."{table_name}" '
                "WHERE record_id = :record_id"
            ),
            {"record_id": record_id},
        )
        return dict(result) if result is not None else None

    async def upsert_record(
        self,
        table_name: str,
        record_id: str,
        payload: dict[str, object],
        *,
        create_only: bool = False,
    ) -> None:
        self._ensure_open()
        _assert_state_table(table_name)
        schema = _quoted_schema(self._schema_name)
        conflict = (
            ""
            if create_only
            else " ON CONFLICT (record_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        await self._connection.execute(
            text(
                f'INSERT INTO {schema}."{table_name}" (record_id, payload) '
                f"VALUES (:record_id, CAST(:payload AS JSONB)){conflict}"
            ),
            {"record_id": record_id, "payload": _json_payload(payload)},
        )

    async def delete_record(self, table_name: str, record_id: str) -> bool:
        self._ensure_open()
        _assert_state_table(table_name)
        schema = _quoted_schema(self._schema_name)
        result = await self._connection.execute(
            text(
                f'DELETE FROM {schema}."{table_name}" '
                "WHERE record_id = :record_id"
            ),
            {"record_id": record_id},
        )
        return bool(result.rowcount)

    async def persist_receipt_row(self, row: dict[str, object]) -> None:
        self._ensure_open()
        receipt_id = row.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("sandbox receipt row requires a receipt ID")
        schema = _quoted_schema(self._schema_name)
        await self._connection.execute(
            text(
                f'INSERT INTO {schema}."receipt" (record_id, payload) '
                "VALUES (:record_id, CAST(:payload AS JSONB))"
            ),
            {
                "record_id": receipt_id,
                "payload": _json_payload(row),
            },
        )

    async def read_receipt_row(self, receipt_id: str) -> dict[str, object] | None:
        self._ensure_open()
        schema = _quoted_schema(self._schema_name)
        result = await self._connection.scalar(
            text(
                f'SELECT payload FROM {schema}."receipt" '
                "WHERE record_id = :record_id"
            ),
            {"record_id": receipt_id},
        )
        return dict(result) if result is not None else None

    async def stage_rollback_probe(self, probe_id: str) -> None:
        if not probe_id:
            raise ValueError("sandbox rollback probe ID is required")
        await self.persist_receipt_row(
            {
                "receipt_id": f"foundation-rollback-probe:{probe_id}",
                "probe_kind": "sandbox_foundation_rollback",
            }
        )

    async def commit(self) -> str | None:
        self._ensure_open()
        try:
            await self._transaction.commit()
        except Exception:
            raise
        self._closed = True
        try:
            await self._connection.close()
        except Exception:
            return "SANDBOX_CONNECTION_CLOSE_FAILED_AFTER_COMMIT"
        return None

    async def rollback(self) -> str | None:
        if self._closed:
            return None
        try:
            await self._transaction.rollback()
        except Exception:
            try:
                await self._connection.close()
            finally:
                self._closed = True
            raise
        self._closed = True
        try:
            await self._connection.close()
        except Exception:
            return "SANDBOX_CONNECTION_CLOSE_FAILED_AFTER_ROLLBACK"
        return None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("sandbox transaction is closed")


def _postgres_url(raw_url: str, *, role: str) -> URL:
    try:
        parsed = make_url(raw_url)
    except Exception as exc:
        raise SandboxIsolationError(
            f"{role.upper()}_DATABASE_URL_INVALID"
        ) from exc
    if parsed.get_backend_name() != "postgresql":
        raise SandboxIsolationError(f"{role.upper()}_DATABASE_MUST_BE_POSTGRESQL")
    if parsed.get_driver_name() != "asyncpg":
        raise SandboxIsolationError(f"{role.upper()}_DATABASE_DRIVER_MUST_BE_ASYNCPG")
    if not parsed.host or not parsed.database or not parsed.username:
        raise SandboxIsolationError(f"{role.upper()}_DATABASE_IDENTITY_INCOMPLETE")
    return parsed


def _database_resource(url: URL) -> tuple[str, str, int, str]:
    return (
        url.get_backend_name(),
        (url.host or "").casefold(),
        url.port or 5432,
        (url.database or "").casefold(),
    )


def _fingerprint(url: URL) -> str:
    payload = "|".join((*map(str, _database_resource(url)), (url.username or "").casefold()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_schema_name(value: str) -> bool:
    prefix = "agent2_tool_sandbox_"
    if not value.startswith(prefix) or len(value) > 63:
        return False
    suffix = value[len(prefix) :]
    return bool(suffix) and all(
        character.isascii()
        and (character.islower() or character.isdigit() or character == "_")
        for character in suffix
    )


def _quoted_schema(schema_name: str) -> str:
    if not _valid_schema_name(schema_name):
        raise SandboxIsolationError("SANDBOX_SCHEMA_NAME_INVALID")
    return f'"{schema_name}"'


def _foundation_table_ddl(schema: str) -> tuple[str, ...]:
    return tuple(
        f"CREATE TABLE {schema}.\"{table_name}\" ("
        "record_id TEXT PRIMARY KEY, "
        "payload JSONB NOT NULL"
        ")"
        for table_name in SANDBOX_SNAPSHOT_TABLES
    )


def _assert_state_table(table_name: str) -> None:
    if table_name not in SANDBOX_STATE_TABLES:
        raise SandboxIsolationError("SANDBOX_STATE_TABLE_NOT_ALLOWED")


def _record_id_field(table_name: str) -> str:
    return {
        "daily_reports": "report_id",
        "daily_items": "item_id",
        "sandbox_pending": "pending_id",
        "personal_memory": "memory_id",
        "personal_memory_audit": "audit_id",
    }[table_name]


def _rollback_changes_reverted(
    before: SandboxSnapshot,
    attempted_after: SandboxSnapshot,
    observed_after: SandboxSnapshot,
) -> bool:
    id_fields = {
        "daily_reports": "report_id",
        "daily_items": "item_id",
        "sandbox_pending": "pending_id",
        "personal_memory": "memory_id",
        "personal_memory_audit": "audit_id",
        "receipt": "receipt_id",
    }
    for table_name, id_field in id_fields.items():
        before_rows = {
            str(row[id_field]): row for row in before.table_rows(table_name)
        }
        attempted_rows = {
            str(row[id_field]): row
            for row in attempted_after.table_rows(table_name)
        }
        observed_rows = {
            str(row[id_field]): row
            for row in observed_after.table_rows(table_name)
        }
        for record_id in before_rows.keys() | attempted_rows.keys():
            before_row = before_rows.get(record_id)
            attempted_row = attempted_rows.get(record_id)
            if attempted_row == before_row:
                continue
            if observed_rows.get(record_id) == attempted_row:
                return False
    return True


async def _postgres_snapshot_rows(
    connection: AsyncConnection,
    schema_name: str,
) -> dict[str, tuple[dict[str, object], ...]]:
    schema = _quoted_schema(schema_name)
    snapshot: dict[str, tuple[dict[str, object], ...]] = {}
    for table_name in SANDBOX_SNAPSHOT_TABLES:
        result = await connection.execute(
            text(
                f'SELECT payload FROM {schema}."{table_name}" '
                "ORDER BY record_id"
            )
        )
        snapshot[table_name] = tuple(dict(row) for row in result.scalars())
    return snapshot


def _json_payload(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
