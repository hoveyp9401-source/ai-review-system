"""Isolated live evaluation for one trusted cross-turn Daily write retry.

The first failed turn is scripted only to reproduce the already-observed
source-binding incident.  Every decision after that boundary is made by the
real DeepSeek Flash adapter.  The public canary ingress, real deterministic
binder, and the existing in-memory conversation harness are used without a
business database, production handler, or message sender.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_agent2_daily_weekly_cross_turn_handoff as harness

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME

_SHANGHAI = harness.SHANGHAI
_CATEGORIES = (
    "retry_lifecycle",
    "new_topic_no_retry",
    "ambiguous_no_retry",
    "daily_with_weekly_tools",
)


@dataclass(frozen=True)
class RetrySeed:
    original_text: str
    expected_content: str
    unsafe_rewrite: str


_PRIMARY_SEED = RetrySeed(
    original_text="今天做了日报的基础功能优化",
    expected_content="做了日报的基础功能优化",
    unsafe_rewrite="日报基础功能已经全部优化完成",
)
_CROSS_DOMAIN_SEED = RetrySeed(
    original_text="今天核对两份用印申请并保留原审批意见",
    expected_content="核对两份用印申请并保留原审批意见",
    unsafe_rewrite="完成两份用印申请复核",
)


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    result: list[dict[str, Any]] = []
    for raw in message.get("tool_calls") or ():
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        arguments_raw = function.get("arguments")
        try:
            arguments = (
                json.loads(arguments_raw)
                if isinstance(arguments_raw, str)
                else dict(arguments_raw or {})
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {}
        result.append({"name": name, "arguments": arguments})
    return result


def _trusted_context(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and isinstance(
            decoded.get("trusted_context"), dict
        ):
            return decoded["trusted_context"]
    return {}


def _request_kind(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    system = ""
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        system = str(messages[0].get("content") or "")
    if "isolated Agent2 semantic reviewer" in system:
        return "independent_semantic_review"
    if "formatting one already-written Agent2 semantic-review" in system:
        return "review_envelope_repair"
    return "main_agent"


class RecordingHttpClient:
    """Record only structural decisions; never retain prompts or reasoning."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.records: list[dict[str, Any]] = []

    async def post(self, endpoint: str, *, json: dict[str, Any], timeout: Any):
        context = _trusted_context(json)
        candidate = context.get("retryable_daily_write")
        candidate_id = (
            str(candidate.get("candidate_id") or "")
            if isinstance(candidate, dict)
            else ""
        )
        response = await self._client.post(endpoint, json=json, timeout=timeout)
        try:
            body = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            body = {}
        message: dict[str, Any] = {}
        choices = body.get("choices") if isinstance(body, dict) else None
        if (
            isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            and isinstance(choices[0].get("message"), dict)
        ):
            message = choices[0]["message"]
        calls = _tool_calls(message)
        self.records.append(
            {
                "request_kind": _request_kind(json),
                "http_status": response.status_code,
                "candidate_present": bool(candidate_id),
                "weekly_write_tool_offered": any(
                    isinstance(tool, dict)
                    and isinstance(tool.get("function"), dict)
                    and tool["function"].get("name") == "apply_next_weekly_plan"
                    for tool in json.get("tools") or ()
                ),
                "response_calls": [
                    {
                        "name": call["name"],
                        "date_selection": call["arguments"].get("date_selection"),
                        "retry_candidate_matches_context": bool(
                            candidate_id
                            and call["arguments"].get("retry_candidate_id")
                            == candidate_id
                        ),
                        "item_fields": [
                            str(item.get("field") or "")
                            for item in call["arguments"].get("items") or ()
                            if isinstance(item, dict)
                        ],
                        "item_contents": [
                            str(item.get("content") or "")
                            for item in call["arguments"].get("items") or ()
                            if isinstance(item, dict)
                        ],
                    }
                    for call in calls
                ],
            }
        )
        return response

    def mark(self) -> int:
        return len(self.records)

    def since(self, mark: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.records[mark:]]


