from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(LOCAL_TZ).date()
TARGET_WEEK_START = TODAY + timedelta(days=(7 - TODAY.weekday()) % 7)
TENANT_ID = "agent2-isolated-api-smoke"
SCHEMA_PREFIX = "agent2_api_smoke_"
CASE_NAMES = (
    "short_full",
    "structured_full",
    "long_voice_text",
    "copy_yesterday",
    "bare_confirm",
    "risk_followup",
    "daily_weekly_followup",
    "no_change_plus_plan",
    "whole_report_replacement",
    "correction_followup",
    "same_turn_submit",
    "standalone_submit",
    "weekly_crud",
    "idempotency",
    "daily_crud",
    "asr_name_correction",
)
ADVERSE_CASE_COVERAGE: dict[str, dict[str, object]] = {
    "d953c1934cca0c681f325c6fc97849b6e16e920ab9a7a122a85a015548e399a0": {
        "cases": ("whole_report_replacement",), "mode": "exact"
    },
    "5c7a525460993f6c44972a1bd1495220f15cb30c407f6181d6db4d086b29f29b": {
        "cases": ("long_voice_text",), "mode": "exact"
    },
    "2f854694c2846deda8452a1c01b3ef80e11f8f1ffe5830898b183c6a8ef48cdf": {
        "cases": ("whole_report_replacement", "correction_followup"),
        "mode": "contextual_analogue",
    },
    "54595ef90ad53d5ff4427282bf3a250a11c6d0be65dc7756310a80e70b94d491": {
        "cases": ("structured_full",), "mode": "exact"
    },
    "6b0f87e35d17a7230afed182bea7e82bde0040158d7e9d5e1b768426ecb722c1": {
        "cases": ("short_full", "whole_report_replacement"), "mode": "exact"
    },
    "0f03eb7a0c8e4a2f3ac6ca15539876aff1e3ddee6341df64b98ebd72ce8ce99d": {
        "cases": ("no_change_plus_plan",), "mode": "exact"
    },
    "e6a65f0d6d450bdf9b0b0efa4e00150769602230d6a844851de68dda332417ee": {
        "cases": ("asr_name_correction",), "mode": "exact"
    },
    "098727e895755cc7d8beb4ae2f7a1a070d6189e866fd71c7a73054d6d94c7e5d": {
        "cases": ("risk_followup",), "mode": "exact"
    },
    "a1d0831359cab6fc906dc9bbb03d865ddf451b214c6564e866b18e330a5108f2": {
        "cases": ("daily_weekly_followup",), "mode": "exact"
    },
    "35e95ddc53e9a567091fa3019f66dc4a912e82e20aff0adf80ce6825ee93497b": {
        "cases": ("correction_followup",), "mode": "exact"
    },
    "7a710a55aa0561bbdc9e6ecaf4dd38aad51295efcbf54e21eb85e7c6990b75e1": {
        "cases": ("copy_yesterday",), "mode": "exact"
    },
    "9ebe36f8cf23673a8933d22ad11532d1173695ec9cf6b1b58edf7cad66cb1d50": {
        "cases": ("standalone_submit",), "mode": "exact"
    },
    "e6cb3825be7a3dabf706a095560b35b9300971069ad573f51debf9ba5384c787": {
        "cases": ("standalone_submit",), "mode": "exact"
    },
    "36f33adaf0942634a8ece1eec4a6f30d44dec73e1ef8704b29d983a57f2e09ae": {
        "cases": ("bare_confirm",), "mode": "exact"
    },
    "08a85f4ab4bab9cac9adb34251164b48c783698f43a1978166000d48015fa2ca": {
        "cases": ("bare_confirm",), "mode": "exact"
    },
}
MODEL_AUDIT_FAILURES: list[dict[str, Any]] = []


