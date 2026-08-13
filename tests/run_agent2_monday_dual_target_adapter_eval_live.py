"""Targeted full-Adapter evaluation for Monday's two Weekly Work Plans.

This script deliberately reuses the strict matrix harness's trusted context and
ephemeral no-op runtime.  It never imports a production executor, database
session, receipt store, or messaging provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.agent2.tool_calling.deepseek_adapter import DeepSeekToolCallingAdapter
from tests.run_agent2_tri_domain_full_adapter_matrix_live import (
    _MONDAY_CURRENT_PLAN_ID,
    _MONDAY_CURRENT_VERSION,
    _MONDAY_NEXT_PLAN_ID,
    _MONDAY_NEXT_VERSION,
    MatrixCase,
    _binding,
    _evaluate_one,
)

_CASES = (
    MatrixCase(
        "monday_bare_weekday_ambiguous",
        "monday_dual_target",
        "周三整理证据",
        frozenset(),
        context_variant="monday_dual",
        response_kind="clarification",
        note="A bare weekday must not choose between current-week late fill and natural next week.",
    ),
    MatrixCase(
        "monday_explicit_current_week",
        "monday_dual_target",
        "本周三整理证据",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                _MONDAY_CURRENT_PLAN_ID,
                _MONDAY_CURRENT_VERSION,
                ("2026-08-19",),
            ),
        ),
    ),
    MatrixCase(
        "monday_explicit_current_week_late_fill",
        "monday_dual_target",
        "补本周周三整理证据",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                _MONDAY_CURRENT_PLAN_ID,
                _MONDAY_CURRENT_VERSION,
                ("2026-08-19",),
            ),
        ),
    ),
    MatrixCase(
        "monday_explicit_next_week",
        "monday_dual_target",
        "下周三整理证据",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                _MONDAY_NEXT_PLAN_ID,
                _MONDAY_NEXT_VERSION,
                ("2026-08-26",),
            ),
        ),
    ),
    MatrixCase(
        "monday_explicit_next_week_doubled_week",
        "monday_dual_target",
        "下周周三整理证据",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                _MONDAY_NEXT_PLAN_ID,
                _MONDAY_NEXT_VERSION,
                ("2026-08-26",),
            ),
        ),
    ),
)


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    schedule = ((_CASES[0], 1), (_CASES[0], 2), *(
        (case, 1) for case in _CASES[1:]
    ))
    results: list[dict[str, object]] = []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(args.timeout_seconds),
    ) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=args.max_tool_loops,
            max_request_attempts=args.max_attempts,
            endpoint=endpoint,
        )
        for case, round_number in schedule:
            result = await _evaluate_one(
                adapter=adapter,
                case=case,
                round_number=round_number,
            )
            results.append(result)
            print(
                json.dumps(
                    {
                        "case_id": case.case_id,
                        "round": round_number,
                        "initial_calls": result.get("initial_calls", ()),
                        "semantic_review_count": result.get("semantic_review_count", 0),
                        "review_changed_draft": result.get("review_changed_draft", False),
                        "final_calls": result.get("final_calls", ()),
                        "final_content": result.get("final_content"),
                        "overall_pass": result.get("overall_pass", False),
                        "failure_reason": result.get("failure_reason") or result.get("error"),
                        "zero_business_writes": result.get("zero_business_writes"),
                        "production_handlers_called": result.get("production_handlers_called"),
                        "messages_sent": result.get("messages_sent"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )

    passed = sum(bool(item.get("overall_pass")) for item in results)
    artifact = {
        "schema_version": "agent2.monday-dual-target.full-adapter-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "adapter_path": "DeepSeekToolCallingAdapter.run_canary_turn",
        "provider_endpoint_origin_sha256": hashlib.sha256(
            f"{parts.scheme}://{parts.netloc}".encode()
        ).hexdigest(),
        "runtime": {
            "kind": "ephemeral_in_memory_no_op_recorder",
            "production_handlers_called": False,
            "business_database_connected": False,
            "business_data_written": False,
            "receipt_store_written": False,
            "messages_sent": False,
        },
        "summary": {
            "requested_case_runs": len(results),
            "passed_case_runs": passed,
            "failed_case_runs": len(results) - passed,
            "zero_business_write_assertions_passed": all(
                item.get("zero_business_writes") is True for item in results
            ),
            "production_handlers_called": False,
            "messages_sent": False,
        },
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {"output": str(output), "artifact_sha256": digest, "summary": artifact["summary"]},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if passed == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Targeted isolated Monday dual-week full-Adapter evaluation"
    )
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-tool-loops", type=int, default=4)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_API_BASE")
        or os.environ.get("LLM_BASE_URL")
        or "https://api.deepseek.com/v1",
    )
    parser.add_argument("--model", default=CANARY_MODEL_NAME)
    parser.add_argument(
        "--output",
        default="artifacts/monday_dual_target_full_adapter_directed.json",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
