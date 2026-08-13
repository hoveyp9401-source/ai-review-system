from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.registry import (
    ToolArgumentsValidationError,
    UnknownToolError,
    deepseek_tool_schemas,
    validate_tool_arguments,
)
from tests.test_agent2_tri_domain_model_eval import (
    MODEL_CASES,
    TRI_DOMAIN_TOOL_NAMES,
    request_payload,
    score_parameters,
    score_selection,
)


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_tool_calls(message: Any) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    if not isinstance(message, dict):
        return [], ["assistant message is not an object"]
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return [], ["tool_calls is not an array"]
    parsed: list[dict[str, Any]] = []
    for index, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            errors.append(f"tool_calls[{index}] is not an object")
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            errors.append(f"tool_calls[{index}].function is not an object")
            continue
        name = function.get("name")
        raw_arguments = function.get("arguments")
        if not isinstance(name, str) or not name:
            errors.append(f"tool_calls[{index}].function.name is invalid")
            continue
        if not isinstance(raw_arguments, str):
            errors.append(f"tool_calls[{index}].function.arguments is not a string")
            continue
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            errors.append(f"{name}: arguments are invalid JSON: {exc.msg}")
            continue
        try:
            validated = validate_tool_arguments(name, arguments)
        except (ToolArgumentsValidationError, UnknownToolError) as exc:
            errors.append(f"{name}: schema validation failed: {exc}")
            validated = arguments
        parsed.append(
            {
                "tool_call_id": call.get("id"),
                "name": name,
                "arguments": validated,
                "raw_arguments": raw_arguments,
            }
        )
    return parsed, errors


async def _one_case(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    case: Any,
    round_number: int,
    max_attempts: int,
) -> dict[str, Any]:
    started = perf_counter()
    transport_errors: list[str] = []
    response: httpx.Response | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(endpoint, json=request_payload(case))
            response.raise_for_status()
            break
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            transport_errors.append(
                f"attempt {attempt}: {type(exc).__name__}: {exc}"
            )
            if attempt == max_attempts:
                response = None
        except httpx.HTTPStatusError as exc:
            transport_errors.append(
                f"attempt {attempt}: HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
            response = None
            break
    elapsed_ms = round((perf_counter() - started) * 1000, 1)
    base = {
        "round": round_number,
        "case_id": case.case_id,
        "category": case.category,
        "user_text": case.user_text,
        "expected_tools": sorted(case.expected_tools),
        "latency_ms": elapsed_ms,
        "transport_errors": transport_errors,
    }
    if response is None:
        return {
            **base,
            "valid_response": False,
            "selection_pass": False,
            "arguments_valid": False,
            "overall_pass": False,
            "failure_reason": "no valid provider response",
        }
    try:
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return {
            **base,
            "valid_response": False,
            "selection_pass": False,
            "arguments_valid": False,
            "overall_pass": False,
            "failure_reason": f"malformed provider response: {type(exc).__name__}",
        }
    tool_calls, parse_errors = _parse_tool_calls(message)
    selection_pass, selection_reason = score_selection(
        case,
        tool_calls=tool_calls,
        assistant_content=message.get("content"),
    )
    try:
        semantic_parameters_valid, semantic_errors = score_parameters(
            case,
            tool_calls=tool_calls,
        )
    except CurrentTurnSourceEvidenceError as exc:
        semantic_parameters_valid = False
        semantic_errors = (f"{type(exc).__name__}: {exc}",)
    argument_errors = tuple(parse_errors) + tuple(semantic_errors)
    arguments_valid = not argument_errors and semantic_parameters_valid
    overall_pass = selection_pass and arguments_valid
    return {
        **base,
        "valid_response": True,
        "served_model": body.get("model"),
        "response_id": body.get("id"),
        "finish_reason": choice.get("finish_reason"),
        "usage": body.get("usage"),
        "assistant_content": message.get("content"),
        "tool_calls": tool_calls,
        "actual_tool_names": sorted(call["name"] for call in tool_calls),
        "selection_pass": selection_pass,
        "selection_reason": selection_reason,
        "arguments_valid": arguments_valid,
        "argument_errors": list(argument_errors),
        "overall_pass": overall_pass,
        "failure_reason": None if overall_pass else (
            "; ".join(argument_errors)
            if selection_pass
            else selection_reason
        ),
    }


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    results: list[dict[str, Any]] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(args.timeout_seconds)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        for round_number in range(1, args.rounds + 1):
            for case in MODEL_CASES:
                result = await _one_case(
                    client,
                    endpoint=endpoint,
                    case=case,
                    round_number=round_number,
                    max_attempts=args.max_attempts,
                )
                results.append(result)
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "case_id": case.case_id,
                            "actual_tool_names": result.get(
                                "actual_tool_names", []
                            ),
                            "selection_pass": result["selection_pass"],
                            "arguments_valid": result["arguments_valid"],
                            "overall_pass": result["overall_pass"],
                            "failure_reason": result.get("failure_reason"),
                            "latency_ms": result["latency_ms"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
    valid = [item for item in results if item["valid_response"]]
    summary = {
        "requested_case_runs": len(results),
        "valid_response_count": len(valid),
        "transport_failure_count": len(results) - len(valid),
        "selection_pass_count": sum(item["selection_pass"] for item in valid),
        "arguments_valid_count": sum(item["arguments_valid"] for item in valid),
        "overall_pass_count": sum(item["overall_pass"] for item in valid),
        "selection_pass_rate": (
            round(sum(item["selection_pass"] for item in valid) / len(valid), 4)
            if valid else None
        ),
        "arguments_valid_rate": (
            round(sum(item["arguments_valid"] for item in valid) / len(valid), 4)
            if valid else None
        ),
        "overall_pass_rate": (
            round(sum(item["overall_pass"] for item in valid) / len(valid), 4)
            if valid else None
        ),
        "failed_case_runs": [
            {
                "round": item["round"],
                "case_id": item["case_id"],
                "failure_reason": item.get("failure_reason"),
                "actual_tool_names": item.get("actual_tool_names", []),
                "argument_errors": item.get("argument_errors", []),
            }
            for item in results
            if not item["overall_pass"]
        ],
    }
    artifact = {
        "schema_version": "agent2.tri-domain.live-eval.v1",
        "zero_write": True,
        "model_layer_only": True,
        "tools_executed": False,
        "business_database_connected": False,
        "messages_sent": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": request_payload(MODEL_CASES[0])["model"],
        "base_url": args.base_url,
        "rounds": args.rounds,
        "system_prompt_sha256": hashlib.sha256(
            canary_system_prompt().encode("utf-8")
        ).hexdigest(),
        "tool_schemas_sha256": _sha256_json(
            deepseek_tool_schemas(TRI_DOMAIN_TOOL_NAMES)
        ),
        "summary": summary,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), "summary": summary}, ensure_ascii=False))
    return 0 if summary["overall_pass_count"] == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--api-key-env", default="LLM_API_KEY")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1",
    )
    parser.add_argument(
        "--output",
        default="artifacts/tri_domain_model_eval_2rounds.json",
    )
    args = parser.parse_args()
    if args.rounds < 1 or args.max_attempts < 1:
        parser.error("rounds and max-attempts must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