class _ModelAuditCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        marker = "agent2_tool_call_model_audit "
        if marker not in message:
            return
        try:
            payload = json.loads(message.split(marker, 1)[1])
        except (IndexError, TypeError, json.JSONDecodeError):
            return
        if payload.get("status") == "failed":
            MODEL_AUDIT_FAILURES.append(
                {
                    "error_type": payload.get("error_type"),
                    "error_message_sha256": hashlib.sha256(
                        str(payload.get("error_message") or "").encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "failure_reason": payload.get("failure_reason"),
                    "model_call_count": payload.get("model_call_count"),
                }
            )


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _SmokeCheckError(AssertionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check(condition: object, code: str) -> None:
    if not condition:
        raise _SmokeCheckError(code)


def _sanitized_exception(exc: Exception) -> dict[str, str]:
    value = f"{type(exc).__name__}: {exc}"
    result = {
        "error_type": type(exc).__name__,
        "error_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code):
        result["error_code"] = code
    return result


def _write_private_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


async def _independent_daily_snapshot_review(
    llm_client: Any,
    *,
    source_messages: tuple[str, ...],
    stored_fields: dict[str, list[str]],
) -> dict[str, Any]:
    system_prompt = (
            "You are an independent acceptance evaluator for one saved Daily Report. "
            "Compare the exact user source with the stored snapshot. Business-control wording "
            "about saving, replacing, confirming, or submitting is not report content. Return "
            "exactly one JSON object with faithful, complete, correct_fields, partitioned, "
            "no_invention, and reason_code. The first five values are boolean. faithful means "
            "actors, projects, actions, objects, dates, numbers, attribution, negation, "
            "conditions, completion states, risks, and plans keep the source meaning. complete "
            "means every asserted Daily matter appears exactly once and no current-snapshot "
            "matter is omitted. correct_fields means performed work, real problems or risks, "
            "and next-reporting-day plans are in today_work, problems, and tomorrow_plan. "
            "partitioned means unrelated matters that can be progressed or reported independently "
            "remain separate, while coordinated actions for one shared work object and outcome "
            "may stay together. no_invention means no new fact or changed completion claim. Use "
            "reason_code=ok only when all five booleans are true; otherwise use one short stable "
            "snake_case defect label. Do not quote or repeat source or stored content."
    )
    user_prompt = json.dumps(
        {
            "ordered_source_messages": list(source_messages),
            "stored_daily_report": stored_fields,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    boolean_keys = {
        "faithful",
        "complete",
        "correct_fields",
        "partitioned",
        "no_invention",
    }
    required = {*boolean_keys, "reason_code"}
    payload = None
    for thinking_enabled, max_tokens in ((True, 8192), (False, 2048)):
        raw = await llm_client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model="deepseek-v4-flash",
            thinking_enabled=thinking_enabled,
            timeout_seconds=180,
            max_retries=1,
            max_tokens=max_tokens,
        )
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and set(candidate) == required:
            payload = candidate
            break
    _check(
        isinstance(payload, dict),
        "daily_snapshot_review_invalid_shape",
    )
    _check(
        all(isinstance(payload.get(key), bool) for key in boolean_keys),
        "daily_snapshot_review_non_boolean",
    )
    _check(
        all(payload[key] for key in boolean_keys)
        and payload.get("reason_code") == "ok",
        "daily_snapshot_review_rejected",
    )
    return payload


def _safe_schema_name() -> str:
    value = f"{SCHEMA_PREFIX}{uuid4().hex[:12]}"
    if not re.fullmatch(r"agent2_api_smoke_[a-f0-9]{12}", value):
        raise RuntimeError("isolated API schema name is unsafe")
    return value


def _configure_isolated_scope(user_ids: tuple[str, ...]) -> None:
    scope = ",".join(user_ids)
    values = {
        "APP_ENV": "production",
        "DB_POOL_SIZE": "3",
        "DB_MAX_OVERFLOW": "2",
        "SCHEDULER_ENABLED": "false",
        "REMINDER_SEND_ENABLED": "false",
        "PROGRESS_WORKER_ENABLED": "false",
        "AGENT2_TOOL_CALL_CANARY_MAX_ACTIVE_USERS": str(len(user_ids)),
        "AGENT2_BUSINESS_TENANT_IDS": TENANT_ID,
        "AGENT2_SEMANTIC_ADMISSION_TENANT_ALLOWLIST": TENANT_ID,
        "AGENT2_SEMANTIC_ADMISSION_USER_ALLOWLIST": scope,
        "AGENT2_DAILY_ENABLED": "true",
        "AGENT2_DAILY_ENABLED_USER_IDS": scope,
        "AGENT2_WEEKLY_PLAN_ENABLED": "true",
        "AGENT2_WEEKLY_PLAN_WRITE_ENABLED": "true",
        "AGENT2_WEEKLY_PLAN_SEND_ENABLED": "false",
        "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST": TENANT_ID,
        "AGENT2_WEEKLY_PLAN_USER_ALLOWLIST": scope,
        "AGENT2_WEEKLY_PLAN_SEND_USER_ALLOWLIST": "",
        "AGENT2_CURRENT_WEEKLY_REPORT_ENABLED": "false",
        "AGENT2_CURRENT_WEEKLY_REPORT_TENANT_ALLOWLIST": "",
        "AGENT2_CURRENT_WEEKLY_REPORT_USER_ALLOWLIST": "",
        "LEGAL_DAILY_DASHBOARD_ENABLED": "false",
        "AGENT2_CROSS_USER_DAILY_READ_ENABLED": "false",
        "AGENT2_CASE_FOLLOWUP_SEND_ENABLED": "false",
        "CASE_FOLLOWUP_SEND_ENABLED": "false",
        "AGENT2_TRAVEL_NOTIFICATION_WORKER_ENABLED": "false",
    }
    os.environ.update(values)


def _load_adverse_inputs(path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    rows = payload.get("inputs")
    if not isinstance(rows, list):
        raise TypeError("adverse input evidence is invalid")
    result = {
        str(item.get("input_sha256") or ""): str(
            item.get("message_text") or ""
        )
        for item in rows
        if isinstance(item, dict)
        and bool(item.get("message_text"))
    }
    if any(not key or not value for key, value in result.items()):
        raise RuntimeError("adverse input evidence is incomplete")
    mismatched_hashes = sorted(
        input_sha256
        for input_sha256, message_text in result.items()
        if hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        != input_sha256
    )
    if mismatched_hashes:
        raise RuntimeError(
            "adverse input evidence text/hash mismatch: "
            + ",".join(mismatched_hashes)
        )
    all_hashes = {
        str(item.get("input_sha256") or "")
        for item in rows
        if isinstance(item, dict) and item.get("input_sha256")
    }
    non_replayable = sorted(all_hashes - set(result))
    return result, {
        "artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "artifact_unique_inputs": len(all_hashes),
        "replayable_text_inputs": len(result),
        "non_replayable_input_sha256": non_replayable,
    }


async def _seed_scope(session_factory, *, user_ids: tuple[UUID, ...], control) -> None:
    from app.agent2.business.models import (
        Agent2IdentityBinding,
        TenantRouteControl,
    )
    from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
    from app.models import Team, User

    team_id = uuid5(NAMESPACE_URL, f"{TENANT_ID}:team")
    async with session_factory() as session:
        session.add(
            Team(
                id=team_id,
                code="agent2-isolated-api-smoke",
                name="Agent2 Isolated API Smoke",
                department_name="Smoke",
                active=True,
            )
        )
        session.add(
            TenantRouteControl(
                tenant_id=TENANT_ID,
                route_mode="agent2_primary",
                canary_user_ids=[],
                agent1_rollback_enabled=False,
                version=1,
                changed_by="isolated-api-smoke",
                change_reason="isolated API validation",
            )
        )
        for index, user_id in enumerate(user_ids, start=1):
            ding_id = f"__agent2_isolated_api_smoke_{index:02d}__"
            session.add(
                User(
                    id=user_id,
                    dingtalk_user_id=ding_id,
                    employee_no=f"isolated-api-{index:02d}",
                    name=f"隔离API测试{index:02d}",
                    team_id=team_id,
                    role="member",
                    timezone="Asia/Shanghai",
                    active=True,
                )
            )
            session.add(
                Agent2IdentityBinding(
                    tenant_id=TENANT_ID,
                    company_id="isolated-company",
                    department_id="isolated-department",
                    team_id="isolated-team",
                    user_id=str(user_id),
                    dingtalk_user_id=ding_id,
                    display_name=f"隔离API测试{index:02d}",
                    role_ids=["member"],
                    permission_scope_json={},
                    active=True,
                )
            )
            session.add(
                ToolCallCanaryControl(
                    control_key=f"isolated-api-{index:02d}",
                    tenant_id=TENANT_ID,
                    user_id=str(user_id),
                    enabled=True,
                    runtime="canary_execute",
                    messages_enabled=True,
                    registry_digest=str(control["registry_digest"]),
                    prompt_sha256=str(control["prompt_sha256"]),
                    model_name=str(control["model_name"]),
                    version=int(control["version"]),
                    changed_by="isolated-api-smoke",
                    change_reason="isolated API validation",
                )
            )
        await session.commit()


async def _assert_canary_scope(session_factory, *, user_id: UUID) -> None:
    from app.agent2.tool_calling.canary_service import (
        resolve_tool_call_canary_route,
    )
    from app.config import get_settings
    from app.models import User

    async with session_factory() as session:
        user = await session.get(User, user_id)
        resolution = await resolve_tool_call_canary_route(
            session,
            user=user,
            dingtalk_user_id=str(user.dingtalk_user_id),
            settings=get_settings(),
            conversation_id="isolated-api-preflight",
            source_message_id="isolated-api-preflight",
            now=datetime.now(LOCAL_TZ),
        )
        _assert(
            resolution.decision.owner == "tool_call_core",
            f"isolated canary preflight blocked: {resolution.decision.reason}",
        )
        await session.rollback()


async def _seed_weekly_collection(
    session_factory,
    *,
    user_ids: tuple[UUID, ...],
) -> None:
    from app.agent2.weekly_plan_collection import WeeklyPlanCollectionWindow
    from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
    from app.agent2.weekly_plan_sql_collection import (
        SqlWeeklyPlanCollectionOrchestrator,
    )
    from app.agent2.weekly_plan_store import SqlWeeklyPlanStore

    opens_at = datetime.combine(
        TARGET_WEEK_START - timedelta(days=3),
        datetime.min.time().replace(hour=15),
        LOCAL_TZ,
    )
    deadline_at = datetime.combine(
        TARGET_WEEK_START,
        datetime.min.time().replace(hour=9),
        LOCAL_TZ,
    )
    roster = tuple(
        WeeklyPlanRosterMember(
            user_id=str(user_id),
            display_name=f"隔离API测试{index:02d}",
            department_id="isolated-department",
            department_name="Smoke",
            team_id="isolated-team",
            team_name="Smoke",
        )
        for index, user_id in enumerate(user_ids, start=1)
    )
    async with session_factory() as session:
        await SqlWeeklyPlanCollectionOrchestrator(
            SqlWeeklyPlanStore(session)
        ).open_collection(
            tenant_id=TENANT_ID,
            target_week_start=TARGET_WEEK_START,
            source_roster=roster,
            canary_user_ids=frozenset(str(value) for value in user_ids),
            window=WeeklyPlanCollectionWindow(
                opens_at=opens_at,
                deadline_at=deadline_at,
                late_fill_until=datetime.combine(
                    TARGET_WEEK_START,
                    datetime.max.time(),
                    LOCAL_TZ,
                ),
            ),
        )
        await session.commit()


def _draft_item_ids(values: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        field: [f"{field}-{index}" for index, _ in enumerate(items, start=1)]
        for field, items in values.items()
    }


async def _seed_report(
    session_factory,
    *,
    user_id: UUID,
    team_id: UUID,
    report_date: date,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    status: str = "collecting",
    acknowledge_problems: bool = False,
) -> None:
    from app.models import DailyReport

    values = {
        "today_work": list(today_work),
        "problems": list(problems),
        "tomorrow_plan": list(tomorrow_plan),
    }
    section_status: dict[str, Any] = {
        "_agent2_report_version": 1,
        "_draft_item_ids": _draft_item_ids(values),
        "_agent2_typed_audit": [],
    }
    if acknowledge_problems:
        section_status["problems_acknowledged_empty"] = True
    async with session_factory() as session:
        session.add(
            DailyReport(
                user_id=user_id,
                team_id=team_id,
                report_date=report_date,
                today_work=values["today_work"],
                problems=values["problems"],
                tomorrow_plan=values["tomorrow_plan"],
                emotion="",
                raw_input="isolated API smoke seed",
                input_fragments=[],
                section_status=section_status,
                completeness_score=Decimal(1) if acknowledge_problems else Decimal("0.67"),
                status=status,
                confirmation_type="none",
                confirmed_by_user=False,
                last_modified_by_user=False,
                source="isolated_api_smoke_seed",
                llm_model="isolated-api-smoke",
                llm_payload={},
            )
        )
        await session.commit()


async def _report(session_factory, user_id: UUID, report_date: date) -> Any | None:
    from app.models import DailyReport

    async with session_factory() as session:
        return await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == user_id,
                DailyReport.report_date == report_date,
            )
        )


def _report_fields(report: Any) -> dict[str, list[str]]:
    return {
        field: list(getattr(report, field) or [])
        for field in ("today_work", "problems", "tomorrow_plan")
    }


async def _weekly_snapshot(session_factory, user_id: UUID) -> dict[str, Any] | None:
    from app.agent2.weekly_plan_store import _days, _items, _plans

    async with session_factory() as session:
        plan = (
            await session.execute(
                select(_plans).where(
                    _plans.c.tenant_id == TENANT_ID,
                    _plans.c.owner_user_id == str(user_id),
                    _plans.c.target_week_start == TARGET_WEEK_START,
                )
            )
        ).mappings().one_or_none()
        if plan is None:
            return None
        rows = (
            await session.execute(
                select(_days.c.plan_date, _items.c.original_text)
                .select_from(
                    _days.join(_items, _days.c.day_id == _items.c.day_id)
                )
                .where(
                    _days.c.plan_id == plan["plan_id"],
                    _items.c.deleted_at.is_(None),
                )
                .order_by(_days.c.plan_date, _items.c.position)
            )
        ).all()
    return {
        "status": str(plan["status"]),
        "version": int(plan["version"]),
        "items": [(row.plan_date.isoformat(), str(row.original_text)) for row in rows],
    }


async def _recent_tool_failures(session_factory) -> list[dict[str, Any]]:
    from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ToolCallCanaryReceipt)
                    .where(ToolCallCanaryReceipt.status != "success")
                    .order_by(ToolCallCanaryReceipt.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
    return [
        {
            "source_message_id": row.source_message_id,
            "tool_name": row.tool_name,
            "status": row.status,
            "error_code": row.error_code,
            "changed": bool(row.changed),
        }
        for row in rows
    ]


async def _run_cases(
    client: httpx.AsyncClient,
    evaluator_llm_client: Any,
    session_factory,
    *,
    user_ids: tuple[UUID, ...],
    team_id: UUID,
    inputs: dict[str, str],
    selected_cases: frozenset[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    async def post(
        case_index: int,
        message: str,
        turn: str,
        *,
        conversation: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        response = await client.post(
            "/reports/manual",
            json={
                "dingtalk_user_id": f"__agent2_isolated_api_smoke_{case_index:02d}__",
                "raw_input": message,
                "source": "isolated_api_smoke",
                "conversation_id": conversation or f"isolated-api-{case_index:02d}",
                "idempotency_key": key or f"isolated-api-{case_index:02d}-{turn}",
            },
        )
        response.raise_for_status()
        payload = response.json()
        blocked = {
            "agent2_entrypoint_blocked",
            "agent2_cognitive_core_disabled",
            "agent2_unavailable_fail_closed",
            "cognitive_v3_unavailable",
            "agent2_tool_call_blocked",
            "agent2_tool_call_failed",
        }
        if payload.get("reply_kind") in blocked:
            reason = re.sub(
                r"[^a-z0-9_]+",
                "_",
                str(payload.get("outcome_reason") or "unknown").casefold(),
            ).strip("_")[:20]
            model_calls = max(0, int(payload.get("model_call_count") or 0))
            transport_retries = max(
                0,
                int(payload.get("model_transport_retry_count") or 0),
            )
            observations = payload.get("pre_execution_block_observations")
            observation = (
                observations[0]
                if isinstance(observations, list)
                and observations
                and isinstance(observations[0], dict)
                else {}
            )
            tool_name = re.sub(
                r"[^a-z0-9_]+",
                "_",
                str(observation.get("tool_name") or "none").casefold(),
            ).strip("_")[:12]
            tool_error = re.sub(
                r"[^a-z0-9_]+",
                "_",
                str(observation.get("error_code") or "none").casefold(),
            ).strip("_")[:18]
            visible = re.sub(
                r"[^a-z0-9_]+",
                "_",
                str(payload.get("user_visible_result") or "unknown").casefold(),
            ).strip("_")[:10]
            blocked_count = max(
                0,
                int(payload.get("tool_blocked_count") or 0),
            )
            clarification_count = max(
                0,
                int(payload.get("tool_clarification_count") or 0),
            )
            failure_count = max(
                0,
                int(payload.get("tool_failure_count") or 0),
            )
            raise _SmokeCheckError(
                f"manual_{visible}_{reason}_{tool_name}_{tool_error}_m{model_calls}b{blocked_count}c{clarification_count}f{failure_count}r{transport_retries}"
            )
        return payload

    async def run(name: str, factory) -> None:
        started = time.perf_counter()
        audit_start = len(MODEL_AUDIT_FAILURES)
        try:
            detail = await factory()
        except Exception as exc:  # noqa: BLE001 - one failed case must not hide later coverage
            results.append(
                {
                    "name": name,
                    "status": "FAIL",
                    **_sanitized_exception(exc),
                    "seconds": round(time.perf_counter() - started, 3),
                    "model_audit_failures": MODEL_AUDIT_FAILURES[
                        audit_start:
                    ],
                    "tool_failures": await _recent_tool_failures(
                        session_factory
                    ),
                }
            )
        else:
            results.append(
                {
                    "name": name,
                    "status": "PASS",
                    "seconds": round(time.perf_counter() - started, 3),
                    "detail": detail,
                }
            )
        print(json.dumps(results[-1], ensure_ascii=False, sort_keys=True), flush=True)

    async def short_full() -> dict[str, Any]:
        source = inputs["6b0f87e35d17a7230afed182bea7e82bde0040158d7e9d5e1b768426ecb722c1"]
        await post(1, source, "write")
        report = await _report(session_factory, user_ids[0], TODAY)
        fields = _report_fields(report)
        _assert(
            [len(fields[key]) for key in fields] == [5, 0, 3],
            f"short full report split mismatch: {fields!r}",
        )
        review = await _independent_daily_snapshot_review(
            evaluator_llm_client,
            source_messages=(source,),
            stored_fields=fields,
        )
        return {
            "field_counts": {key: len(value) for key, value in fields.items()},
            "semantic_review": review,
        }

    async def structured_full() -> dict[str, Any]:
        source = inputs["54595ef90ad53d5ff4427282bf3a250a11c6d0be65dc7756310a80e70b94d491"]
        await post(2, source, "write")
        report = await _report(session_factory, user_ids[1], TODAY)
        fields = _report_fields(report)
        _assert(
            [len(fields[key]) for key in fields] == [7, 0, 5],
            f"structured full report split mismatch: {fields!r}",
        )
        review = await _independent_daily_snapshot_review(
            evaluator_llm_client,
            source_messages=(source,),
            stored_fields=fields,
        )
        return {
            "field_counts": {key: len(value) for key, value in fields.items()},
            "semantic_review": review,
        }

    async def long_voice_text() -> dict[str, Any]:
        source = inputs["5c7a525460993f6c44972a1bd1495220f15cb30c407f6181d6db4d086b29f29b"]
        await post(3, source, "write")
        report = await _report(session_factory, user_ids[2], TODAY)
        fields = _report_fields(report)
        counts = {key: len(value) for key, value in fields.items()}
        _check(
            all(counts[key] >= 1 for key in counts),
            "long_report_lost_section",
        )
        review = await _independent_daily_snapshot_review(
            evaluator_llm_client,
            source_messages=(source,),
            stored_fields=fields,
        )
        return {"field_counts": counts, "semantic_review": review}

    async def copy_yesterday() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[3],
            team_id=team_id,
            report_date=TODAY - timedelta(days=1),
            today_work=["完成合同复核", "整理付款材料"],
            problems=[],
            tomorrow_plan=["继续跟进签署", "同步项目组"],
            status="completed",
            acknowledge_problems=True,
        )
        response = await post(
            4,
            inputs[
                "7a710a55aa0561bbdc9e6ecaf4dd38aad51295efcbf54e21eb85e7c6990b75e1"
            ],
            "copy",
        )
        report = await _report(session_factory, user_ids[3], TODAY)
        _assert(
            report is not None,
            "copy did not create today's report: "
            f"reply={response.get('message')!r}",
        )
        fields = _report_fields(report)
        _assert(fields["today_work"] == ["完成合同复核", "整理付款材料"], "copy lost today work")
        _assert(fields["tomorrow_plan"] == ["继续跟进签署", "同步项目组"], "copy lost plans")
        return {"field_counts": {key: len(value) for key, value in fields.items()}}

    async def bare_confirm() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[4],
            team_id=team_id,
            report_date=TODAY,
            today_work=["完成合同复核"],
            problems=[],
            tomorrow_plan=["继续跟进签署"],
            status="pending_confirmation",
            acknowledge_problems=True,
        )
        await post(
            5,
            inputs[
                "08a85f4ab4bab9cac9adb34251164b48c783698f43a1978166000d48015fa2ca"
            ],
            "submit",
        )
        report = await _report(session_factory, user_ids[4], TODAY)
        _assert(report.status == "completed", "bare confirmation did not submit")
        await post(
            5,
            inputs[
                "36f33adaf0942634a8ece1eec4a6f30d44dec73e1ef8704b29d983a57f2e09ae"
            ],
            "confirm-no-op",
        )
        report = await _report(session_factory, user_ids[4], TODAY)
        _assert(report.status == "completed", "bare confirmation changed completed status")
        return {"status": report.status}

    async def risk_followup() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[5],
            team_id=team_id,
            report_date=TODAY,
            today_work=["完成电子印章功能沟通"],
            problems=[],
            tomorrow_plan=["继续跟进开发"],
        )
        response = await post(6, inputs["098727e895755cc7d8beb4ae2f7a1a070d6189e866fd71c7a73054d6d94c7e5d"], "risk")
        report = await _report(session_factory, user_ids[5], TODAY)
        _assert(
            "百润电子印章" in "\n".join(report.problems or []),
            "risk follow-up was not saved: "
            f"problems={list(report.problems or [])!r}; reply={response.get('message')!r}",
        )
        return {"problem_count": len(report.problems or [])}

    async def daily_weekly_followup() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[6],
            team_id=team_id,
            report_date=TODAY,
            today_work=["完成既有工作"],
            problems=[],
            tomorrow_plan=["返程", "实地案件相关工作"],
            acknowledge_problems=True,
        )
        await post(7, inputs["a1d0831359cab6fc906dc9bbb03d865ddf451b214c6564e866b18e330a5108f2"], "followup")
        report = await _report(session_factory, user_ids[6], TODAY)
        weekly = await _weekly_snapshot(session_factory, user_ids[6])
        combined = "\n".join(report.tomorrow_plan or []) + "\n" + "\n".join(
            item[1] for item in (weekly or {}).get("items", [])
        )
        _assert("其他日常工作" in combined, "daily follow-up matter was lost")
        _assert("团队会议沟通安排" in combined, "Saturday weekly matter was lost")
        daily_plan = "\n".join(report.tomorrow_plan or [])
        _assert(
            weekly is not None
            and len((weekly or {}).get("items", [])) == 0
            and "其他日常工作" in daily_plan
            and "团队会议沟通安排" in daily_plan,
            "next-reporting-day plans were routed into the Weekly Work Plan",
        )
        return {"daily_plan_count": len(report.tomorrow_plan or []), "weekly_item_count": len((weekly or {}).get("items", []))}

    async def no_change_plus_plan() -> dict[str, Any]:
        today_items = ["齐河智慧产业园答辩意见", "广州融创化债沟通", "宋都信息公开跟进"]
        await _seed_report(
            session_factory,
            user_id=user_ids[7],
            team_id=team_id,
            report_date=TODAY,
            today_work=today_items,
            problems=[],
            tomorrow_plan=[],
            acknowledge_problems=True,
        )
        response = await post(8, inputs["0f03eb7a0c8e4a2f3ac6ca15539876aff1e3ddee6341df64b98ebd72ce8ce99d"], "plan")
        report = await _report(session_factory, user_ids[7], TODAY)
        _assert(list(report.today_work or []) == today_items, "no-change statement rewrote today work")
        body = "\n".join(report.tomorrow_plan or [])
        _assert(
            "苏宁38家债权" in body and "齐河智慧产业园" in body,
            "two plans were not saved: "
            f"plans={list(report.tomorrow_plan or [])!r}; reply={response.get('message')!r}",
        )
        return {"today_count": len(report.today_work or []), "plan_count": len(report.tomorrow_plan or [])}

    async def whole_report_replacement() -> dict[str, Any]:
        first = inputs["6b0f87e35d17a7230afed182bea7e82bde0040158d7e9d5e1b768426ecb722c1"]
        replacement = inputs["d953c1934cca0c681f325c6fc97849b6e16e920ab9a7a122a85a015548e399a0"]
        await post(9, first, "first")
        before = await _report(session_factory, user_ids[8], TODAY)
        before_status = before.status
        await post(9, replacement, "replacement")
        report = await _report(session_factory, user_ids[8], TODAY)
        fields = _report_fields(report)
        _assert(len(fields["today_work"]) == 5, "whole replacement duplicated today work")
        _assert(len(fields["tomorrow_plan"]) == 3, "whole replacement lost a plan")
        plan_body = "\n".join(fields["tomorrow_plan"])
        _assert("福州人才港" in plan_body, "whole replacement did not apply new plan")
        _assert(report.status == before_status, "whole replacement changed report status")
        review = await _independent_daily_snapshot_review(
            evaluator_llm_client,
            source_messages=(replacement,),
            stored_fields=fields,
        )
        return {
            "field_counts": {key: len(value) for key, value in fields.items()},
            "status_preserved": True,
            "semantic_review": review,
        }

    async def correction_followup() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[9],
            team_id=team_id,
            report_date=TODAY,
            today_work=[
                "整理70份协议明细，准备明天班车传至西方",
                "英语审核",
            ],
            problems=[],
            tomorrow_plan=[
                "70份协议放置班车传至西环",
                "英语审核",
            ],
            acknowledge_problems=True,
        )
        wording_response = await post(10, "英语审核的“英语”改成用印作用的“用”，印章的“印”", "wording", conversation="isolated-correction")
        location_response = await post(10, inputs["35e95ddc53e9a567091fa3019f66dc4a912e82e20aff0adf80ce6825ee93497b"], "location", conversation="isolated-correction")
        report = await _report(session_factory, user_ids[9], TODAY)
        body = "\n".join([*(report.today_work or []), *(report.tomorrow_plan or [])])
        _assert(
            "用印审核" in body and "英语审核" not in body,
            "ASR wording correction failed: "
            f"body={body!r}; replies={[wording_response.get('message'), location_response.get('message')]!r}",
        )
        _assert(
            "总部" in body and "西方" not in body and "西环" not in body,
            "location correction failed: "
            f"body={body!r}; reply={location_response.get('message')!r}",
        )
        _assert("改为总部" not in body and "东西南北" not in body, "correction instruction was stored as content")
        return {"today_count": len(report.today_work or []), "plan_count": len(report.tomorrow_plan or [])}

    async def asr_name_correction() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[15],
            team_id=team_id,
            report_date=TODAY,
            today_work=["陆警方准备明天班车传至西方"],
            problems=[],
            tomorrow_plan=["陆警方准备明天班车传至西环"],
            acknowledge_problems=True,
        )
        response = await post(
            16,
            inputs[
                "e6a65f0d6d450bdf9b0b0efa4e00150769602230d6a844851de68dda332417ee"
            ],
            "name-location-correction",
            conversation="isolated-asr-name-correction",
        )
        report = await _report(session_factory, user_ids[15], TODAY)
        body = "\n".join(
            [*(report.today_work or []), *(report.tomorrow_plan or [])]
        )
        _check(
            "陆静芳" in body and "陆警方" not in body,
            "asr_name_correction_not_applied",
        )
        _check(
            "西环" in body
            and "大陆的陆" not in body
            and "安静的静" not in body,
            "asr_correction_instruction_stored_as_content",
        )
        return {
            "today_count": len(report.today_work or []),
            "plan_count": len(report.tomorrow_plan or []),
            "reply_kind": response.get("reply_kind"),
        }

    async def same_turn_submit() -> dict[str, Any]:
        await post(11, "今天完成合同复核，明天整理附件，请提交日报。", "submit")
        report = await _report(session_factory, user_ids[10], TODAY)
        _assert(report.status == "completed", "same-turn explicit submit failed")
        return {"status": report.status}

    async def standalone_submit() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[11],
            team_id=team_id,
            report_date=TODAY,
            today_work=["完成合同复核"],
            problems=[],
            tomorrow_plan=["继续整理附件"],
        )
        await post(
            12,
            inputs[
                "e6cb3825be7a3dabf706a095560b35b9300971069ad573f51debf9ba5384c787"
            ],
            "submit-log",
        )
        report = await _report(session_factory, user_ids[11], TODAY)
        _assert(report.status == "completed", "explicit standalone submit stayed blocked by an omitted section")
        await post(
            12,
            inputs[
                "9ebe36f8cf23673a8933d22ad11532d1173695ec9cf6b1b58edf7cad66cb1d50"
            ],
            "confirm-submit-no-op",
        )
        report = await _report(session_factory, user_ids[11], TODAY)
        _assert(report.status == "completed", "repeated submit changed completed status")
        return {"status": report.status}

    async def weekly_crud() -> dict[str, Any]:
        add_response = await post(13, "下周一复核采购合同；下周二整理付款资料；周六团队会议沟通安排。先别提交。", "add", conversation="isolated-weekly")
        first = await _weekly_snapshot(session_factory, user_ids[12])
        _assert(
            first is not None and len(first["items"]) == 3,
            "weekly add did not create three items: "
            f"snapshot={first!r}; reply={add_response.get('message')!r}",
        )
        await post(13, "把下周二的计划改成整理付款资料并同步财务，然后提交下周计划。", "edit-submit", conversation="isolated-weekly")
        second = await _weekly_snapshot(session_factory, user_ids[12])
        _assert(
            second is not None
            and second["status"] in {"collecting", "pending_confirmation"},
            "weekly change did not remain available for confirmation",
        )
        _assert("同步财务" in "\n".join(item[1] for item in second["items"]), "weekly edit was not saved")
        await post(13, "确认提交下周计划。", "confirm", conversation="isolated-weekly")
        third = await _weekly_snapshot(session_factory, user_ids[12])
        _assert(third is not None and third["status"] == "submitted", "weekly plan was not submitted after confirmation")
        await post(13, "给我看一下下周工作计划。", "query", conversation="isolated-weekly")
        fourth = await _weekly_snapshot(session_factory, user_ids[12])
        _assert(fourth == third, "weekly query changed data")
        return {"item_count": len(third["items"]), "status": third["status"], "version": third["version"]}

    async def idempotency() -> dict[str, Any]:
        key = "isolated-api-idempotency-replay"
        first = await post(14, "今天完成合同审核。", "first", key=key)
        second = await post(14, "今天完成合同审核。", "second", key=key)
        report = await _report(session_factory, user_ids[13], TODAY)
        _assert(first == second, "API idempotency response changed")
        _assert(
            len(report.today_work or []) == 1
            and "完成合同审核" in str((report.today_work or [""])[0]),
            "API replay duplicated content",
        )
        return {"response_equal": True, "today_count": len(report.today_work or [])}

    async def daily_crud() -> dict[str, Any]:
        await _seed_report(
            session_factory,
            user_id=user_ids[14],
            team_id=team_id,
            report_date=TODAY,
            today_work=["合同初稿复核", "整理付款材料"],
            problems=[],
            tomorrow_plan=["继续跟进"],
            acknowledge_problems=True,
        )
        await post(15, "把今天日报今日工作的第一条改成完成合同终稿复核。", "edit", conversation="isolated-crud")
        await post(15, "删除今天日报今日工作的第二条。", "delete", conversation="isolated-crud")
        before = await _report(session_factory, user_ids[14], TODAY)
        await post(15, "给我看一下今天的日报。", "query", conversation="isolated-crud")
        after = await _report(session_factory, user_ids[14], TODAY)
        _assert(_report_fields(before) == _report_fields(after), "daily query changed content")
        _assert(list(after.today_work or []) == ["完成合同终稿复核"], "daily edit/delete result mismatch")
        return {"today_count": len(after.today_work or [])}

    factories = (
        ("short_full", short_full),
        ("structured_full", structured_full),
        ("long_voice_text", long_voice_text),
        ("copy_yesterday", copy_yesterday),
        ("bare_confirm", bare_confirm),
        ("risk_followup", risk_followup),
        ("daily_weekly_followup", daily_weekly_followup),
        ("no_change_plus_plan", no_change_plus_plan),
        ("whole_report_replacement", whole_report_replacement),
        ("correction_followup", correction_followup),
        ("same_turn_submit", same_turn_submit),
        ("standalone_submit", standalone_submit),
        ("weekly_crud", weekly_crud),
        ("idempotency", idempotency),
        ("daily_crud", daily_crud),
        ("asr_name_correction", asr_name_correction),
    )
    for name, factory in factories:
        if selected_cases and name not in selected_cases:
            continue
        await run(name, factory)
    return results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adverse-inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    selected_cases = frozenset(str(value) for value in args.case)
    unknown_cases = selected_cases - set(CASE_NAMES)
    if unknown_cases:
        parser.error(f"unknown cases: {sorted(unknown_cases)}")
    schema = _safe_schema_name()
    user_uuid_values = tuple(
        uuid5(NAMESPACE_URL, f"{TENANT_ID}:{name}") for name in CASE_NAMES
    )
    user_ids = tuple(str(value) for value in user_uuid_values)
    _configure_isolated_scope(user_ids)
    inputs, adverse_evidence = _load_adverse_inputs(args.adverse_inputs)
    unmapped_inputs = sorted(set(inputs) - set(ADVERSE_CASE_COVERAGE))
    if unmapped_inputs:
        raise RuntimeError(
            "adverse input evidence contains unmapped replayable inputs: "
            + ",".join(unmapped_inputs)
        )
    production_url = os.environ.get("DATABASE_URL", "")
    if not production_url:
        raise RuntimeError("DATABASE_URL is required")
    admin_engine = create_async_engine(production_url, poolclass=NullPool)
    schema_engine = None
    schema_created = False
    app_started = False
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True

        from app.config import get_settings

        get_settings.cache_clear()
        import app.db as app_db

        await app_db.engine.dispose()
        schema_engine = create_async_engine(
            production_url,
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": schema}},
        )

        @event.listens_for(schema_engine.sync_engine, "handle_error")
        def _record_isolated_database_error(exception_context) -> None:
            original = exception_context.original_exception
            original_text = str(original)
            statement = str(exception_context.statement or "")
            table_match = re.search(
                r"(?:INSERT\s+INTO|UPDATE|FROM)\s+([a-zA-Z0-9_.]+)",
                statement,
                flags=re.IGNORECASE,
            )
            print(
                "ISOLATED_DB_ERROR "
                + json.dumps(
                    {
                        "type": type(original).__name__,
                        "message_sha256": hashlib.sha256(
                            original_text.encode("utf-8")
                        ).hexdigest(),
                        "table": table_match.group(1) if table_match else None,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
        session_factory = async_sessionmaker(
            schema_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        app_db.engine = schema_engine
        app_db.AsyncSessionLocal = session_factory
        app_db.settings = get_settings()

        from app.agent2.admission_artifact_sink_sql import (
            _metadata as admission_artifact_metadata,
        )
        from app.agent2.admission_store_sql import (
            _metadata as admission_store_metadata,
        )
        from app.agent2.information_pending_sql import (
            _metadata as information_pending_metadata,
        )
        from app.agent2.selection_pending_sql import (
            _metadata as selection_pending_metadata,
        )
        from app.agent2.tool_calling.canary_service import _runtime_attestation
        from app.agent2.weekly_plan_reminder_outbox import (
            _metadata as weekly_reminder_metadata,
        )
        from app.agent2.weekly_plan_store import _metadata as weekly_metadata
        from app.main import app, lifespan

        model_audit_logger = logging.getLogger(
            "agent2.tool_calling.model_audit"
        )
        model_audit_logger.setLevel(logging.INFO)
        model_audit_logger.addHandler(_ModelAuditCapture())

        async with schema_engine.begin() as connection:
            active_schema = await connection.scalar(text("SELECT current_schema()"))
            _assert(active_schema == schema, "isolated API engine escaped its schema")
            await connection.run_sync(app_db.Base.metadata.create_all)
            await connection.run_sync(admission_artifact_metadata.create_all)
            await connection.run_sync(admission_store_metadata.create_all)
            await connection.run_sync(information_pending_metadata.create_all)
            await connection.run_sync(selection_pending_metadata.create_all)
            await connection.run_sync(weekly_reminder_metadata.create_all)
            await connection.run_sync(weekly_metadata.create_all)
            for ddl in (
                (
                    "CREATE UNIQUE INDEX weekly_batch_tenant_week_uq ON "
                    "agent2_weekly_plan_batches (tenant_id, target_week_start)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_roster_user_uq ON "
                    "agent2_weekly_plan_roster_members (tenant_id, batch_id, user_id)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_plan_owner_week_uq ON "
                    "agent2_weekly_plans (tenant_id, owner_user_id, target_week_start)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_day_date_uq ON "
                    "agent2_weekly_plan_days (tenant_id, plan_id, plan_date)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_day_index_uq ON "
                    "agent2_weekly_plan_days (tenant_id, plan_id, day_index)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_suggestion_source_uq ON "
                    "agent2_weekly_plan_suggestions "
                    "(tenant_id, plan_id, source_kind, source_ref, source_version)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_receipt_idempotency_uq ON "
                    "agent2_weekly_plan_command_receipts (tenant_id, idempotency_key)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_snapshot_batch_uq ON "
                    "agent2_weekly_plan_monday_snapshots (tenant_id, batch_id)"
                ),
                (
                    "CREATE UNIQUE INDEX weekly_reminder_idempotency_uq ON "
                    "agent2_weekly_plan_reminder_outbox (tenant_id, idempotency_key)"
                ),
            ):
                await connection.execute(text(ddl))

        runtime = _runtime_attestation(get_settings())
        control = {
            "registry_digest": runtime.registry_digest,
            "prompt_sha256": runtime.prompt_sha256,
            "model_name": runtime.model_name,
            "version": 1,
        }
        await _seed_scope(
            session_factory,
            user_ids=user_uuid_values,
            control=control,
        )
        await _assert_canary_scope(
            session_factory,
            user_id=user_uuid_values[0],
        )
        await _seed_weekly_collection(
            session_factory,
            user_ids=user_uuid_values,
        )
        team_id = uuid5(NAMESPACE_URL, f"{TENANT_ID}:team")
        async with lifespan(app):
            app_started = True
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://agent2-isolated-api",
                timeout=httpx.Timeout(360),
            ) as client:
                health = await client.get("/health")
                _assert(health.status_code == 200, "isolated API health failed")
                results = await _run_cases(
                    client,
                    app.state.llm_client,
                    session_factory,
                    user_ids=user_uuid_values,
                    team_id=team_id,
                    inputs=inputs,
                    selected_cases=selected_cases,
                )
        app_started = False
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        if schema_created:
            if not re.fullmatch(r"agent2_api_smoke_[a-f0-9]{12}", schema):
                raise RuntimeError("refusing to drop an unsafe schema")
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()

    summary = {
        "total": len(results),
        "pass": sum(item["status"] == "PASS" for item in results),
        "fail": sum(item["status"] == "FAIL" for item in results),
        "seconds": round(time.perf_counter() - started, 3),
        "production_api_code": True,
        "production_llm": get_settings().llm_intent_model,
        "isolated_postgresql_schema_removed": schema_created and not app_started,
        "dingtalk_send_calls": 0,
    }
    executed_cases = {item["name"] for item in results}
    coverage_rows = [
        {
            "input_sha256": input_sha256,
            "cases": list(mapping["cases"]),
            "mode": mapping["mode"],
            "executed": bool(
                executed_cases.intersection(mapping["cases"])
            ),
        }
        for input_sha256, mapping in sorted(
            ADVERSE_CASE_COVERAGE.items()
        )
        if input_sha256 in inputs
    ]
    payload = {
        "summary": summary,
        "adverse_evidence": {
            **adverse_evidence,
            "mapped_replayable_inputs": len(coverage_rows),
            "unmapped_replayable_input_sha256": unmapped_inputs,
            "coverage": coverage_rows,
        },
        "results": results,
    }
    _write_private_json(args.output, payload)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(f"OUTPUT {args.output}")
    return 1 if summary["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
