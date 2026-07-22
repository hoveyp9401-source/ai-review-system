from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid5


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent2.cognitive_core_v3 import CognitiveCoreV3
from app.agent2.command_planner_v3 import CognitiveCommandPlanner
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.runtime import (
    Agent2RuntimeHarness,
    InMemoryDailyDomainExecutor,
    InMemoryRuntimeAuditSink,
    MvpContextAssembler,
    RuntimeActor,
    RuntimeTurnRequest,
    build_phase1_domain_registry,
)
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.config import get_settings
from app.llm.client import LLMClient


CASES = (
    {
        "case_id": "active-daily-plain-work",
        "setup": ("进入日报",),
        "text": "半年度绩效评估",
        "expected": "append_today_work",
    },
    {
        "case_id": "active-daily-section-correction",
        "setup": ("进入日报", "梳理推进待办，并梳理参赛作品"),
        "text": "梳理推进待办，并梳理参赛作品，这是明天的计划",
        "expected": "move_to_tomorrow_plan",
    },
    {
        "case_id": "active-daily-explicit-question",
        "setup": ("进入日报",),
        "text": "半年度绩效评估什么时候提交？",
        "expected": "no_daily_write_internal_query",
    },
    {
        "case_id": "active-daily-case-query",
        "setup": ("进入日报",),
        "text": "帮我查一下某案件的进展",
        "expected": "no_daily_write_case_query",
    },
    {
        "case_id": "active-daily-travel-switch",
        "setup": ("进入日报",),
        "text": "明天去上海出差",
        "expected": "travel_event_with_required_tomorrow_plan",
    },
    {
        "case_id": "no-active-daily-future-travel-projection",
        "setup": (),
        "text": "明天去上海出差",
        "expected": "travel_event_with_required_tomorrow_plan",
    },
    {
        "case_id": "active-daily-compound-correction",
        "setup": ("进入日报", "梳理推进待办，并梳理参赛作品"),
        "text": (
            "梳理推进待办，并梳理参赛作品，这是明天的计划。"
            "问题是来函界面调整后降低了效率。"
        ),
        "expected": "move_and_record_problem",
    },
)