def _all_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        call
        for record in records
        for call in record.get("response_calls") or ()
        if isinstance(call, dict)
    ]


def _calls_by_kind(
    records: list[dict[str, Any]],
    kind: str,
) -> list[dict[str, Any]]:
    return [
        call
        for record in records
        if record.get("request_kind") == kind
        for call in record.get("response_calls") or ()
        if isinstance(call, dict)
    ]


async def _seed_failed_write(
    monkeypatch: pytest.MonkeyPatch,
    session: harness._ConversationSession,
    *,
    seed: RetrySeed,
    now: datetime,
    identity: str,
) -> dict[str, Any]:
    unsafe = harness._daily_call(
        f"seed-{identity}",
        content=seed.unsafe_rewrite,
    )
    reviewed = json.loads(json.dumps(unsafe, ensure_ascii=False))
    reviewed["id"] = f"seed-reviewed-{identity}"
    outcome, _ = await harness._run_turn(
        monkeypatch,
        session,
        source_message_id=f"seed-message-{identity}",
        user_text=seed.original_text,
        now=now,
        model_messages=[
            harness._assistant_tools(unsafe),
            harness._assistant_tools(reviewed),
            harness._terminal(
                reply="这次没有写入日报。",
                actual_write=False,
                outcome="not_executed",
            ),
        ],
    )
    observation = session.events[-1].response_payload["_agent2_turn_observation_v1"]
    candidate = observation["pre_execution_blocks"][0]["retry_candidate"]
    if (
        outcome.actual_write
        or outcome.tool_blocked_count != 1
        or session.committed["daily"]
    ):
        raise AssertionError("failed-write seed did not remain zero-write")
    return {
        "blocked": True,
        "candidate_created": bool(candidate.get("candidate_id")),
        "daily_item_count": 0,
    }


def _retry_selection_pass(
    records: list[dict[str, Any]],
    *,
    expected_content: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    main_calls = _calls_by_kind(records, "main_agent")
    review_calls = _calls_by_kind(records, "independent_semantic_review")
    for label, calls in (("main", main_calls), ("review", review_calls)):
        daily = [call for call in calls if call.get("name") == "add_daily_items"]
        if len(daily) != 1:
            errors.append(f"{label} selected {len(daily)} Daily retry calls")
            continue
        selected = daily[0]
        if selected.get("date_selection") != "trusted_failed_write":
            errors.append(f"{label} did not use trusted_failed_write")
        if selected.get("retry_candidate_matches_context") is not True:
            errors.append(f"{label} did not bind the unique candidate")
        contents = selected.get("item_contents") or []
        if len(contents) != 1 or expected_content not in str(contents[0]):
            errors.append(f"{label} did not preserve the complete source matter")
    return not errors, errors


async def _evaluate_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    client: RecordingHttpClient,
    *,
    round_number: int,
) -> dict[str, Any]:
    session = harness._ConversationSession()
    now = datetime(2026, 8, 15, 21, 36, tzinfo=_SHANGHAI) + timedelta(
        minutes=round_number * 10
    )
    seed_result = await _seed_failed_write(
        monkeypatch,
        session,
        seed=_PRIMARY_SEED,
        now=now,
        identity=f"lifecycle-{round_number}",
    )
    mark = client.mark()
    retry, _ = await harness._run_turn(
        monkeypatch,
        session,
        source_message_id=f"retry-lifecycle-{round_number}",
        user_text="你再试试",
        now=now + timedelta(minutes=1),
        model_client=client,
    )
    retry_records = client.since(mark)
    selected, selection_errors = _retry_selection_pass(
        retry_records,
        expected_content=_PRIMARY_SEED.expected_content,
    )
    persisted = list(session.committed["daily"])
    exact_server_source = bool(
        len(persisted) == 1
        and persisted[0] in _PRIMARY_SEED.original_text
        and _PRIMARY_SEED.expected_content in persisted[0]
    )

    mark = client.mark()
    repeat, _ = await harness._run_turn(
        monkeypatch,
        session,
        source_message_id=f"repeat-lifecycle-{round_number}",
        user_text="再试试",
        now=now + timedelta(minutes=2),
        model_client=client,
    )
    repeat_records = client.since(mark)
    repeat_has_candidate = any(
        record.get("candidate_present") for record in repeat_records
    )
    no_duplicate = bool(
        not repeat.actual_write
        and session.committed["daily"] == persisted
        and session.committed["daily_version"] == 1
    )
    errors = [*selection_errors]
    if not retry.actual_write or retry.tool_success_count != 1:
        errors.append("retry did not commit exactly one in-memory write")
    if not exact_server_source:
        errors.append(
            "server-bound content was not the complete original source matter"
        )
    if repeat_has_candidate:
        errors.append("candidate remained visible after success")
    if not no_duplicate:
        errors.append("repeat after success duplicated the write")
    return {
        "category": "retry_lifecycle",
        "round": round_number,
        "pass": not errors,
        "errors": errors,
        "seed": seed_result,
        "retry": {
            "actual_write": retry.actual_write,
            "tool_success_count": retry.tool_success_count,
            "server_persisted_contents": persisted,
            "main_and_review_selected_unique_candidate": selected,
            "trace": retry_records,
        },
        "repeat_after_success": {
            "actual_write": repeat.actual_write,
            "candidate_visible": repeat_has_candidate,
            "daily_version": session.committed["daily_version"],
            "trace": repeat_records,
        },
    }


