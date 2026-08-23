from __future__ import annotations

import argparse
import asyncio
from datetime import date
import json
import os
from pathlib import Path
from uuid import UUID


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-process-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-counts", default="")
    parser.add_argument("--owner-names", default="")
    parser.add_argument(
        "--row-status",
        choices=("generation_failed", "snapshot_ready"),
        default="generation_failed",
    )
    return parser.parse_args()


def _load_process_environment(pid: int) -> None:
    values = {
        item.split(b"=", 1)[0].decode(): item.split(b"=", 1)[1].decode()
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in item
    }
    if pid <= 1 or not values:
        raise RuntimeError("source process environment is unavailable")
    os.environ.clear()
    os.environ.update(values)


def _safe_error_category(exc: Exception) -> str:
    detail = str(getattr(exc, "repair_detail", "") or str(exc)).lower()
    categories = (
        ("independent model review rejected", "semantic_review_rejected"),
        ("payload keys", "payload_keys"),
        ("source universe is incomplete", "source_coverage"),
        ("source dispositions are incomplete", "source_dispositions"),
        ("item sources are invalid", "item_source_shape"),
        ("unknown sources", "unknown_source"),
        ("completed item requires", "completed_evidence"),
        ("plan progress must cover", "weekly_plan_coverage"),
        ("plan progress requires", "weekly_plan_evidence"),
        ("plan status requires", "plan_status_evidence"),
        ("no-follow-up status", "no_followup_evidence"),
        ("critical facts are missing", "critical_fact_missing"),
        ("critical facts cannot be excluded", "critical_fact_excluded"),
        ("message is too long", "message_too_long"),
        ("section is invalid", "section_shape"),
        ("items are invalid", "items_shape"),
        ("cannot mix items", "empty_note_conflict"),
        ("item is invalid", "item_shape"),
        ("independent model review", "review_output"),
        ("invalid output", "other_model_validation"),
    )
    return next(
        (category for marker, category in categories if marker in detail),
        "unclassified",
    )


