"""Run a pre-built zero-write model evaluation bundle on the production host.

This standalone runner imports no application module, executes no returned tool,
and never opens a business database or messaging client.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import requests
from jsonschema import Draft202012Validator


def _tool_map(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["function"]["name"]: item["function"]
        for item in request.get("tools", [])
    }


def _parse_calls(
    message: dict[str, Any],
    *,
    tools: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return [], ["tool_calls is not an array"]
    for index, raw_call in enumerate(raw_calls):
        try:
            function = raw_call["function"]
            name = function["name"]
            arguments = json.loads(function["arguments"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"tool_calls[{index}] malformed: {type(exc).__name__}")
            continue
        schema = tools.get(name)
        if schema is None:
            errors.append(f"{name}: unknown tool")
        else:
            for error in sorted(
                Draft202012Validator(schema["parameters"]).iter_errors(arguments),
                key=lambda item: list(item.absolute_path),
            ):
                path = ".".join(str(part) for part in error.absolute_path)
                errors.append(f"{name}.{path or '<root>'}: {error.message}")
        parsed.append(
            {
                "tool_call_id": raw_call.get("id"),
                "name": name,
                "arguments": arguments,
            }
        )
    return parsed, errors


def _content_is_grounded(text: str, value: Any) -> bool:
    return not isinstance(value, str) or not value or value in text


def _score_parameters(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
    parse_errors: list[str],
) -> tuple[bool, list[str]]:
    errors = list(parse_errors)
    text = case["user_text"]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        by_name.setdefault(call["name"], []).append(call["arguments"])
    for name, values in by_name.items():
        if len(values) > 1:
            errors.append(f"{name}: duplicate domain calls")

    for arguments in by_name.get("add_daily_items", []):
        fields = {item.get("field") for item in arguments.get("items", [])}
        if fields != set(case["expected_daily_fields"]):
            errors.append(
                f"add_daily_items: expected fields {case['expected_daily_fields']}, "
                f"got {sorted(fields)}"
            )
        for item in arguments.get("items", []):
            if not _content_is_grounded(text, item.get("content")):
                errors.append("add_daily_items: content is not grounded in current text")
            if item.get("source_evidence", {}).get("source_message_index") != 1:
                errors.append("add_daily_items: wrong source message index")

    for arguments in by_name.get("apply_current_weekly_report", []):
        if arguments.get("report_id") != (
            "b9bb2812-89ed-4d2c-b2c4-ed2476744963"
        ) or arguments.get("expected_version") != 3:
            errors.append("apply_current_weekly_report: wrong trusted report binding")
        fields = {
            item.get("field")
            for item in arguments.get("operations", [])
            if item.get("operation") == "append"
        }
        if fields != set(case["expected_periodic_fields"]):
            errors.append(
                "apply_current_weekly_report: expected fields "
                f"{case['expected_periodic_fields']}, got {sorted(fields)}"
            )
        for item in arguments.get("operations", []):
            value = item.get("content") or item.get("replacement")
            if not _content_is_grounded(text, value):
                errors.append(
                    "apply_current_weekly_report: content is not grounded in current text"
                )
            if item.get("source_evidence", {}).get("source_message_index") != 1:
                errors.append("apply_current_weekly_report: wrong source message index")

    for arguments in by_name.get("submit_current_weekly_report", []):
        if arguments.get("report_id") != (
            "b9bb2812-89ed-4d2c-b2c4-ed2476744963"
        ) or arguments.get("expected_version") != 3:
            errors.append("submit_current_weekly_report: wrong trusted report binding")
        if arguments.get("confirmation_evidence", {}).get(
            "source_message_index"
        ) != 1:
            errors.append("submit_current_weekly_report: wrong source message index")

    for arguments in by_name.get("apply_next_weekly_plan", []):
        if arguments.get("plan_id") != (
            "4fa86875-6f8a-477b-b324-8602010d809b"
        ) or arguments.get("expected_version") != 4:
            errors.append("apply_next_weekly_plan: wrong trusted plan binding")
        dates = {
            str(item.get("plan_date"))
            for item in arguments.get("operations", [])
            if item.get("operation") in {"add", "set_day_empty"}
        }
        if dates != set(case["expected_weekly_dates"]):
            errors.append(
                f"apply_next_weekly_plan: expected dates {case['expected_weekly_dates']}, "
                f"got {sorted(dates)}"
            )
        for item in arguments.get("operations", []):
            value = item.get("content")
            if not _content_is_grounded(text, value):
                errors.append("apply_next_weekly_plan: content is not grounded in current text")
            evidence = item.get("source_evidence", {})
            if evidence.get("source_message_index") != 1:
                errors.append("apply_next_weekly_plan: wrong source message index")
            quote = evidence.get("exact_clause_quote")
            if isinstance(quote, str) and quote not in text:
                errors.append("apply_next_weekly_plan: exact clause is not grounded")
    return not errors, errors


def _score_selection(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
    assistant_content: str | None,
) -> tuple[bool, str]:
    actual = {call["name"] for call in calls}
    expected = set(case["expected_tools"])
    if actual != expected:
        return False, f"unexpected tools: {sorted(actual)}"
    if not case["requires_clarification"]:
        return True, "matched"
    reply = (assistant_content or "").strip()
    if not reply:
        return False, "clarification case returned no question"
    if not any(token in reply for token in ("今天", "下周", "日报", "工作计划")):
        return False, "clarification did not distinguish daily versus weekly plan"
    return True, "matched"


def _load_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    _load_env(Path(args.env_file))
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if not api_key or not base_url:
        raise RuntimeError("LLM_API_KEY or LLM_BASE_URL is unavailable")
    endpoint = base_url + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    results: list[dict[str, Any]] = []
    for round_number in range(1, args.rounds + 1):
        for case in bundle["cases"]:
            started = time.perf_counter()
            response = requests.post(
                endpoint,
                headers=headers,
                json=case["request"],
                timeout=args.timeout_seconds,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            calls, parse_errors = _parse_calls(
                message,
                tools=_tool_map(case["request"]),
            )
            selection_pass, selection_reason = _score_selection(
                case, calls, message.get("content")
            )
            arguments_valid, argument_errors = _score_parameters(
                case, calls, parse_errors
            )
            overall_pass = selection_pass and arguments_valid
            result = {
                "round": round_number,
                "case_id": case["case_id"],
                "category": case["category"],
                "user_text": case["user_text"],
                "expected_tools": case["expected_tools"],
                "actual_tool_names": sorted(call["name"] for call in calls),
                "selection_pass": selection_pass,
                "selection_reason": selection_reason,
                "arguments_valid": arguments_valid,
                "argument_errors": argument_errors,
                "overall_pass": overall_pass,
                "assistant_content": message.get("content"),
                "tool_calls": calls,
                "finish_reason": choice.get("finish_reason"),
                "served_model": body.get("model"),
                "usage": body.get("usage"),
                "latency_ms": elapsed_ms,
            }
            results.append(result)
            print(
                json.dumps(
                    {
                        "round": round_number,
                        "case_id": case["case_id"],
                        "actual_tool_names": result["actual_tool_names"],
                        "selection_pass": selection_pass,
                        "arguments_valid": arguments_valid,
                        "overall_pass": overall_pass,
                        "latency_ms": elapsed_ms,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    count = len(results)
    selection_count = sum(item["selection_pass"] for item in results)
    arguments_count = sum(item["arguments_valid"] for item in results)
    overall_count = sum(item["overall_pass"] for item in results)
    summary = {
        "requested_case_runs": count,
        "valid_response_count": count,
        "transport_failure_count": 0,
        "selection_pass_count": selection_count,
        "selection_pass_rate": round(selection_count / count, 4),
        "arguments_valid_count": arguments_count,
        "arguments_valid_rate": round(arguments_count / count, 4),
        "overall_pass_count": overall_count,
        "overall_pass_rate": round(overall_count / count, 4),
        "failed_case_runs": [
            {
                "round": item["round"],
                "case_id": item["case_id"],
                "selection_reason": item["selection_reason"],
                "argument_errors": item["argument_errors"],
                "actual_tool_names": item["actual_tool_names"],
            }
            for item in results if not item["overall_pass"]
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
        "model": bundle["cases"][0]["request"]["model"],
        "base_url": base_url,
        "rounds": args.rounds,
        "system_prompt_sha256": hashlib.sha256(
            bundle["cases"][0]["request"]["messages"][0]["content"].encode("utf-8")
        ).hexdigest(),
        "prompt_audit": bundle["prompt_audit"],
        "summary": summary,
        "results": results,
    }
    Path(args.output).write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)
    return 0 if overall_count == count else 2


if __name__ == "__main__":
    raise SystemExit(main())
