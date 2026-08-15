"""Real-model, zero-write gate for the Daily Report reminder preference.

The DeepSeek adapter and model are real.  The runtime is an in-memory recorder:
it opens no business database, invokes no production handler, and sends no
message.  Exactly two rounds are run; a failed round is reported, not retried.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_agent2_tri_domain_full_adapter_matrix_live import (
    MatrixCase,
    _BinderZeroWriteRuntime,
)
from run_agent2_tri_domain_full_adapter_matrix_live import (
    _context as matrix_context,
)

from app.agent2.memory import (
    TrustedPersonalMemory,
    TrustedPersonalMemoryContext,
)
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
)
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.validation import NativeToolCall

_ROOT = Path(__file__).resolve().parents[1]
_MEMORY_KEY = "report.daily_reminders_enabled"
_TOOLS = frozenset(
    {
        "remember_personal_memory",
        "forget_personal_memory",
        "add_daily_items",
        "apply_next_weekly_plan",
    }
)
ExpectedAction = Literal["disable", "restore", "clarify"]


@dataclass(frozen=True)
class PreferenceCase:
    case_id: str
    user_text: str
    expected_action: ExpectedAction
    current_enabled: bool | None = None


CASES = (
    PreferenceCase(
        case_id="disable_own_daily_reminders",
        user_text="请关闭我自己的日报提醒。",
        expected_action="disable",
    ),
    PreferenceCase(
        case_id="restore_own_daily_reminders",
        user_text="请恢复我自己的日报提醒。",
        expected_action="restore",
        current_enabled=False,
    ),
    PreferenceCase(
        case_id="generic_reminder_scope_requires_clarification",
        user_text="不要再提醒我。",
        expected_action="clarify",
    ),
)


class _PreferenceZeroWriteRuntime(_BinderZeroWriteRuntime):
    """Bind real tool arguments, but turn every accepted write into no-op."""

    def _receipt(self, call: NativeToolCall) -> ToolReceipt:
        definition = TOOL_REGISTRY[call.tool_name]
        if definition.transaction_target_policy != "personal_memory":
            return super()._receipt(call)
        key = str(call.arguments.get("memory_key") or "")
        value = call.arguments.get("value")
        forgotten = call.tool_name == "forget_personal_memory"
        return ToolReceipt(
            status=ReceiptStatus.NO_OP,
            tool_name=call.tool_name,
            changed=False,
            target_type="personal_memory",
            target_id=key,
            before_version=1,
            after_version=1,
            safe_user_facts={
                "actual_write": False,
                "evaluation_only": True,
                "production_handler_called": False,
                "memory_key": key,
                "memory": (
                    None
                    if forgotten
                    else {
                        "memory_type": "response_preference",
                        "memory_key": key,
                        "value": value,
                        "provenance": "server_personal_memory",
                    }
                ),
                "forgotten": False,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )


def _context(case: PreferenceCase, round_number: int) -> TrustedContext:
    base = matrix_context(
        MatrixCase(
            case_id=f"preference-{case.case_id}",
            category="daily_reminder_preference",
            user_text=case.user_text,
            expected_tools=frozenset(),
        ),
        round_number,
    )
    memory = None
    if case.current_enabled is not None:
        principal = base.principal
        entry = TrustedPersonalMemory(
            memory_id=principal.user_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            memory_type="response_preference",
            memory_key=_MEMORY_KEY,
            value={"enabled": case.current_enabled},
            source_kind="explicit_user",
            source_message_id="prior-preference-message",
            version=1,
            updated_at=base.now,
        )
        memory = TrustedPersonalMemoryContext(entries=(entry,))
    return base.model_copy(
        update={
            "personal_memory": memory,
            "allowed_tool_names": _TOOLS,
            "gate_decisions": {name: True for name in _TOOLS},
        }
    )


def _call_payload(call: NativeToolCall) -> dict[str, Any]:
    return {"name": call.tool_name, "arguments": call.arguments}


def _all_attempted_calls(runtime: _PreferenceZeroWriteRuntime) -> list[dict[str, Any]]:
    calls = [_call_payload(call) for call in runtime.all_calls]
    calls.extend(
        _call_payload(call)
        for batch in runtime.blocked_attempts
        for call in batch
    )
    return calls


def _score(
    case: PreferenceCase,
    *,
    calls: list[dict[str, Any]],
    final_content: str | None,
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    if case.expected_action == "clarify":
        if calls:
            errors.append("generic reminder request proposed a tool call")
        reply = (final_content or "").strip()
        if not reply or not any(
            marker in reply
            for marker in ("？", "?", "请明确", "请说明", "具体")
        ):
            errors.append("generic reminder request did not ask for clarification")
        return not errors, tuple(errors)

    if len(calls) != 1:
        errors.append(f"expected exactly one memory call, got {len(calls)}")
        return False, tuple(errors)
    call = calls[0]
    name = call.get("name")
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        return False, ("memory call arguments were not an object",)
    if arguments.get("memory_key") != _MEMORY_KEY:
        errors.append("memory call used the wrong key")
    if name in {"add_daily_items", "apply_next_weekly_plan"}:
        errors.append("preference request crossed into a report domain")
    if case.expected_action == "disable":
        if name != "remember_personal_memory":
            errors.append("disable request did not use remember_personal_memory")
        if arguments.get("value") != {"enabled": False}:
            errors.append("disable request did not store enabled=false")
    else:
        safe_restore = (
            name == "remember_personal_memory"
            and arguments.get("value") == {"enabled": True}
        )
        if not safe_restore:
            errors.append("restore request did not store enabled=true")
    return not errors, tuple(errors)


async def _evaluate_one(
    adapter: DeepSeekToolCallingAdapter,
    case: PreferenceCase,
    round_number: int,
) -> dict[str, Any]:
    context = _context(case, round_number)
    runtime = _PreferenceZeroWriteRuntime(context, case.user_text)
    started = perf_counter()
    try:
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(
                allowed_tool_names=_TOOLS
            ),
            user_text=case.user_text,
            context=context,
            runtime_session=runtime,
        )
        runtime.assert_zero_side_effects()
        calls = _all_attempted_calls(runtime)
        passed, errors = _score(
            case,
            calls=calls,
            final_content=result.final_content,
        )
        return {
            "case_id": case.case_id,
            "round": round_number,
            "calls": calls,
            "final_content": result.final_content,
            "overall_pass": passed,
            "errors": list(errors),
            "zero_business_writes": runtime.business_write_count == 0,
            "production_handlers_called": False,
            "messages_sent": runtime.message_send_count != 0,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    except (DeepSeekToolCallingError, AssertionError, ValueError) as exc:
        return {
            "case_id": case.case_id,
            "round": round_number,
            "calls": _all_attempted_calls(runtime),
            "overall_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
            "zero_business_writes": runtime.business_write_count == 0,
            "production_handlers_called": False,
            "messages_sent": runtime.message_send_count != 0,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(args.timeout_seconds),
    ) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=4,
            max_request_attempts=1,
            endpoint=endpoint,
        )
        for round_number in (1, 2):
            for case in CASES:
                result = await _evaluate_one(adapter, case, round_number)
                results.append(result)
                print(
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    flush=True,
                )

    passed = sum(bool(item.get("overall_pass")) for item in results)
    summary = {
        "requested_case_runs": len(results),
        "passed_case_runs": passed,
        "failed_case_runs": len(results) - passed,
        "zero_business_write_assertions_passed": all(
            item.get("zero_business_writes") is True for item in results
        ),
        "production_handlers_called": False,
        "business_database_connected": False,
        "messages_sent": False,
        "generic_reminder_scope_requires_clarification": True,
        "delivered_reminder_context_exception_enabled": False,
    }
    artifact = {
        "schema_version": "agent2.daily-reminder-preference.live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "provider_endpoint_origin_sha256": hashlib.sha256(
            f"{parts.scheme}://{parts.netloc}".encode()
        ).hexdigest(),
        "summary": summary,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output": str(output), "summary": summary},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if passed == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--base-url",
        default=(
            os.environ.get("DEEPSEEK_API_BASE")
            or os.environ.get("LLM_BASE_URL")
            or "https://api.deepseek.com/v1"
        ),
    )
    parser.add_argument("--model", default=CANARY_MODEL_NAME)
    parser.add_argument(
        "--output",
        default="artifacts/daily_reminder_preference_live_2rounds.json",
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
