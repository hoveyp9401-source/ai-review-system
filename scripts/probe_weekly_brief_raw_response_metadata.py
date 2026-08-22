#!/usr/bin/env python3
"""Inspect safe response metadata for the redacted weekly-brief generation call."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.agent2.personal_weekly_brief import _SYSTEM_PROMPT
from app.config import Settings
from app.llm.client import LLMClient
from scripts.run_agent2_personal_weekly_brief_model_eval import (
    EXPECTED_MODEL,
    _complex_snapshot,
    _read_model_only_config,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2, choices=range(1, 4))
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--thinking", choices=("enabled", "disabled"), required=True)
    return parser.parse_args()


def _settings(path: Path) -> Settings:
    selected = _read_model_only_config(path)
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        llm_base_url=selected.get("LLM_BASE_URL", ""),
        llm_api_key=selected.get("LLM_API_KEY", ""),
        llm_model=EXPECTED_MODEL,
        llm_timeout_seconds=60,
        llm_max_retries=0,
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


def _write_private(path: Path, payload: dict) -> None:
    if path.exists():
        raise FileExistsError(path)
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
    finally:
        if temporary.exists():
            temporary.unlink()


async def _run(args: argparse.Namespace) -> dict:
    client = LLMClient(_settings(args.env_file))
    snapshot = _complex_snapshot()
    user_payload = {
        "task": "生成个人本周工作简报",
        "recipient": {
            "authenticated_display_name": "脱敏用户",
            "personal_memory": {"entries": []},
        },
        "trusted_snapshot": snapshot.as_payload(),
    }
    request_payload = {
        "model": EXPECTED_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "thinking": {"type": args.thinking},
        "max_tokens": args.max_tokens,
    }
    results = []
    try:
        for round_number in range(1, args.rounds + 1):
            started = perf_counter()
            try:
                response = await client.native_http_client.post(
                    "/chat/completions",
                    json=request_payload,
                    timeout=60,
                )
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                reasoning = message.get("reasoning_content") or ""
                usage = data.get("usage") or {}
                result = {
                    "round": round_number,
                    "status": "PASS",
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "finish_reason": choice.get("finish_reason"),
                    "content_length": len(content),
                    "reasoning_length": len(reasoning),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
            except Exception as exc:
                result = {
                    "round": round_number,
                    "status": "FAIL",
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "error_type": type(exc).__name__,
                }
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        await client.close()
    return {
        "schema_version": "agent2.personal_weekly_brief.raw_metadata_probe.v1",
        "model": EXPECTED_MODEL,
        "max_tokens": args.max_tokens,
        "thinking": args.thinking,
        "database_accessed": False,
        "dingtalk_transport_called": False,
        "real_user_data_used": False,
        "results": results,
    }


def main() -> int:
    args = _args()
    payload = asyncio.run(_run(args))
    _write_private(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