async def _evaluate_nonselection(
    monkeypatch: pytest.MonkeyPatch,
    client: RecordingHttpClient,
    *,
    round_number: int,
    category: Literal["new_topic_no_retry", "ambiguous_no_retry"],
) -> dict[str, Any]:
    session = harness._ConversationSession()
    now = datetime(2026, 8, 15, 19, 0, tzinfo=_SHANGHAI) + timedelta(
        minutes=round_number * 10
    )
    seed_result = await _seed_failed_write(
        monkeypatch,
        session,
        seed=_PRIMARY_SEED,
        now=now,
        identity=f"{category}-{round_number}",
    )
    user_text = (
        "先别重试日报了，换个话题，给我讲讲这个安全拦截是什么意思。"
        if category == "new_topic_no_retry"
        else "你看着办吧"
    )
    mark = client.mark()
    outcome, _ = await harness._run_turn(
        monkeypatch,
        session,
        source_message_id=f"{category}-{round_number}",
        user_text=user_text,
        now=now + timedelta(minutes=1),
        model_client=client,
    )
    records = client.since(mark)
    calls = _all_calls(records)
    selected_candidate = any(
        call.get("date_selection") == "trusted_failed_write" for call in calls
    )
    errors: list[str] = []
    if selected_candidate:
        errors.append("model selected the retry candidate without an explicit retry")
    if outcome.actual_write or session.committed["daily"]:
        errors.append("non-retry turn wrote Daily content")
    return {
        "category": category,
        "round": round_number,
        "pass": not errors,
        "errors": errors,
        "seed": seed_result,
        "candidate_was_available": any(
            record.get("candidate_present") for record in records
        ),
        "selected_candidate": selected_candidate,
        "actual_write": outcome.actual_write,
        "daily_item_count": len(session.committed["daily"]),
        "trace": records,
    }


