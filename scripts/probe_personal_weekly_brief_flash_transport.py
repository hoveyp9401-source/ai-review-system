#!/usr/bin/env python3
"""Probe real Flash transport for one redacted weekly-brief complex case."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.agent2.personal_weekly_brief import (
    Agent2PersonalWeeklyBriefGenerator,
    Agent2PersonalWeeklyBriefModelPipeline,
    Agent2PersonalWeeklyBriefReviewer,
)
from app.config import Settings
from app.llm.client import LLMClient
from scripts.run_agent2_personal_weekly_brief_model_eval import (
    EXPECTED_MODEL,
    _assert_complex,
    _complex_snapshot,
    _read_model_only_config,
)


class _TokenCappedClient:
    def __init__(self, client: LLMClient, *, max_tokens: int) -> None:
        self._client = client
        self._max_tokens = max_tokens

    async def complete_json(self, **kwargs: Any) -> str:
        requested = int(kwargs.get("max_tokens") or self._max_tokens)
        kwargs["max_tokens"] = min(requested, self._max_tokens)
        return await self._client.complete_json(**kwargs)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2, choices=range(1, 6))
    parser.add_argument("--generation-max-tokens", type=int, required=True)
    parser.add_argument("--review-max-tokens", type=int, required=True)
    parser.add_argument(
        "--generation-thinking",
        choices=("enabled", "disabled"),
        required=True,
    )
    parser.add_argument(
        "--review-thinking",
        choices=("enabled", "disabled"),
        required=True,
    )
    parser.add_argument("--request-retries", type=int, default=0, choices=(0, 1))
    return parser.parse_args()


def _settings(env_file: Path) -> Settings:
    selected = _read_model_only_config(env_file)
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        llm_base_url=selected.get("LLM_BASE_URL", ""),
        llm_api_key=selected.get("LLM_API_KEY", ""),
        llm_model=EXPECTED_MODEL,
        llm_timeout_seconds=float(selected.get("LLM_TIMEOUT_SECONDS", "60") or "60"),
        llm_max_retries=int(selected.get("LLM_MAX_RETRIES", "1") or "1"),
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


def _error_payload(exc: BaseException) -> dict[str, Any]:
    chain: list[dict[str, str]] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        message = str(current)
        category = "other"
        normalized = message.casefold()
        if "server disconnected without sending a response" in normalized:
            category = "server_disconnected_before_response"
        elif "incomplete message body" in normalized or "incomplete chunked read" in normalized:
            category = "incomplete_response_body"
        elif "timed out" in normalized:
            category = "timeout"
        chain.append(
            {
                "error_type": type(current).__name__,
                "category": category,
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            }
        )
        current = current.__cause__ or current.__context__
    payload: dict[str, Any] = {"error_chain": chain}
    if isinstance(exc, AssertionError):
        payload["deterministic_oracle_reason"] = str(exc)[:500]
    return payload


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"probe output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = _settings(args.env_file)
    if not settings.llm_api_key or not settings.llm_base_url:
        raise RuntimeError("real model configuration is missing")
    base_client = LLMClient(settings)
    generator = Agent2PersonalWeeklyBriefGenerator(
        _TokenCappedClient(
            base_client,
            max_tokens=args.generation_max_tokens,
        ),
        model=EXPECTED_MODEL,
        thinking_enabled=args.generation_thinking == "enabled",
        timeout_seconds=60,
        max_retries=args.request_retries,
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        _TokenCappedClient(
            base_client,
            max_tokens=args.review_max_tokens,
        ),
        model=EXPECTED_MODEL,
        thinking_enabled=args.review_thinking == "enabled",
        timeout_seconds=60,
        max_retries=args.request_retries,
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=generator,
        reviewer=reviewer,
    )
    snapshot = _complex_snapshot()
    results: list[dict[str, Any]] = []
    try:
        for round_number in range(1, args.rounds + 1):
            started = perf_counter()
            stage = "generation"
            try:
                outcome = await pipeline.generate_and_review(
                    snapshot=snapshot,
                    recipient_name="脱敏用户",
                    personal_memory={"entries": []},
                )
                content = outcome.content
                _assert_complex(content)
                results.append(
                    {
                        "round": round_number,
                        "status": "PASS",
                        "generation_seconds": [
                            round(value, 3) for value in outcome.generation_seconds
                        ],
                        "review_seconds": [
                            round(value, 3) for value in outcome.review_seconds
                        ],
                        "total_seconds": round(perf_counter() - started, 3),
                        "review_approved": outcome.review.get("approved") is True,
                        "model_calls": outcome.model_calls,
                        "semantic_attempts": outcome.semantic_attempts,
                    }
                )
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "status": "PASS",
                            "total_seconds": results[-1]["total_seconds"],
                        }
                    ),
                    flush=True,
                )
            except Exception as exc:
                results.append(
                    {
                        "round": round_number,
                        "status": "FAIL",
                        "stage": stage,
                        "elapsed_seconds": round(perf_counter() - started, 3),
                        **_error_payload(exc),
                    }
                )
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "status": "FAIL",
                            "stage": stage,
                            "error_type": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
    finally:
        await base_client.close()
    failures = [item for item in results if item["status"] != "PASS"]
    return {
        "schema_version": "agent2.personal_weekly_brief.flash_transport_probe.v1",
        "status": "PASS" if not failures else "FAIL",
        "model": EXPECTED_MODEL,
        "rounds": args.rounds,
        "generation_max_tokens": args.generation_max_tokens,
        "review_max_tokens": args.review_max_tokens,
        "generation_thinking": args.generation_thinking,
        "review_thinking": args.review_thinking,
        "request_retries": args.request_retries,
        "database_accessed": False,
        "dingtalk_transport_called": False,
        "real_user_data_used": False,
        "results": results,
    }


def main() -> int:
    args = _args()
    payload = asyncio.run(_run(args))
    _write_private_json(args.output, payload)
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