async def main() -> None:
    args = _args()
    if args.output.exists() or args.output.is_symlink():
        raise RuntimeError("failed-snapshot evidence path exists")
    _load_process_environment(args.source_process_id)

    from sqlalchemy import select

    from app.agent2.personal_weekly_brief import (
        Agent2PersonalWeeklyBriefGenerator,
        Agent2PersonalWeeklyBriefModelPipeline,
        Agent2PersonalWeeklyBriefReviewer,
        PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS,
        PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED,
        PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
        PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
        PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS,
        PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
        PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
        PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES,
        PersonalWeeklyBriefSnapshot,
    )
    from app.agent2.personal_weekly_brief_store import SqlPersonalWeeklyBriefStore
    from app.agent2.tool_calling.canary_config import (
        CANARY_MAX_REQUEST_ATTEMPTS,
        CANARY_MODEL_NAME,
        CANARY_TIMEOUT_SECONDS,
    )
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.llm.client import LLMClient
    from app.models import User
    from app.utils.json import extract_json_object

    settings = get_settings()
    tenant_values = tuple(
        value.strip()
        for value in str(settings.agent2_weekly_plan_tenant_allowlist or "").split(",")
        if value.strip()
    )
    if len(tenant_values) != 1:
        raise RuntimeError("runtime tenant is not unique")
    tenant_id = tenant_values[0]
    week_start = date(2026, 8, 17)
    async with AsyncSessionLocal() as session:
        failed = await SqlPersonalWeeklyBriefStore(session).load_for_status(
            tenant_id=tenant_id,
            status=args.row_status,
            week_start=week_start,
            limit=100,
        )
        requested_names = tuple(
            value.strip()
            for value in args.owner_names.split(",")
            if value.strip()
        )
        if not requested_names and len(failed) < 6:
            raise RuntimeError("not enough failed snapshots for bounded validation")
        ordered = sorted(
            failed,
            key=lambda row: len((row.source_snapshot or {}).get("sources") or []),
        )
        all_users = tuple(
            (
                await session.scalars(
                    select(User).where(
                        User.id.in_([UUID(row.owner_user_id) for row in ordered])
                    )
                )
            ).all()
        )
        all_names = {str(user.id): user.name for user in all_users}
        requested_counts = tuple(
            int(value.strip())
            for value in args.source_counts.split(",")
            if value.strip()
        )
        if requested_names:
            selected = tuple(
                row
                for name in requested_names
                for row in ordered
                if all_names.get(row.owner_user_id) == name
            )
            if len(selected) != len(requested_names):
                raise RuntimeError("requested failed owner set is incomplete")
        elif requested_counts:
            selected_rows = []
            for requested_count in requested_counts:
                match = next(
                    (
                        row
                        for row in ordered
                        if len((row.source_snapshot or {}).get("sources") or [])
                        == requested_count
                        and row not in selected_rows
                    ),
                    None,
                )
                if match is None:
                    raise RuntimeError(
                        f"no failed snapshot with source count {requested_count}"
                    )
                selected_rows.append(match)
            selected = tuple(selected_rows)
        else:
            indexes = (
                0,
                len(ordered) // 5,
                2 * len(ordered) // 5,
                3 * len(ordered) // 5,
                4 * len(ordered) // 5,
                len(ordered) - 1,
            )
            selected = tuple(ordered[index] for index in dict.fromkeys(indexes))
        names = all_names
        await session.rollback()

    llm_client = LLMClient(settings)

    class _CaptureLLM:
        def __init__(self, inner) -> None:
            self.inner = inner
            self.calls: list[dict[str, object]] = []

        async def complete_json(self, **kwargs) -> str:
            raw = await self.inner.complete_json(**kwargs)
            system_prompt = str(kwargs.get("system_prompt") or "")
            if "关键语义复核" in system_prompt:
                phase = "critical_review"
            elif "事实复核步骤" in system_prompt:
                phase = "general_review"
            else:
                phase = "generation"
            trace: dict[str, object] = {
                "phase": phase,
                "response_length": len(str(raw or "")),
                "thinking_enabled": bool(kwargs.get("thinking_enabled")),
                "max_tokens": int(kwargs.get("max_tokens") or 0),
            }
            try:
                parsed = extract_json_object(raw)
            except (TypeError, ValueError):
                trace["json_object"] = False
            else:
                trace.update(
                    {
                        "json_object": True,
                        "top_level_keys": sorted(str(key) for key in parsed),
                        "approved_type": type(parsed.get("approved")).__name__,
                        "issues_type": type(parsed.get("issues")).__name__,
                    }
                )
                if phase == "generation":
                    trace["sections"] = {
                        section_name: {
                            "items_type": type(
                                (parsed.get(section_name) or {}).get("items")
                            ).__name__
                            if isinstance(parsed.get(section_name), dict)
                            else "missing",
                            "item_count": len(
                                (parsed.get(section_name) or {}).get("items") or []
                            )
                            if isinstance(parsed.get(section_name), dict)
                            and isinstance(
                                (parsed.get(section_name) or {}).get("items"),
                                list,
                            )
                            else -1,
                            "empty_note_present": bool(
                                str(
                                    (parsed.get(section_name) or {}).get(
                                        "empty_note"
                                    )
                                    or ""
                                ).strip()
                            )
                            if isinstance(parsed.get(section_name), dict)
                            else False,
                        }
                        for section_name in (
                            "completed",
                            "plan_progress",
                            "possible_open_loops",
                        )
                    }
            self.calls.append(trace)
            return raw

    captured_llm = _CaptureLLM(llm_client)
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(
            captured_llm,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
        ),
        reviewer=Agent2PersonalWeeklyBriefReviewer(
            captured_llm,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
        ),
        critical_reviewer=Agent2PersonalWeeklyBriefReviewer(
            captured_llm,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS,
            review_mode="critical_facts",
        ),
        max_semantic_attempts=PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS,
        review_votes=PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES,
    )
    semaphore = asyncio.Semaphore(2)

    async def verify_one(row):
        async with semaphore:
            source_count = len((row.source_snapshot or {}).get("sources") or [])
            try:
                outcome = await pipeline.generate_and_review(
                    snapshot=PersonalWeeklyBriefSnapshot.from_payload(
                        row.source_snapshot
                    ),
                    recipient_name=names[row.owner_user_id],
                    personal_memory=dict(row.personal_memory_json or {}),
                )
            except Exception as exc:
                return {
                    "source_count": source_count,
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error_category": _safe_error_category(exc),
                }
            return {
                "source_count": source_count,
                "status": "PASS",
                "semantic_attempts": outcome.semantic_attempts,
                "model_calls": outcome.model_calls,
                "message_length": len(outcome.content.message_text),
                "source_disposition_count": len(
                    outcome.content.source_dispositions
                ),
            }

    try:
        results = await asyncio.gather(*(verify_one(row) for row in selected))
    finally:
        await llm_client.close()
    payload = {
        "status": "PASS" if all(row["status"] == "PASS" for row in results) else "FAIL",
        "selected": len(selected),
        "max_concurrency": 2,
        "database_writes": 0,
        "dingtalk_sends": 0,
        "results": results,
        "call_trace": captured_llm.calls,
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
