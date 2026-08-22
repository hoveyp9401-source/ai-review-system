#!/usr/bin/env python3
"""Run a redacted personal-weekly-brief matrix against the real Agent2 model."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.agent2.personal_weekly_brief import (
    Agent2PersonalWeeklyBriefGenerator,
    Agent2PersonalWeeklyBriefModelPipeline,
    Agent2PersonalWeeklyBriefReviewer,
    PersonalWeeklyBriefContent,
    PersonalWeeklyBriefReviewRejected,
    PersonalWeeklyBriefSnapshot,
    SourceEvidence,
)
from app.agent2.tool_calling.canary_config import (
    CANARY_MAX_REQUEST_ATTEMPTS,
    CANARY_MODEL_NAME,
    CANARY_TIMEOUT_SECONDS,
)
from app.config import Settings
from app.llm.client import LLMClient


SHANGHAI = ZoneInfo("Asia/Shanghai")
SNAPSHOT_AT = datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)
WEEK_START = date(2026, 8, 17)
WEEK_END = date(2026, 8, 21)
OWNER_USER_ID = "11111111-1111-4111-8111-111111111111"
EXPECTED_MODEL = "deepseek-v4-flash"
SCHEMA_VERSION = "agent2.personal_weekly_brief.real_model_eval.v1"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2, choices=range(1, 4))
    return parser.parse_args()


def _source(
    source_id: str,
    *,
    kind: str,
    source_date: date,
    section: str,
    text: str,
    record_id: str,
) -> SourceEvidence:
    return SourceEvidence(
        source_id=source_id,
        source_kind=kind,
        source_record_id=record_id,
        source_date=source_date,
        section=section,
        original_text=text,
    )


def _snapshot(*sources: SourceEvidence) -> PersonalWeeklyBriefSnapshot:
    return PersonalWeeklyBriefSnapshot(
        tenant_id="tenant-redacted",
        owner_user_id=OWNER_USER_ID,
        week_start=WEEK_START,
        week_end=WEEK_END,
        snapshot_at=SNAPSHOT_AT,
        daily_report_dates=tuple(
            sorted(
                {
                    source.source_date
                    for source in sources
                    if source.source_kind == "daily_report"
                }
            )
        ),
        weekly_plan_found=any(
            source.source_kind == "weekly_plan" for source in sources
        ),
        sources=tuple(sources),
    )


def _complex_snapshot() -> PersonalWeeklyBriefSnapshot:
    return _snapshot(
        _source(
            "daily:star:mon",
            kind="daily_report",
            source_date=date(2026, 8, 17),
            section="today_work",
            text="跟进星河项目合同争议，涉案金额120万元，对方原定8月20日前回复。",
            record_id="report-mon",
        ),
        _source(
            "daily:star:thu",
            kind="daily_report",
            source_date=date(2026, 8, 20),
            section="today_work",
            text="继续跟进星河项目合同争议；对方没有承诺付款，表示只有付款条件确认后才答复。",
            record_id="report-thu",
        ),
        _source(
            "plan:completed",
            kind="weekly_plan",
            source_date=date(2026, 8, 17),
            section="plan_item",
            text="完成甲项目合同定稿并发给经办人。",
            record_id="plan-current",
        ),
        _source(
            "daily:completed",
            kind="daily_report",
            source_date=date(2026, 8, 18),
            section="today_work",
            text="甲项目合同定稿已完成并发给经办人。",
            record_id="report-tue",
        ),
        _source(
            "plan:ongoing",
            kind="weekly_plan",
            source_date=date(2026, 8, 18),
            section="plan_item",
            text="整理乙案件证据材料。",
            record_id="plan-current",
        ),
        _source(
            "daily:ongoing",
            kind="daily_report",
            source_date=date(2026, 8, 19),
            section="today_work",
            text="乙案件证据材料持续推进，目前仍在补充送达证明。",
            record_id="report-wed",
        ),
        _source(
            "plan:adjusted",
            kind="weekly_plan",
            source_date=date(2026, 8, 19),
            section="plan_item",
            text="周三参加丙案件开庭。",
            record_id="plan-current",
        ),
        _source(
            "daily:adjusted",
            kind="daily_report",
            source_date=date(2026, 8, 19),
            section="today_work",
            text="丙案件原定周三开庭，因法院通知改期到周五。",
            record_id="report-wed",
        ),
        _source(
            "plan:future",
            kind="weekly_plan",
            source_date=date(2026, 8, 20),
            section="plan_item",
            text="确认丁公司发票条件。",
            record_id="plan-current",
        ),
        _source(
            "daily:future",
            kind="daily_report",
            source_date=date(2026, 8, 21),
            section="tomorrow_plan",
            text="后续安排下周一确认丁公司发票条件。",
            record_id="report-fri",
        ),
        _source(
            "plan:no-followup",
            kind="weekly_plan",
            source_date=date(2026, 8, 21),
            section="plan_item",
            text="复核戊项目补充协议。",
            record_id="plan-current",
        ),
        _source(
            "daily:risk",
            kind="daily_report",
            source_date=date(2026, 8, 21),
            section="problems",
            text="如果8月25日前仍未收到授权文件，付款审批将无法按原计划发起。",
            record_id="report-fri",
        ),
    )


def _partial_without_plan_snapshot() -> PersonalWeeklyBriefSnapshot:
    return _snapshot(
        _source(
            "daily:partial:mon",
            kind="daily_report",
            source_date=date(2026, 8, 17),
            section="today_work",
            text="起草己项目补充协议，等待业务部门确认付款条件。",
            record_id="partial-mon",
        ),
        _source(
            "daily:partial:thu",
            kind="daily_report",
            source_date=date(2026, 8, 20),
            section="today_work",
            text="继续与业务部门沟通己项目付款条件，尚未形成最终意见。",
            record_id="partial-thu",
        ),
    )


def _plan_only_snapshot() -> PersonalWeeklyBriefSnapshot:
    return _snapshot(
        _source(
            "plan:only",
            kind="weekly_plan",
            source_date=date(2026, 8, 18),
            section="plan_item",
            text="准备庚案件庭审材料。",
            record_id="plan-only",
        )
    )


def _empty_snapshot() -> PersonalWeeklyBriefSnapshot:
    return _snapshot()


def _all_items(content: PersonalWeeklyBriefContent):
    return (
        *content.completed.items,
        *content.plan_progress.items,
        *content.possible_open_loops.items,
    )


def _assert_complex(content: PersonalWeeklyBriefContent) -> dict[str, Any]:
    by_plan_source = {
        source_id: item
        for item in content.plan_progress.items
        for source_id in item.source_ids
        if source_id.startswith("plan:")
    }
    expected_statuses = {
        "plan:completed": "已完成",
        "plan:ongoing": "持续推进",
        "plan:adjusted": "安排调整",
        "plan:future": "后续安排",
        "plan:no-followup": "暂时没有找到后续记录",
    }
    observed_statuses = {
        source_id: by_plan_source[source_id].status for source_id in expected_statuses
    }
    if observed_statuses != expected_statuses:
        raise AssertionError(
            f"unexpected plan statuses: {observed_statuses!r}"
        )
    merged = [
        item
        for item in _all_items(content)
        if {"daily:star:mon", "daily:star:thu"}.issubset(item.source_ids)
    ]
    if len(merged) != 1:
        raise AssertionError("cross-day Star River matter was not merged once")
    message = content.message_text
    for fact in ("120万元", "8月20日", "付款条件确认后"):
        if fact not in message:
            raise AssertionError(f"key fact was lost: {fact}")
    if not any(
        wording in message
        for wording in ("没有承诺付款", "未承诺付款", "并未承诺付款")
    ):
        raise AssertionError("the no-payment-commitment fact was lost")
    if "星河项目合同争议已完成" in message or "完成星河项目合同争议" in message:
        raise AssertionError("follow-up work was promoted to completion")
    plan_keys = {item.matter_key for item in content.plan_progress.items}
    open_keys = {item.matter_key for item in content.possible_open_loops.items}
    if plan_keys & open_keys:
        raise AssertionError("plan progress and open loops were not deduplicated")
    if "不等于未完成" not in message:
        raise AssertionError("no-follow-up disclaimer is missing")
    return {"plan_statuses": observed_statuses, "merged_source_count": 2}


def _assert_empty(content: PersonalWeeklyBriefContent) -> dict[str, Any]:
    if _all_items(content):
        raise AssertionError("empty snapshot invented brief items")
    return {"item_count": 0}


def _assert_partial_without_plan(
    content: PersonalWeeklyBriefContent,
) -> dict[str, Any]:
    if content.plan_progress.items:
        raise AssertionError("brief invented plan progress without a weekly plan")
    cited = {
        source_id
        for item in _all_items(content)
        for source_id in item.source_ids
    }
    if not {"daily:partial:mon", "daily:partial:thu"}.issubset(cited):
        raise AssertionError("partial-date daily work was not preserved")
    if "尚未形成最终意见" not in content.message_text:
        raise AssertionError("partial-date non-completion fact was lost")
    return {"daily_dates": ["2026-08-17", "2026-08-20"], "plan_items": 0}


def _assert_plan_only(content: PersonalWeeklyBriefContent) -> dict[str, Any]:
    if content.completed.items:
        raise AssertionError("brief invented completed work without a daily report")
    if len(content.plan_progress.items) != 1:
        raise AssertionError("plan-only snapshot did not produce one plan progress item")
    item = content.plan_progress.items[0]
    if item.status != "暂时没有找到后续记录":
        raise AssertionError("plan-only item was stated too strongly")
    if item.source_ids != ("plan:only",):
        raise AssertionError("plan-only source trace changed")
    return {"status": item.status}


CaseValidator = Callable[[PersonalWeeklyBriefContent], dict[str, Any]]


def _cases() -> tuple[
    tuple[str, PersonalWeeklyBriefSnapshot, CaseValidator], ...
]:
    return (
        ("cross_day_five_statuses", _complex_snapshot(), _assert_complex),
        ("partial_dates_without_weekly_plan", _partial_without_plan_snapshot(), _assert_partial_without_plan),
        ("weekly_plan_without_daily_reports", _plan_only_snapshot(), _assert_plan_only),
        ("no_daily_no_weekly_plan", _empty_snapshot(), _assert_empty),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_model_only_config(path: Path) -> dict[str, str]:
    """Read only the four allow-listed LLM fields from a private env file."""

    allowed = {
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    }
    selected: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if key not in allowed:
                continue
            value = raw_value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            selected[key] = value
    return selected


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary output already exists: {temporary}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.env_file.is_file():
        raise FileNotFoundError("model environment file not found")
    model_config = _read_model_only_config(args.env_file)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        llm_base_url=model_config.get("LLM_BASE_URL", ""),
        llm_api_key=model_config.get("LLM_API_KEY", ""),
        llm_model=EXPECTED_MODEL,
        llm_timeout_seconds=float(
            model_config.get("LLM_TIMEOUT_SECONDS", "60") or "60"
        ),
        llm_max_retries=int(
            model_config.get("LLM_MAX_RETRIES", "1") or "1"
        ),
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
    if CANARY_MODEL_NAME != EXPECTED_MODEL:
        raise AssertionError(
            f"Agent2 model is {CANARY_MODEL_NAME}, expected {EXPECTED_MODEL}"
        )
    if not settings.llm_api_key:
        raise AssertionError("actual model API key is not configured")
    host = urlparse(settings.llm_base_url).hostname or ""
    if not host or host == "api.example.invalid":
        raise AssertionError("actual model base URL is not configured")

    client = LLMClient(settings)
    generator = Agent2PersonalWeeklyBriefGenerator(
        client,
        model=CANARY_MODEL_NAME,
        thinking_enabled=True,
        timeout_seconds=CANARY_TIMEOUT_SECONDS,
        max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        client,
        model=CANARY_MODEL_NAME,
        thinking_enabled=True,
        timeout_seconds=CANARY_TIMEOUT_SECONDS,
        max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=generator,
        reviewer=reviewer,
    )
    results: list[dict[str, Any]] = []
    successful_content_by_case: dict[str, PersonalWeeklyBriefContent] = {}
    repair_performance_exercise: dict[str, Any] | None = None
    try:
        for round_number in range(1, args.rounds + 1):
            for case_id, snapshot, validator in _cases():
                print(
                    json.dumps(
                        {
                            "event": "case_started",
                            "round": round_number,
                            "case_id": case_id,
                            "model": CANARY_MODEL_NAME,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                try:
                    outcome = await pipeline.generate_and_review(
                        snapshot=snapshot,
                        recipient_name="脱敏用户",
                        personal_memory={"entries": []},
                    )
                except PersonalWeeklyBriefReviewRejected as exc:
                    print(
                        json.dumps(
                            {
                                "event": "review_rejected_after_repair",
                                "round": round_number,
                                "case_id": case_id,
                                "issues": list(exc.issues),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    raise
                content = outcome.content
                try:
                    assertions = validator(content)
                except AssertionError as exc:
                    print(
                        json.dumps(
                            {
                                "event": "deterministic_oracle_rejected",
                                "round": round_number,
                                "case_id": case_id,
                                "reason": str(exc),
                                "draft": content.as_payload(),
                                "trace": content.trace_payload(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    raise
                review = outcome.review
                total_seconds = outcome.total_seconds
                result = {
                    "round": round_number,
                    "case_id": case_id,
                    "status": "PASS",
                    "source_fingerprint": snapshot.fingerprint,
                    "source_count": len(snapshot.sources),
                    "daily_report_dates": [
                        value.isoformat() for value in snapshot.daily_report_dates
                    ],
                    "weekly_plan_found": snapshot.weekly_plan_found,
                    "content": content.as_payload(),
                    "trace": content.trace_payload(),
                    "message_sha256": _sha256(content.message_text),
                    "message_text": content.message_text,
                    "independent_model_review": review,
                    "model_metrics": {
                        "model_calls": outcome.model_calls,
                        "semantic_attempts": outcome.semantic_attempts,
                        "generation_seconds": [
                            round(value, 3) for value in outcome.generation_seconds
                        ],
                        "review_seconds": [
                            round(value, 3) for value in outcome.review_seconds
                        ],
                        "total_seconds": round(total_seconds, 3),
                        "timeout_seconds_per_attempt": CANARY_TIMEOUT_SECONDS,
                        "max_attempts_per_call": CANARY_MAX_REQUEST_ATTEMPTS,
                    },
                    "assertions": assertions,
                }
                results.append(result)
                successful_content_by_case[case_id] = content
                print(
                    json.dumps(
                        {
                            "event": "case_passed",
                            "round": round_number,
                            "case_id": case_id,
                            "total_seconds": round(total_seconds, 3),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        natural_repair = next(
            (
                item
                for item in reversed(results)
                if item["model_metrics"]["model_calls"] == 4
            ),
            None,
        )
        if natural_repair is not None:
            repair_performance_exercise = {
                "status": "PASS",
                "exercise_kind": "natural_bounded_repair_from_real_matrix",
                "combined_worst_path_model_calls": 4,
                "combined_worst_path_seconds": natural_repair[
                    "model_metrics"
                ]["total_seconds"],
                "case_id": natural_repair["case_id"],
                "round": natural_repair["round"],
                "independent_model_review": natural_repair[
                    "independent_model_review"
                ],
                "assertions": natural_repair["assertions"],
                "content": natural_repair["content"],
                "trace": natural_repair["trace"],
            }
        else:
            normal_complex = next(
                item
                for item in reversed(results)
                if item["case_id"] == "cross_day_five_statuses"
            )
            prior_content = successful_content_by_case[
                "cross_day_five_statuses"
            ]
            star_item = next(
                item
                for item in _all_items(prior_content)
                if {"daily:star:mon", "daily:star:thu"}.issubset(
                    item.source_ids
                )
            )
            repair_context = {
                "instruction": (
                    "这是有界修复路径的脱敏性能演练。完整 trusted_snapshot "
                    "仍是唯一事实来源；请重新逐源核对并返回完整五字段 JSON。"
                ),
                "previous_draft": prior_content.as_payload(),
                "issues": [
                    {
                        "matter_key": star_item.matter_key,
                        "reason": (
                            "请再次确认该事项完整保留来源中的金额、8月20日前日期、"
                            "未承诺付款的否定以及付款条件。"
                        ),
                    }
                ],
            }
            repair_generation_started = perf_counter()
            repaired_content = await generator.generate(
                snapshot=_complex_snapshot(),
                recipient_name="脱敏用户",
                personal_memory={"entries": []},
                repair_context=repair_context,
            )
            repair_generation_seconds = (
                perf_counter() - repair_generation_started
            )
            repair_review_started = perf_counter()
            repaired_review = await reviewer.review(
                snapshot=_complex_snapshot(),
                content=repaired_content,
            )
            repair_review_seconds = perf_counter() - repair_review_started
            repair_assertions = _assert_complex(repaired_content)
            repair_phase_seconds = (
                repair_generation_seconds + repair_review_seconds
            )
            combined_seconds = (
                float(normal_complex["model_metrics"]["total_seconds"])
                + repair_phase_seconds
            )
            repair_performance_exercise = {
                "status": "PASS",
                "exercise_kind": (
                    "forced_repair_with_real_generation_and_real_review"
                ),
                "normal_phase_model_calls": 2,
                "repair_phase_model_calls": 2,
                "combined_worst_path_model_calls": 4,
                "repair_generation_seconds": round(
                    repair_generation_seconds, 3
                ),
                "repair_review_seconds": round(repair_review_seconds, 3),
                "repair_phase_seconds": round(repair_phase_seconds, 3),
                "normal_phase_seconds": normal_complex["model_metrics"][
                    "total_seconds"
                ],
                "combined_worst_path_seconds": round(combined_seconds, 3),
                "independent_model_review": repaired_review,
                "assertions": repair_assertions,
                "content": repaired_content.as_payload(),
                "trace": repaired_content.trace_payload(),
            }
        combined_seconds = float(
            repair_performance_exercise["combined_worst_path_seconds"]
        )
        print(
            json.dumps(
                {
                    "event": "repair_performance_exercise_passed",
                    "combined_model_calls": 4,
                    "combined_seconds": round(combined_seconds, 3),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        await client.close()

    observed_totals = [
        float(item["model_metrics"]["total_seconds"]) for item in results
    ]
    normal_totals = [
        float(item["model_metrics"]["total_seconds"])
        for item in results
        if item["model_metrics"]["model_calls"] == 2
    ]
    repair_totals = [
        float(item["model_metrics"]["total_seconds"])
        for item in results
        if item["model_metrics"]["model_calls"] == 4
    ]
    if repair_performance_exercise is None:
        raise AssertionError("repair performance exercise did not run")
    repair_totals.append(
        float(repair_performance_exercise["combined_worst_path_seconds"])
    )
    concurrency_limit = 4
    projected_seconds = math.ceil(74 / concurrency_limit) * max(observed_totals)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "model": CANARY_MODEL_NAME,
        "rounds": args.rounds,
        "case_count": len(results),
        "repair_performance_exercise": repair_performance_exercise,
        "performance": {
            "normal_model_calls_per_owner": 2,
            "maximum_model_calls_per_owner": 4,
            "observed_owner_seconds_min": min(observed_totals),
            "observed_owner_seconds_max": max(observed_totals),
            "observed_owner_seconds_average": round(
                sum(observed_totals) / len(observed_totals), 3
            ),
            "normal_two_call_owner_seconds": normal_totals,
            "repair_four_call_owner_seconds": repair_totals,
            "production_concurrency_limit": concurrency_limit,
            "projected_74_normal_minutes_using_observed_max": (
                round(
                    math.ceil(74 / concurrency_limit) * max(normal_totals) / 60,
                    3,
                )
                if normal_totals
                else None
            ),
            "projected_74_repair_minutes_using_observed_max": (
                round(
                    math.ceil(74 / concurrency_limit) * max(repair_totals) / 60,
                    3,
                )
                if repair_totals
                else None
            ),
            "projected_74_owner_seconds_using_observed_max": round(
                projected_seconds, 3
            ),
            "projected_74_owner_minutes_using_observed_max": round(
                projected_seconds / 60, 3
            ),
        },
        "config_attestation": {
            "api_key_configured": True,
            "base_url_host": host,
            "model_config_fingerprint": _sha256(
                f"{host}\x1f{CANARY_MODEL_NAME}"
            ),
            "loaded_config_fields": sorted(model_config),
            "database_accessed": False,
            "dingtalk_transport_called": False,
            "real_user_data_used": False,
        },
        "results": results,
    }


def main() -> int:
    args = _args()
    payload = asyncio.run(_run(args))
    _write_json_atomically(args.output, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "model": payload["model"],
                "case_count": payload["case_count"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
