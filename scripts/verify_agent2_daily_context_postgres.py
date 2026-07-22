#!/usr/bin/env python3
"""Verify the Agent2 Daily release candidate against isolated PostgreSQL data.

The executable gate is intentionally separate from every production request
entrypoint.  It requires an allow-listed run identifier and an explicit
operator confirmation before any database connection is attempted.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from dotenv import dotenv_values
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


RUN_ID_RE = re.compile(r"^release_[0-9]{8}_[a-z0-9]{8,32}$")
CONFIRMATION = "RUN_ISOLATED_AGENT2_DAILY_POSTGRES_GATE"
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "case_name",
        "case_number",
        "content",
        "database_url",
        "dingtalk_user_id",
        "dsn",
        "message",
        "password",
        "person_name",
        "raw_input",
        "secret",
        "tenant_id",
        "token",
        "user_id",
        "user_message",
    }
)
DATABASE_URL_RE = re.compile(r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://", re.IGNORECASE)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ISOLATED_TABLES = (
    "teams",
    "users",
    "daily_reports",
    "report_interaction_events",
    "agent2_daily_command_receipts",
)
REQUIRED_TRUE_CHECKS = (
    "stable_item_id_move_atomic",
    "partial_update_preserved_other_sections",
    "three_command_versions_advanced",
    "duplicate_delivery_idempotent",
    "concurrent_duplicate_serialized",
    "noop_preserved_row_and_version",
    "reply_matches_committed_snapshot",
    "transaction_rollback_atomic",
    "public_schema_unchanged",
    "cleanup_confirmed",
)
REQUIRED_ZERO_CHECKS = (
    "database_write_outside_isolated_schema",
    "external_api_calls",
    "model_calls",
    "message_sends",
)
RUNTIME_ENV_FENCES = {
    "AGENT2_CASE_FOLLOWUP_SEND_ENABLED": "false",
    "CASE_FOLLOWUP_SEND_ENABLED": "false",
    "PROGRESS_ENABLED": "false",
    "PROGRESS_OUTBOX_ENABLED": "false",
    "PROGRESS_WORKER_ENABLED": "false",
    "REMINDER_SEND_ENABLED": "false",
    "SHADOW_MEMORY_ENABLED": "false",
}


class EvidenceSafetyError(ValueError):
    """A public gate artifact would expose forbidden data."""


def validate_public_artifact(value: Any) -> Any:
    """Fail closed when a public artifact contains credentials or plaintext."""

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = str(key).strip().lower()
                if normalized in FORBIDDEN_EVIDENCE_KEYS:
                    raise EvidenceSafetyError(f"forbidden evidence key: {normalized}")
                visit(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
            return
        if isinstance(item, str) and DATABASE_URL_RE.search(item):
            raise EvidenceSafetyError("database URL is forbidden in public evidence")

    visit(value)
    return value


@dataclass(frozen=True)
class GateConfig:
    run_id: str
    schema: str

    @classmethod
    def create(cls, *, run_id: str, confirmation: str) -> "GateConfig":
        normalized = str(run_id or "").strip()
        if not RUN_ID_RE.fullmatch(normalized):
            raise ValueError("run id does not match the isolated gate allow-list")
        if confirmation != CONFIRMATION:
            raise ValueError("explicit isolated PostgreSQL gate confirmation is required")
        return cls(run_id=normalized, schema=f"agent2_daily_gate_{normalized}")


@dataclass(frozen=True)
class IsolationPlan:
    schema: str
    tables: tuple[str, ...]
    clone_statements: tuple[str, ...]
    drop_statement: str


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def build_isolation_plan(config: GateConfig) -> IsolationPlan:
    schema = _quote_ident(config.schema)
    statements = tuple(
        f"CREATE TABLE {schema}.{_quote_ident(table)} "
        f"(LIKE public.{_quote_ident(table)} INCLUDING ALL)"
        for table in ISOLATED_TABLES
    )
    return IsolationPlan(
        schema=config.schema,
        tables=ISOLATED_TABLES,
        clone_statements=statements,
        drop_statement=f"DROP SCHEMA {schema} CASCADE",
    )


def evaluate_gate_checks(checks: dict[str, Any]) -> dict[str, Any]:
    failed = [name for name in REQUIRED_TRUE_CHECKS if checks.get(name) is not True]
    failed.extend(name for name in REQUIRED_ZERO_CHECKS if checks.get(name) != 0)
    return {
        "decision": "PASS" if not failed else "NO_GO",
        "failed_checks": sorted(failed),
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_failure_symbol(exc: BaseException) -> str:
    candidate = getattr(exc, "name", None)
    if not candidate and isinstance(exc, KeyError) and len(exc.args) == 1:
        candidate = exc.args[0]
    normalized = str(candidate or "").strip()
    return normalized if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", normalized) else ""


def _async_database_url(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("postgresql+asyncpg://"):
        return normalized
    if normalized.startswith("postgresql://"):
        return "postgresql+asyncpg://" + normalized.removeprefix("postgresql://")
    if normalized.startswith("postgres://"):
        return "postgresql+asyncpg://" + normalized.removeprefix("postgres://")
    raise ValueError("configured database is not PostgreSQL")


@dataclass(frozen=True)
class SnapshotProbe:
    version: int
    today_work: tuple[str, ...]
    problems: tuple[str, ...]
    tomorrow_plan: tuple[str, ...]
    item_ids: dict[str, tuple[str, ...]]
    xmin: str

    @property
    def sha256(self) -> str:
        return _canonical_sha256(
            {
                "version": self.version,
                "today_work": list(self.today_work),
                "problems": list(self.problems),
                "tomorrow_plan": list(self.tomorrow_plan),
                "item_ids": {
                    key: list(values)
                    for key, values in sorted(self.item_ids.items())
                },
            }
        )


async def _schema_count(connection: Any, schema: str) -> int:
    return int(
        (
            await connection.execute(
                text("SELECT count(*) FROM pg_namespace WHERE nspname=:schema"),
                {"schema": schema},
            )
        ).scalar_one()
    )


async def _public_probe(connection: Any, *, actor_prefix: str, tenant_ref: str) -> dict[str, int]:
    statements = {
        "actor_rows": (
            "SELECT count(*) FROM public.users WHERE dingtalk_user_id LIKE :actor_prefix",
            {"actor_prefix": f"{actor_prefix}%"},
        ),
        "report_rows": (
            "SELECT count(*) FROM public.daily_reports r "
            "JOIN public.users u ON u.id=r.user_id "
            "WHERE u.dingtalk_user_id LIKE :actor_prefix",
            {"actor_prefix": f"{actor_prefix}%"},
        ),
        "receipt_rows": (
            "SELECT count(*) FROM public.agent2_daily_command_receipts "
            "WHERE tenant_id=:tenant_ref",
            {"tenant_ref": tenant_ref},
        ),
    }
    result: dict[str, int] = {}
    for key, (sql, params) in statements.items():
        result[key] = int((await connection.execute(text(sql), params)).scalar_one())
    return result


async def _load_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    report_id: UUID,
) -> SnapshotProbe:
    from app.agent2.daily_state import DRAFT_ITEM_IDS_KEY, REPORT_FIELD_ORDER
    from app.agent2.typed_daily_executor import TYPED_REPORT_VERSION_KEY
    from app.models import DailyReport

    async with session_factory() as session:
        report = (
            await session.execute(select(DailyReport).where(DailyReport.id == report_id))
        ).scalar_one()
        xmin = str(
            (
                await session.execute(
                    text("SELECT xmin::text FROM daily_reports WHERE id=:report_id"),
                    {"report_id": report_id},
                )
            ).scalar_one()
        )
        section_status = dict(report.section_status or {})
        raw_ids = dict(section_status.get(DRAFT_ITEM_IDS_KEY) or {})
        return SnapshotProbe(
            version=int(section_status.get(TYPED_REPORT_VERSION_KEY, 0) or 0),
            today_work=tuple(str(item) for item in (report.today_work or [])),
            problems=tuple(str(item) for item in (report.problems or [])),
            tomorrow_plan=tuple(str(item) for item in (report.tomorrow_plan or [])),
            item_ids={
                field: tuple(str(item) for item in (raw_ids.get(field) or []))
                for field in REPORT_FIELD_ORDER
            },
            xmin=xmin,
        )


async def _receipt_count(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_ref: str,
) -> int:
    from app.models import Agent2DailyCommandReceipt

    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count(Agent2DailyCommandReceipt.receipt_id)).where(
                        Agent2DailyCommandReceipt.tenant_id == tenant_ref
                    )
                )
            ).scalar_one()
        )


async def _run_isolated_scenarios(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    config: GateConfig,
    tenant_ref: str,
    checks: dict[str, Any],
    metrics: dict[str, Any],
    digests: dict[str, str],
) -> None:
    from app.agent2.daily_state import DRAFT_ITEM_IDS_KEY
    from app.agent2.typed_daily_commands import TypedDailyCommand
    from app.agent2.typed_daily_executor import (
        TYPED_REPORT_VERSION_KEY,
        TypedDailyExecutionContext,
        execute_typed_agent2_daily_commands,
    )
    from app.models import DailyReport, Team, User

    namespace = f"agent2-daily-context-postgres:{config.run_id}"
    team_id = uuid5(NAMESPACE_URL, f"{namespace}:team")
    actor_id = uuid5(NAMESPACE_URL, f"{namespace}:actor")
    report_id = uuid5(NAMESPACE_URL, f"{namespace}:report")
    report_date = date(2099, 7, 22)
    actor_ref = SimpleNamespace(
        id=actor_id,
        team_id=team_id,
        timezone="Asia/Shanghai",
        dingtalk_user_id=f"__agent2_daily_gate__{config.run_id}",
    )
    settings = SimpleNamespace(timezone="Asia/Shanghai")

    initial_work = ("gate-work-a", "gate-work-b")
    initial_problems = ("gate-problem-a",)
    initial_plan: tuple[str, ...] = ()
    initial_ids = {
        "today_work": ("gate-work-id-a", "gate-work-id-b"),
        "problems": ("gate-problem-id-a",),
        "tomorrow_plan": (),
    }

    async with session_factory() as session:
        session.add_all(
            [
                Team(
                    id=team_id,
                    code=f"__agent2_daily_gate__{config.run_id}",
                    name="Synthetic Daily Gate Team",
                    department_name="Synthetic Gate",
                    active=True,
                ),
                User(
                    id=actor_id,
                    dingtalk_user_id=actor_ref.dingtalk_user_id,
                    employee_no=f"gate-{config.run_id}",
                    name="Synthetic Daily Gate Actor",
                    team_id=team_id,
                    role="member",
                    timezone="Asia/Shanghai",
                    active=True,
                ),
                DailyReport(
                    id=report_id,
                    user_id=actor_id,
                    team_id=team_id,
                    report_date=report_date,
                    today_work=list(initial_work),
                    problems=list(initial_problems),
                    tomorrow_plan=list(initial_plan),
                    emotion="",
                    raw_input="",
                    input_fragments=[],
                    section_status={
                        TYPED_REPORT_VERSION_KEY: 1,
                        DRAFT_ITEM_IDS_KEY: {
                            key: list(values) for key, values in initial_ids.items()
                        },
                    },
                    completeness_score=0,
                    status="collecting",
                    confirmation_type="none",
                    confirmed_by_user=False,
                    last_modified_by_user=False,
                    source="agent2_daily_postgres_gate",
                    llm_payload={},
                ),
            ]
        )
        await session.commit()

    def command(
        *,
        command_type: str,
        version: int,
        patch: dict[str, Any],
        label: str,
        target_item_ids: tuple[str, ...] = (),
    ) -> TypedDailyCommand:
        return TypedDailyCommand(
            command_id=uuid5(NAMESPACE_URL, f"{namespace}:command:{label}"),
            decision_id=uuid5(NAMESPACE_URL, f"{namespace}:decision:{label}"),
            sub_decision_id=uuid5(NAMESPACE_URL, f"{namespace}:subdecision:{label}"),
            command_type=command_type,
            report_id=report_id,
            report_version=version,
            target_item_ids=target_item_ids,
            patch=patch,
            idempotency_key=f"{config.run_id}:{label}",
        )

    async def execute(
        commands: tuple[TypedDailyCommand, ...],
        *,
        label: str,
        commit: bool = True,
    ) -> Any:
        async with session_factory() as session:
            result = await execute_typed_agent2_daily_commands(
                session,
                user=actor_ref,
                commands=commands,
                execution_context=TypedDailyExecutionContext(
                    report_date=report_date,
                    source="agent2_daily_postgres_gate",
                    source_text_hash=_sha256_text(f"{config.run_id}:{label}"),
                    tenant_id=tenant_ref,
                ),
                settings=settings,
                execution_authority="authenticated_admin_command",
            )
            if commit:
                await session.commit()
            else:
                await session.flush()
                await session.rollback()
            return result

    before = await _load_snapshot(session_factory, report_id=report_id)
    digests["initial_snapshot_sha256"] = before.sha256

    move = command(
        command_type="move_items",
        version=1,
        patch={"target_field": "tomorrow_plan"},
        label="stable-move",
        target_item_ids=("gate-work-id-b",),
    )
    move_result = await execute((move,), label="stable-move")
    moved = await _load_snapshot(session_factory, report_id=report_id)
    checks["stable_item_id_move_atomic"] = (
        move_result.report_saved is True
        and moved.version == 2
        and moved.today_work == ("gate-work-a",)
        and moved.problems == initial_problems
        and moved.tomorrow_plan == ("gate-work-b",)
        and moved.item_ids["today_work"] == ("gate-work-id-a",)
        and moved.item_ids["tomorrow_plan"] == ("gate-work-id-b",)
    )

    replace_plan = command(
        command_type="replace_section",
        version=2,
        patch={"field": "tomorrow_plan", "items": ["gate-plan-b"]},
        label="replace-plan",
    )
    await execute((replace_plan,), label="replace-plan")
    partial = await _load_snapshot(session_factory, report_id=report_id)
    checks["partial_update_preserved_other_sections"] = (
        partial.version == 3
        and partial.today_work == moved.today_work
        and partial.problems == moved.problems
        and partial.tomorrow_plan == ("gate-plan-b",)
    )

    complete_commands = (
        command(
            command_type="replace_section",
            version=3,
            patch={"field": "today_work", "items": ["gate-work-c"]},
            label="complete-work",
        ),
        command(
            command_type="replace_section",
            version=4,
            patch={"field": "problems", "items": ["gate-problem-c"]},
            label="complete-problem",
        ),
        command(
            command_type="replace_section",
            version=5,
            patch={"field": "tomorrow_plan", "items": ["gate-plan-c"]},
            label="complete-plan",
        ),
    )
    complete_result = await execute(complete_commands, label="complete-document")
    complete = await _load_snapshot(session_factory, report_id=report_id)
    checks["three_command_versions_advanced"] = (
        complete.version == 6
        and [item["audit"]["before_version"] for item in complete_result.command_results]
        == [3, 4, 5]
        and [item["audit"]["after_version"] for item in complete_result.command_results]
        == [4, 5, 6]
    )
    checks["reply_matches_committed_snapshot"] = (
        tuple(complete_result.today_work) == complete.today_work
        and tuple(complete_result.problems) == complete.problems
        and tuple(complete_result.tomorrow_plan) == complete.tomorrow_plan
    )
    digests["complete_snapshot_sha256"] = complete.sha256

    receipt_before_duplicate = await _receipt_count(
        session_factory, tenant_ref=tenant_ref
    )
    duplicate_result = await execute(complete_commands, label="complete-document-replay")
    duplicate = await _load_snapshot(session_factory, report_id=report_id)
    receipt_after_duplicate = await _receipt_count(
        session_factory, tenant_ref=tenant_ref
    )
    checks["duplicate_delivery_idempotent"] = (
        duplicate.sha256 == complete.sha256
        and duplicate.xmin == complete.xmin
        and receipt_after_duplicate == receipt_before_duplicate
        and all(
            item["status"] == "duplicate" and item["actual_write"] is False
            for item in duplicate_result.command_results
        )
    )

    noop = command(
        command_type="replace_section",
        version=6,
        patch={"field": "tomorrow_plan", "items": ["gate-plan-c"]},
        label="noop-plan",
    )
    noop_result = await execute((noop,), label="noop-plan")
    after_noop = await _load_snapshot(session_factory, report_id=report_id)
    checks["noop_preserved_row_and_version"] = (
        noop_result.report_saved is False
        and after_noop.sha256 == complete.sha256
        and after_noop.xmin == complete.xmin
        and noop_result.command_results[0]["actual_write"] is False
        and tuple(noop_result.today_work) == complete.today_work
        and tuple(noop_result.problems) == complete.problems
        and tuple(noop_result.tomorrow_plan) == complete.tomorrow_plan
    )

    receipt_before_rollback = await _receipt_count(
        session_factory, tenant_ref=tenant_ref
    )
    rollback_command = command(
        command_type="append_item",
        version=6,
        patch={"field": "today_work", "items": ["gate-rollback-probe"]},
        label="rollback",
    )
    await execute((rollback_command,), label="rollback", commit=False)
    after_rollback = await _load_snapshot(session_factory, report_id=report_id)
    receipt_after_rollback = await _receipt_count(
        session_factory, tenant_ref=tenant_ref
    )
    checks["transaction_rollback_atomic"] = (
        after_rollback.sha256 == complete.sha256
        and after_rollback.xmin == complete.xmin
        and receipt_after_rollback == receipt_before_rollback
    )

    concurrent = command(
        command_type="append_item",
        version=6,
        patch={"field": "today_work", "items": ["gate-concurrent-item"]},
        label="concurrent-duplicate",
    )

    async def concurrent_execute(index: int) -> Any:
        return await execute(
            (concurrent,),
            label=f"concurrent-duplicate-{index}",
        )

    started = datetime.now(UTC)
    concurrent_results = await asyncio.wait_for(
        asyncio.gather(concurrent_execute(1), concurrent_execute(2)),
        timeout=30,
    )
    elapsed_ms = (datetime.now(UTC) - started).total_seconds() * 1000
    concurrent_snapshot = await _load_snapshot(session_factory, report_id=report_id)
    statuses = sorted(
        result.command_results[0]["status"] for result in concurrent_results
    )
    checks["concurrent_duplicate_serialized"] = (
        statuses == ["duplicate", "executed"]
        and concurrent_snapshot.version == 7
        and concurrent_snapshot.today_work.count("gate-concurrent-item") == 1
        and sum(bool(result.report_saved) for result in concurrent_results) == 1
    )
    checks["reply_matches_committed_snapshot"] = checks[
        "reply_matches_committed_snapshot"
    ] and all(
        tuple(result.today_work) == concurrent_snapshot.today_work
        and tuple(result.problems) == concurrent_snapshot.problems
        and tuple(result.tomorrow_plan) == concurrent_snapshot.tomorrow_plan
        for result in concurrent_results
    )
    metrics.update(
        {
            "concurrent_elapsed_ms": round(elapsed_ms, 3),
            "final_version": concurrent_snapshot.version,
            "receipt_count": await _receipt_count(
                session_factory, tenant_ref=tenant_ref
            ),
        }
    )
    digests["final_snapshot_sha256"] = concurrent_snapshot.sha256


async def run_postgres_gate(
    *,
    config: GateConfig,
    database_url: str,
    candidate_sha: str,
    execution_transport: str,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(candidate_sha):
        raise ValueError("candidate SHA must be a full lowercase Git SHA")
    async_url = _async_database_url(database_url)
    for key, value in RUNTIME_ENV_FENCES.items():
        os.environ[key] = value
    os.environ["DATABASE_URL"] = async_url

    actor_prefix = f"__agent2_daily_gate__{config.run_id}"
    tenant_ref = f"agent2-daily-gate-{_sha256_text(config.run_id)[:20]}"
    plan = build_isolation_plan(config)
    checks: dict[str, Any] = {
        name: False for name in REQUIRED_TRUE_CHECKS
    }
    checks.update({name: 0 for name in REQUIRED_ZERO_CHECKS})
    metrics: dict[str, Any] = {}
    digests: dict[str, str] = {}
    failure_type = ""
    failure_sha256 = ""
    failure_symbol = ""
    schema_created = False
    public_before: dict[str, int] = {}
    public_after: dict[str, int] = {}
    gate_engine = None
    admin_engine = create_async_engine(
        async_url,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": "agent2_daily_context_release_gate_admin"
            }
        },
    )
    try:
        async with admin_engine.begin() as connection:
            if await _schema_count(connection, config.schema):
                raise RuntimeError("refusing to reuse a pre-existing gate schema")
            public_before = await _public_probe(
                connection,
                actor_prefix=actor_prefix,
                tenant_ref=tenant_ref,
            )
            if any(public_before.values()):
                raise RuntimeError("refusing to reuse a gate identity found in public")
            await connection.execute(text(f"CREATE SCHEMA {_quote_ident(config.schema)}"))
            schema_created = True
            for statement in plan.clone_statements:
                await connection.execute(text(statement))

        gate_engine = create_async_engine(
            async_url,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "application_name": "agent2_daily_context_release_gate",
                    "search_path": f"{config.schema},public",
                }
            },
        )
        session_factory = async_sessionmaker(
            gate_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        async with gate_engine.connect() as connection:
            current_schema = str(
                (await connection.execute(text("SELECT current_schema()"))).scalar_one()
            )
            if current_schema != config.schema:
                raise RuntimeError("gate connection did not select the isolated schema")

        await _run_isolated_scenarios(
            session_factory=session_factory,
            config=config,
            tenant_ref=tenant_ref,
            checks=checks,
            metrics=metrics,
            digests=digests,
        )
    except Exception as exc:
        failure_type = type(exc).__name__
        failure_sha256 = _sha256_text(str(exc))
        failure_symbol = _safe_failure_symbol(exc)
    finally:
        if gate_engine is not None:
            await gate_engine.dispose()
        try:
            if schema_created:
                async with admin_engine.begin() as connection:
                    await connection.execute(text(plan.drop_statement))
            async with admin_engine.connect() as connection:
                checks["cleanup_confirmed"] = (
                    await _schema_count(connection, config.schema) == 0
                )
                public_after = await _public_probe(
                    connection,
                    actor_prefix=actor_prefix,
                    tenant_ref=tenant_ref,
                )
                checks["public_schema_unchanged"] = (
                    public_after == public_before and not any(public_after.values())
                )
                checks["database_write_outside_isolated_schema"] = sum(
                    public_after.values()
                )
        except Exception as cleanup_exc:
            checks["cleanup_confirmed"] = False
            if not failure_type:
                failure_type = type(cleanup_exc).__name__
                failure_sha256 = _sha256_text(str(cleanup_exc))
                failure_symbol = _safe_failure_symbol(cleanup_exc)
        await admin_engine.dispose()
        try:
            from app.db import engine as default_app_engine

            await default_app_engine.dispose()
        except Exception:
            pass

    decision = evaluate_gate_checks(checks)
    artifact: dict[str, Any] = {
        "artifact_version": "agent2.daily_context.postgres_release_gate.v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "candidate_sha": candidate_sha,
        "execution_transport": execution_transport,
        "run_id": config.run_id,
        "temporary_schema_sha256": _sha256_text(config.schema),
        "database_target": "configured_postgresql_redacted",
        "decision": decision["decision"],
        "failed_checks": decision["failed_checks"],
        "checks": checks,
        "metrics": metrics,
        "snapshot_digests": digests,
        "failure_type": failure_type,
        "failure_sha256": failure_sha256,
        "failure_symbol": failure_symbol,
        "public_probe_before": public_before,
        "public_probe_after": public_after,
    }
    validate_public_artifact(artifact)
    artifact["report_sha256"] = _canonical_sha256(artifact)
    return artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated PostgreSQL gate for the Agent2 Daily release candidate."
    )
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument(
        "--execution-transport",
        choices=("local", "ssh", "unspecified"),
        default="unspecified",
    )
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--output", required=True)
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    config = GateConfig.create(
        run_id=args.run_id,
        confirmation=args.confirmation,
    )
    values = dotenv_values(args.env_file)
    database_url = str(values.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or "")
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")
    artifact = await run_postgres_gate(
        config=config,
        database_url=database_url,
        candidate_sha=str(args.candidate_sha).strip().lower(),
        execution_transport=args.execution_transport,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "decision": artifact["decision"],
                "failed_checks": artifact["failed_checks"],
                "report_sha256": artifact["report_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if artifact["decision"] == "PASS" else 1


def main() -> int:
    return asyncio.run(_main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
