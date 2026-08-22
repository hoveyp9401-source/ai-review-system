#!/usr/bin/env python3
"""Probe four concurrent redacted Agent2 weekly-brief owner pipelines."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

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
)
from app.agent2.tool_calling.canary_config import (
    CANARY_MAX_REQUEST_ATTEMPTS,
    CANARY_MODEL_NAME,
    CANARY_TIMEOUT_SECONDS,
)
from app.config import Settings
from app.llm.client import LLMClient
from scripts.run_agent2_personal_weekly_brief_model_eval import (
    EXPECTED_MODEL,
    _assert_complex,
    _complex_snapshot,
    _empty_snapshot,
    _read_model_only_config,
)


CONCURRENCY = 4
SCHEMA_VERSION = "agent2.personal_weekly_brief.real_concurrency_probe.v1"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("empty", "complex"), default="empty")
    parser.add_argument("--waves", type=int, default=1, choices=range(1, 20))
    return parser.parse_args()


def _settings(path: Path) -> tuple[Settings, str, tuple[str, ...]]:
    values = _read_model_only_config(path)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        llm_base_url=values.get("LLM_BASE_URL", ""),
        llm_api_key=values.get("LLM_API_KEY", ""),
        llm_model=EXPECTED_MODEL,
        llm_timeout_seconds=float(values.get("LLM_TIMEOUT_SECONDS", "60") or "60"),
        llm_max_retries=int(values.get("LLM_MAX_RETRIES", "1") or "1"),
        dingtalk_incoming_token="",
        dingtalk_callback_token="",
        dingtalk_callback_aes_key="",
        dingtalk_default_robot_webhook="",
        dingtalk_default_robot_secret="",
        dingtalk_corp_id="",
        dingtalk_agent_id="",
        dingtalk_app_key="",
        dingtalk_app_secret="",
    )
    host = urlparse(settings.llm_base_url).hostname or ""
    if (
        CANARY_MODEL_NAME != EXPECTED_MODEL
        or not settings.llm_api_key
        or not host
        or host == "api.example.invalid"
    ):
        raise AssertionError("actual Agent2 model-only configuration is incomplete")
    return settings, host, tuple(sorted(values))


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings, host, loaded_fields = _settings(args.env_file)
    client = LLMClient(settings)
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(
            client,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
        ),
        reviewer=Agent2PersonalWeeklyBriefReviewer(
            client,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
        ),
        critical_reviewer=Agent2PersonalWeeklyBriefReviewer(
            client,
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
    active = 0
    maximum_active = 0
    lock = asyncio.Lock()
    snapshot = _complex_snapshot() if args.case == "complex" else _empty_snapshot()

    async def one_owner(index: int) -> dict[str, Any]:
        nonlocal active, maximum_active
        async with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        started = perf_counter()
        try:
            outcome = await pipeline.generate_and_review(
                snapshot=snapshot,
                recipient_name=f"脱敏并发用户{index}",
                personal_memory={"entries": []},
            )
            if args.case == "complex":
                _assert_complex(outcome.content)
            return {
                "owner_index": index,
                "status": "PASS",
                "seconds": round(perf_counter() - started, 3),
                "model_calls": outcome.model_calls,
                "semantic_attempts": outcome.semantic_attempts,
            }
        except Exception as exc:
            return {
                "owner_index": index,
                "status": "FAIL",
                "seconds": round(perf_counter() - started, 3),
                "error_type": type(exc).__name__,
            }
        finally:
            async with lock:
                active -= 1

    started_at = datetime.now(UTC)
    wall_started = perf_counter()
    try:
        results = []
        for wave in range(args.waves):
            wave_results = await asyncio.gather(
                *(
                    one_owner(wave * CONCURRENCY + index)
                    for index in range(CONCURRENCY)
                )
            )
            results.extend(wave_results)
    finally:
        await client.close()
    wall_seconds = perf_counter() - wall_started
    expected_count = CONCURRENCY * args.waves
    success_count = sum(item["status"] == "PASS" for item in results)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "PASS"
            if success_count == expected_count and maximum_active == CONCURRENCY
            else "FAIL"
        ),
        "model": CANARY_MODEL_NAME,
        "case": args.case,
        "waves": args.waves,
        "expected_count": expected_count,
        "configured_concurrency": CONCURRENCY,
        "maximum_observed_active_owners": maximum_active,
        "success_count": success_count,
        "failure_count": expected_count - success_count,
        "wall_seconds": round(wall_seconds, 3),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "results": results,
        "config_attestation": {
            "base_url_host": host,
            "loaded_config_fields": loaded_fields,
            "database_accessed": False,
            "dingtalk_transport_called": False,
            "real_user_data_used": False,
        },
    }
    return payload


def main() -> int:
    args = _args()
    payload = asyncio.run(_run(args))
    _write(args.output, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "success_count": payload["success_count"],
                "failure_count": payload["failure_count"],
                "maximum_observed_active_owners": payload[
                    "maximum_observed_active_owners"
                ],
                "wall_seconds": payload["wall_seconds"],
                "output": str(args.output),
            }
        )
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