async def _evaluate_cross_domain(
    monkeypatch: pytest.MonkeyPatch,
    client: RecordingHttpClient,
    *,
    round_number: int,
) -> dict[str, Any]:
    session = harness._ConversationSession()
    now = datetime(2026, 8, 15, 20, 0, tzinfo=_SHANGHAI) + timedelta(
        minutes=round_number * 10
    )
    seed_result = await _seed_failed_write(
        monkeypatch,
        session,
        seed=_CROSS_DOMAIN_SEED,
        now=now,
        identity=f"cross-domain-{round_number}",
    )
    mark = client.mark()
    outcome, _ = await harness._run_turn(
        monkeypatch,
        session,
        source_message_id=f"cross-domain-retry-{round_number}",
        user_text="请按刚才那笔日报原样再试一次",
        now=now + timedelta(minutes=1),
        model_client=client,
    )
    records = client.since(mark)
    selected, selection_errors = _retry_selection_pass(
        records,
        expected_content=_CROSS_DOMAIN_SEED.expected_content,
    )
    weekly_tool_open = any(
        record.get("weekly_write_tool_offered") for record in records
    )
    calls = _all_calls(records)
    cross_domain_calls = [
        call.get("name")
        for call in calls
        if call.get("name")
        in {
            "apply_next_weekly_plan",
            "submit_next_weekly_plan",
            "record_weekly_plan_items_as_today_work",
        }
    ]
    persisted = list(session.committed["daily"])
    exact_server_source = bool(
        len(persisted) == 1
        and persisted[0] in _CROSS_DOMAIN_SEED.original_text
        and _CROSS_DOMAIN_SEED.expected_content in persisted[0]
    )
    errors = [*selection_errors]
    if not weekly_tool_open:
        errors.append("weekly write tool was not simultaneously open")
    if cross_domain_calls or session.committed["weekly_plan"] is not None:
        errors.append("pure Daily retry crossed into the weekly-plan domain")
    if not outcome.actual_write or not exact_server_source:
        errors.append("pure Daily retry did not write one exact source matter")
    return {
        "category": "daily_with_weekly_tools",
        "round": round_number,
        "pass": not errors,
        "errors": errors,
        "seed": seed_result,
        "weekly_write_tool_open": weekly_tool_open,
        "main_and_review_selected_unique_candidate": selected,
        "cross_domain_calls": cross_domain_calls,
        "daily_contents": persisted,
        "weekly_plan_created": session.committed["weekly_plan"] is not None,
        "trace": records,
    }


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    base_url = args.base_url.rstrip("/")
    endpoint = base_url + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    original_settings = harness._settings
    base_settings = vars(original_settings()).copy()
    base_settings["llm_base_url"] = base_url
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        harness,
        "_settings",
        lambda: SimpleNamespace(**base_settings),
    )

    results: list[dict[str, Any]] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(args.timeout_seconds),
        ) as raw_client:
            client = RecordingHttpClient(raw_client)
            selected_categories = tuple(args.category or _CATEGORIES)
            for round_number in range(1, args.rounds + 1):
                for category in selected_categories:
                    if category == "retry_lifecycle":
                        result = await _evaluate_lifecycle(
                            monkeypatch,
                            client,
                            round_number=round_number,
                        )
                    elif category in {
                        "new_topic_no_retry",
                        "ambiguous_no_retry",
                    }:
                        result = await _evaluate_nonselection(
                            monkeypatch,
                            client,
                            round_number=round_number,
                            category=category,
                        )
                    else:
                        result = await _evaluate_cross_domain(
                            monkeypatch,
                            client,
                            round_number=round_number,
                        )
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                "category": result["category"],
                                "round": result["round"],
                                "pass": result["pass"],
                                "errors": result["errors"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        flush=True,
                    )
    finally:
        monkeypatch.undo()

    passed = sum(bool(item["pass"]) for item in results)
    summary = {
        "model": CANARY_MODEL_NAME,
        "rounds_per_category": args.rounds,
        "category_count": len(tuple(args.category or _CATEGORIES)),
        "case_runs": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "all_passed": passed == len(results),
        "business_database_connected": False,
        "production_handlers_called": False,
        "messages_sent": False,
        "business_data_written": False,
        "in_memory_harness_only": True,
    }
    artifact = {
        "schema_version": "agent2.daily-write-retry.isolated-live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
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
    return 0 if summary["all_passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Isolated live evaluation for trusted Daily write retry"
    )
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument(
        "--category",
        action="append",
        choices=_CATEGORIES,
        help="Run only this category; repeat to select more than one.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_API_BASE")
        or os.environ.get("LLM_BASE_URL")
        or "https://api.deepseek.com/v1",
    )
    parser.add_argument(
        "--output",
        default="artifacts/daily_retry_live_2rounds.json",
    )
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("rounds must be positive")
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