class RecordingClient:
    def __init__(self, delegate: LLMClient) -> None:
        self.delegate = delegate
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, **kwargs: Any) -> str:
        started = time.perf_counter()
        call = {
            "system_prompt_sha256": _sha256(str(kwargs.get("system_prompt") or "")),
            "user_prompt_sha256": _sha256(str(kwargs.get("user_prompt") or "")),
            "model": kwargs.get("model"),
            "thinking_enabled": kwargs.get("thinking_enabled"),
        }
        try:
            output = await self.delegate.complete_json(**kwargs)
        except Exception as exc:
            call.update(
                {
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            self.calls.append(call)
            raise
        call.update(
            {
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "raw_output": output,
            }
        )
        self.calls.append(call)
        return output


async def run(
    *,
    repetitions: int,
    output_path: Path,
    case_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    settings = get_settings()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or settings.llm_api_key
    if not api_key:
        raise RuntimeError("no preconfigured offline model credential")
    model_settings = settings.model_copy(
        update={
            "llm_api_key": api_key,
            "llm_model": settings.agent2_cognitive_core_v3_model,
        }
    )
    client = LLMClient(model_settings)
    selected_cases = tuple(
        case for case in CASES if not case_ids or case["case_id"] in set(case_ids)
    )
    unknown_case_ids = set(case_ids) - {str(case["case_id"]) for case in CASES}
    if unknown_case_ids:
        raise ValueError(f"unknown case ids: {sorted(unknown_case_ids)}")
    results: list[dict[str, Any]] = []
    try:
        for repetition in range(1, repetitions + 1):
            for case in selected_cases:
                results.append(
                    await _run_case(
                        client,
                        model=model_settings.agent2_cognitive_core_v3_model,
                        thinking=model_settings.agent2_cognitive_core_v3_thinking,
                        case=case,
                        repetition=repetition,
                    )
                )
    finally:
        await client.close()
    payload = {
        "schema_version": "agent2-daily-context-real-model-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "offline_in_memory_no_business_side_effects",
        "model": model_settings.agent2_cognitive_core_v3_model,
        "thinking_enabled": model_settings.agent2_cognitive_core_v3_thinking,
        "temperature": 0,
        "client_timeout_seconds": model_settings.llm_timeout_seconds,
        "client_max_retries": model_settings.llm_max_retries,
        "semantic_schema_attempts": 3,
        "repetitions": repetitions,
        "case_count": len(selected_cases),
        "turn_run_count": len(results),
        "model_call_count": sum(len(item["model_calls"]) for item in results),
        "external_business_side_effects": 0,
        "successful_runs": sum(item["status"] == "completed" for item in results),
        "failed_runs": sum(item["status"] != "completed" for item in results),
        "acceptance_passed": sum(item["acceptance"] == "PASS" for item in results),
        "acceptance_failed": sum(item["acceptance"] == "FAIL" for item in results),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


async def _run_case(
    client: LLMClient,
    *,
    model: str,
    thinking: bool,
    case: dict[str, Any],
    repetition: int,
) -> dict[str, Any]:
    actor_id = uuid5(NAMESPACE_URL, f"real-model-release:{case['case_id']}:{repetition}")
    daily = InMemoryDailyDomainExecutor(
        DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, f"real-model-release-report:{case['case_id']}:{repetition}"),
            owner_user_id=actor_id,
            version=0,
            status="collecting",
        )
    )
    recorder = RecordingClient(client)
    interpreter = LLMCognitiveSemanticInterpreter(
        recorder,
        model=model,
        thinking_enabled=thinking,
    )
    runtime = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(interpreter),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=InMemoryConversationStateStore(),
            daily_snapshot_provider=daily,
            daily_policy={"current_report_date": "2026-07-21"},
        ),
        domains=build_phase1_domain_registry(daily_executor=daily),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    conversation_id = f"real-model-release:{case['case_id']}:{repetition}"
    base_time = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
    for index, setup_text in enumerate(case["setup"], start=1):
        await runtime.handle(
            _request(
                actor_id,
                conversation_id,
                f"{case['case_id']}-setup-{repetition}-{index}",
                setup_text,
                base_time,
            )
        )
    recorder.calls.clear()
    before = daily.snapshot
    message_id = f"{case['case_id']}-target-{repetition}"
    started = time.perf_counter()
    try:
        outcome = await runtime.handle(
            _request(
                actor_id,
                conversation_id,
                message_id,
                str(case["text"]),
                base_time,
            )
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        after = daily.snapshot
        acceptance = _acceptance(
            str(case["expected"]),
            before,
            after,
            decision=(outcome.decision.as_dict() if outcome.decision else None),
        )
        return {
            "case_id": case["case_id"],
            "repetition": repetition,
            "input": case["text"],
            "expected": case["expected"],
            "status": "completed",
            "elapsed_ms": elapsed_ms,
            "semantic_source": interpreter.source_for(message_id),
            "model_calls": list(recorder.calls),
            "semantic_decision": outcome.decision.as_dict() if outcome.decision else None,
            "typed_command": outcome.command_plan.as_dict() if outcome.command_plan else None,
            "before_snapshot": _snapshot(before),
            "after_snapshot": _snapshot(after),
            "clarification": (
                asdict(outcome.decision.clarification_need)
                if outcome.decision and outcome.decision.clarification_need
                else None
            ),
            "user_reply": asdict(outcome.reply),
            "safe_write": _safe_write(str(case["expected"]), before, after),
            "acceptance": acceptance,
        }
    except Exception as exc:
        return {
            "case_id": case["case_id"],
            "repetition": repetition,
            "input": case["text"],
            "expected": case["expected"],
            "status": "error",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "semantic_source": interpreter.source_for(message_id),
            "model_calls": list(recorder.calls),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "before_snapshot": _snapshot(before),
            "after_snapshot": _snapshot(daily.snapshot),
            "safe_write": daily.snapshot == before,
            "acceptance": "FAIL",
        }


def _request(actor_id, conversation_id: str, message_id: str, text: str, occurred_at: datetime):
    return RuntimeTurnRequest(
        tenant_id="tenant-offline-real-model",
        actor=RuntimeActor(actor_id=actor_id, display_name="reviewer-alias"),
        conversation_id=conversation_id,
        message_id=message_id,
        text=text,
        occurred_at=occurred_at,
        channel="offline_real_model_acceptance",
        request_metadata={"source": "daily_context_release_candidate"},
    )


def _snapshot(snapshot: DailyReportMutationSnapshot) -> dict[str, Any]:
    return {
        "report_id": str(snapshot.report_id),
        "owner_user_id": str(snapshot.owner_user_id),
        "version": snapshot.version,
        "status": snapshot.status,
        "today_work": list(snapshot.today_work),
        "problems": list(snapshot.problems),
        "tomorrow_plan": list(snapshot.tomorrow_plan),
        "item_ids": {key: list(value) for key, value in snapshot.item_ids.items()},
    }


def _acceptance(
    expected: str,
    before: DailyReportMutationSnapshot,
    after: DailyReportMutationSnapshot,
    *,
    decision: dict[str, Any] | None,
) -> str:
    if expected == "append_today_work":
        passed = len(after.today_work) == len(before.today_work) + 1
    elif expected == "move_to_tomorrow_plan":
        passed = len(after.today_work) + 1 == len(before.today_work) and len(after.tomorrow_plan) == len(before.tomorrow_plan) + 1
    elif expected == "move_and_record_problem":
        passed = (
            len(after.today_work) + 1 == len(before.today_work)
            and len(after.tomorrow_plan) == len(before.tomorrow_plan) + 1
            and len(after.problems) == len(before.problems) + 1
        )
    elif expected.startswith("no_daily_write_"):
        intents = [str(value) for value in (decision or {}).get("intents", [])]
        if expected.endswith("internal_query"):
            recognized = any(value in {"internal_query", "knowledge_query"} for value in intents)
        elif expected.endswith("case_query"):
            recognized = any("case" in value and "query" in value for value in intents)
        else:
            recognized = any("travel" in value for value in intents)
        passed = after == before and recognized
    elif expected == "travel_event_with_required_tomorrow_plan":
        intents = [str(value) for value in (decision or {}).get("intents", [])]
        recognized = any("travel" in value for value in intents)
        passed = (
            recognized
            and after.today_work == before.today_work
            and after.problems == before.problems
            and len(after.tomorrow_plan) == len(before.tomorrow_plan) + 1
            and after.version == before.version + 1
        )
    else:
        passed = False
    return "PASS" if passed else "FAIL"


def _safe_write(expected: str, before: DailyReportMutationSnapshot, after: DailyReportMutationSnapshot) -> bool:
    if expected.startswith("no_daily_write_"):
        return after == before
    if expected == "travel_event_with_required_tomorrow_plan":
        return (
            after.today_work == before.today_work
            and after.problems == before.problems
            and len(after.tomorrow_plan) == len(before.tomorrow_plan) + 1
            and after.version == before.version + 1
        )
    return after.owner_user_id == before.owner_user_id and after.report_id == before.report_id


def _expected_for_case(case_id: str) -> str:
    return next(
        str(case["expected"])
        for case in CASES
        if str(case["case_id"]) == str(case_id)
    )


def _rescore_artifacts(input_paths: tuple[Path, ...], output_path: Path) -> dict[str, Any]:
    source_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in input_paths]
    results: list[dict[str, Any]] = []
    for source_path, payload in zip(input_paths, source_payloads, strict=True):
        for raw in payload.get("results", []):
            result = dict(raw)
            expected = _expected_for_case(str(result["case_id"]))
            before = _snapshot_from_payload(result["before_snapshot"])
            after = _snapshot_from_payload(result["after_snapshot"])
            decision = result.get("semantic_decision")
            result["expected"] = expected
            result["acceptance"] = _acceptance(
                expected,
                before,
                after,
                decision=decision if isinstance(decision, dict) else None,
            )
            result["safe_write"] = _safe_write(expected, before, after)
            result["source_artifact"] = str(source_path)
            results.append(result)
    payload = {
        "schema_version": "agent2-daily-context-real-model-rescored-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "adjudication": (
            "An explicit future travel event may also be recorded as tomorrow_plan; "
            "it must not be written as today_work."
        ),
        "source_artifacts": [str(path) for path in input_paths],
        "turn_run_count": len(results),
        "model_call_count": sum(len(item.get("model_calls", [])) for item in results),
        "acceptance_passed": sum(item["acceptance"] == "PASS" for item in results),
        "acceptance_failed": sum(item["acceptance"] == "FAIL" for item in results),
        "external_business_side_effects": 0,
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _snapshot_from_payload(payload: dict[str, Any]) -> DailyReportMutationSnapshot:
    return DailyReportMutationSnapshot(
        report_id=__import__("uuid").UUID(str(payload["report_id"])),
        owner_user_id=__import__("uuid").UUID(str(payload["owner_user_id"])),
        version=int(payload["version"]),
        status=str(payload["status"]),
        today_work=tuple(payload.get("today_work") or ()),
        problems=tuple(payload.get("problems") or ()),
        tomorrow_plan=tuple(payload.get("tomorrow_plan") or ()),
        item_ids={
            str(key): tuple(value)
            for key, value in dict(payload.get("item_ids") or {}).items()
        },
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--rescore-input", action="append", type=Path, default=[])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evidence/agent2_daily_context_release/real_model_acceptance.json"),
    )
    args = parser.parse_args()
    if args.rescore_input:
        result = _rescore_artifacts(tuple(args.rescore_input), args.output)
    else:
        result = asyncio.run(
            run(
                repetitions=args.repetitions,
                output_path=args.output,
                case_ids=tuple(args.case_id),
            )
        )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "turn_run_count",
                    "model_call_count",
                    "acceptance_passed",
                    "acceptance_failed",
                    "external_business_side_effects",
                )
                if key in result
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.get("failed_runs", 0) == 0 and result["acceptance_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
