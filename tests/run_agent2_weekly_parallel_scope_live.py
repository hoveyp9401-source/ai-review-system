"""Zero-write live red loop for parallel weekly-plan items sharing Monday."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
)
from tests.run_agent2_tri_domain_full_adapter_matrix_live import (
    CASES,
    _BinderZeroWriteRuntime,
    _calls_from_runtime,
    _context,
    _raw_calls,
    _score,
)


_CASE_ID = "plan_parallel_items_share_monday_scope"
_ALLOWED_TOOLS = frozenset(
    {"add_daily_items", "apply_next_weekly_plan"}
)


async def _run() -> int:
    case = next(item for item in CASES if item.case_id == _CASE_ID)
    context = _context(case, 1).model_copy(
        update={
            "allowed_tool_names": _ALLOWED_TOOLS,
            "gate_decisions": {name: True for name in _ALLOWED_TOOLS},
        }
    )
    runtime = _BinderZeroWriteRuntime(context, case.user_text)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("missing credential in DEEPSEEK_API_KEY")
    base_url = (
        os.environ.get("DEEPSEEK_API_BASE")
        or os.environ.get("LLM_BASE_URL")
        or "https://api.deepseek.com/v1"
    ).rstrip("/")
    endpoint = base_url + "/chat/completions"
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("model endpoint must be absolute HTTP(S)")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(90.0),
    ) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=CANARY_MODEL_NAME,
            timeout_seconds=90.0,
            max_tool_loops=4,
            max_request_attempts=2,
            endpoint=endpoint,
        )
        try:
            result = await adapter.run_canary_turn(
                system_prompt=canary_system_prompt(
                    allowed_tool_names=_ALLOWED_TOOLS
                ),
                user_text=case.user_text,
                context=context,
                runtime_session=runtime,
                thinking_enabled=True,
            )
        except DeepSeekToolCallingError as exc:
            runtime.assert_zero_side_effects()
            print(
                json.dumps(
                    {
                        "case_id": _CASE_ID,
                        "pass": False,
                        "failure_kind": "adapter_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "model_turn_count": len(exc.model_turns),
                        "zero_business_writes": True,
                        "messages_sent": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2

    runtime.assert_zero_side_effects()
    initial_calls = _raw_calls(result.model_turns[0])
    final_calls = _calls_from_runtime(runtime)
    passed, errors = _score(
        case,
        calls=final_calls,
        assistant_content=result.final_content,
    )
    review_count = sum(
        bool(
            turn.response_metadata.get(
                "daily_weekly_write_semantic_review"
            )
        )
        for turn in result.model_turns
    )
    passed = bool(passed and review_count >= 1)
    if review_count < 1:
        errors = (*errors, "adapter semantic review did not run")
    print(
        json.dumps(
            {
                "case_id": _CASE_ID,
                "user_text": case.user_text,
                "expected": [
                    {"plan_date": date_value, "content": content}
                    for date_value, content in case.expected_plan_additions
                ],
                "initial_calls": initial_calls,
                "model_turns": [
                    {
                        "semantic_review": bool(
                            turn.response_metadata.get(
                                "daily_weekly_write_semantic_review"
                            )
                        ),
                        "calls": _raw_calls(turn),
                    }
                    for turn in result.model_turns
                ],
                "final_calls": final_calls,
                "final_content": result.final_content,
                "receipts": [
                    {
                        "tool_name": receipt.tool_name,
                        "status": receipt.status.value,
                        "error_code": receipt.error_code,
                        "changed": receipt.changed,
                    }
                    for receipt in result.receipts
                ],
                "semantic_review_count": review_count,
                "pass": passed,
                "errors": list(errors),
                "zero_business_writes": True,
                "messages_sent": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
