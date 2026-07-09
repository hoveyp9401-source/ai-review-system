from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm.client import LLMClient
from scripts.run_workflow_gate_replay import run_replay


WORKFLOWS = {
    "daily_report",
    "monthly_report",
    "weekly_report",
    "case_progress",
    "travel_coordination",
    "legal_research",
    "internal_qa",
    "small_talk",
    "unknown_or_help",
}

GATE_DECISIONS = {"allow_legacy_daily", "block_legacy_daily", "needs_confirmation"}


@dataclass(frozen=True)
class EvalItem:
    eval_id: str
    row: dict[str, Any]


def main() -> None:
    parser = argparse.ArgumentParser(description="Use a real LLM as an offline judge for Agent 2.0 workflow routing.")
    parser.add_argument("--source", choices=["webhook", "interaction", "performance", "all"], default="interaction")
    parser.add_argument("--mode", choices=["observe_only", "protective_gate", "strict_gate"], default="protective_gate")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--model", default="", help="Defaults to settings.llm_intent_model.")
    parser.add_argument("--output", default="outputs/workflow_llm_eval_latest.json")
    parser.add_argument("--max-text-chars", type=int, default=1800)
    args = parser.parse_args()

    result = asyncio.run(run_eval(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = result["summary"]
    print(f"workflow llm eval written: {output}")
    print(
        "summary: "
        f"total={summary['total']} judged={summary['judged']} errors={summary['llm_error_count']} "
        f"primary_mismatch={summary['primary_mismatch_count']} "
        f"gate_mismatch={summary['gate_mismatch_count']} "
        f"gate_allow_llm_block={summary['gate_allow_llm_block_count']} "
        f"gate_block_llm_allow={summary['gate_block_llm_allow_count']}"
    )


async def run_eval(args: argparse.Namespace) -> dict[str, Any]:
    replay_args = argparse.Namespace(
        source=args.source,
        mode=args.mode,
        days=args.days,
        start=args.start,
        end=args.end,
        limit=args.limit,
        output="",
        include_text=True,
    )
    replay = await run_replay(replay_args)
    rows = replay["rows"]
    items = [
        EvalItem(eval_id=f"m{index:05d}", row=row)
        for index, row in enumerate(rows, start=1)
        if str(row.get("raw_text") or "").strip()
    ]

    settings = get_settings()
    client = LLMClient(settings)
    model = args.model or settings.llm_intent_model or settings.llm_model
    judgments: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    try:
        for batch_start in range(0, len(items), max(1, args.batch_size)):
            batch = items[batch_start : batch_start + max(1, args.batch_size)]
            try:
                batch_judgments = await _judge_batch(
                    client=client,
                    model=model,
                    batch=batch,
                    max_text_chars=args.max_text_chars,
                )
            except Exception as exc:
                errors.append(
                    {
                        "batch_start": batch_start,
                        "batch_size": len(batch),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            for judgment in batch_judgments:
                eval_id = str(judgment.get("id") or "")
                if eval_id:
                    judgments[eval_id] = _normalize_judgment(judgment)
            print(
                f"LLM_EVAL_PROGRESS judged={len(judgments)} errors={len(errors)} total={len(items)}",
                flush=True,
            )
    finally:
        await client.close()

    evaluated_rows: list[dict[str, Any]] = []
    primary_mismatches: list[dict[str, Any]] = []
    gate_mismatches: list[dict[str, Any]] = []
    gate_allow_llm_block: list[dict[str, Any]] = []
    gate_block_llm_allow: list[dict[str, Any]] = []

    for item in items:
        judgment = judgments.get(item.eval_id)
        row = item.row
        if not judgment:
            continue
        comparison = _compare(row, judgment)
        combined = {
            "id": item.eval_id,
            "source_kind": row.get("source_kind"),
            "source_id": row.get("source_id"),
            "created_at": row.get("created_at"),
            "user_name": row.get("user_name"),
            "raw_text_hash": row.get("raw_text_hash"),
            "raw_text_preview": row.get("raw_text_preview"),
            "agent2": {
                "primary_workflow": row.get("primary_workflow"),
                "matched_workflows": row.get("matched_workflows") or [],
                "effects": row.get("effects") or [],
                "gate_allow_legacy_daily": bool((row.get("gate") or {}).get("allow_legacy_daily")),
                "gate_reply_type": (row.get("gate") or {}).get("reply_type"),
                "gate_audit_tags": (row.get("gate") or {}).get("audit_tags") or [],
                "legacy_impact": row.get("legacy_impact"),
                "segments": row.get("segments") or [],
            },
            "llm": judgment,
            "comparison": comparison,
        }
        evaluated_rows.append(combined)
        if comparison["primary_mismatch"]:
            primary_mismatches.append(combined)
        if comparison["gate_mismatch"]:
            gate_mismatches.append(combined)
        if comparison["gate_allow_llm_block"]:
            gate_allow_llm_block.append(combined)
        if comparison["gate_block_llm_allow"]:
            gate_block_llm_allow.append(combined)

    return {
        "source": args.source,
        "mode": args.mode,
        "model": model,
        "replay_summary": replay["summary"],
        "summary": {
            "total": len(items),
            "judged": len(evaluated_rows),
            "llm_error_count": len(errors),
            "primary_mismatch_count": len(primary_mismatches),
            "gate_mismatch_count": len(gate_mismatches),
            "gate_allow_llm_block_count": len(gate_allow_llm_block),
            "gate_block_llm_allow_count": len(gate_block_llm_allow),
        },
        "errors": errors,
        "primary_mismatches": primary_mismatches[:200],
        "gate_mismatches": gate_mismatches[:200],
        "gate_allow_llm_block": gate_allow_llm_block[:200],
        "gate_block_llm_allow": gate_block_llm_allow[:200],
        "rows": evaluated_rows,
    }


async def _judge_batch(
    *,
    client: LLMClient,
    model: str,
    batch: list[EvalItem],
    max_text_chars: int,
) -> list[dict[str, Any]]:
    payload = [
        {
            "id": item.eval_id,
            "source_kind": item.row.get("source_kind"),
            "active_daily_context": bool(item.row.get("daily_context_active_before")),
            "active_task_workflows": item.row.get("active_task_workflows") or [],
            "legacy_action": item.row.get("legacy_action") or "",
            "legacy_impact_kind": item.row.get("legacy_impact_kind") or "",
            "legacy_write_impact": bool(item.row.get("legacy_write_impact")),
            "text": _truncate(str(item.row.get("raw_text") or ""), max_text_chars),
        }
        for item in batch
    ]
    response = await client.complete_json(
        system_prompt=_system_prompt(),
        user_prompt=json.dumps({"messages": payload}, ensure_ascii=False),
        model=model,
        thinking_enabled=False,
        timeout_seconds=90,
        max_retries=1,
    )
    data = json.loads(response)
    judgments = data.get("judgments")
    if not isinstance(judgments, list):
        raise ValueError("LLM response missing judgments list")
    return [judgment for judgment in judgments if isinstance(judgment, dict)]


def _system_prompt() -> str:
    return """
You are an offline QA judge for a Chinese DingTalk legal-department Agent 2.0 workflow router.

Classify each message independently. Do not execute anything.

Available workflows:
- daily_report: user is filling, editing, confirming, copying, backfilling, displaying, or otherwise operating a daily report.
- monthly_report: user is filling metric/monthly performance fields, such as 未完成原因/存在问题, 下月目标, 行动方案, 指标, 绩效, 月报.
- weekly_report: user is preparing a weekly report.
- case_progress: user reports or requests collection of specific case progress.
- travel_coordination: user mentions travel schedule/coordination.
- legal_research: user asks for law, regulation, precedent, case search, or legal research.
- internal_qa: user asks an internal process/system/question that is not legal research.
- small_talk: social chatter that should not be written to a workflow.
- unknown_or_help: unclear or unsupported.

Important rules:
- Workflow ownership is not a single-choice problem. A message may contain multiple sentence-level intents.
- If active_daily_context is true, short non-question replies such as “休假”, “交了”, “复制昨天”, “不怎么涉及风险” can belong to daily_report.
- Real questions such as “怎么填写这个内容” should not be silently written to daily_report, even with active daily context.
- Monthly-report templates should not silently fall into daily_report.
- Metric/monthly-report replies or edits containing “未完成原因/存在问题/下月目标/行动方案/指标/绩效/月报”, especially “第N项/指标名 的行动方案 改为...”, are monthly_report and must block legacy daily.
- If there is no active_daily_context and no explicit daily-report wording, bare acknowledgements or fragments such as “确认”, “确定”, “全部”, “问题”, “暂无计划” should be unknown_or_help/block_legacy_daily rather than assumed daily_report.
- Personal-life chatter, food/travel-for-food, insults, and bot/output feedback such as “鸡蛋饼/手抓饼/汉堡/炸鸡/你是傻子/格式有问题” must block legacy daily even if they contain 今天/明天/去/问题.
- A concrete work trip sentence such as “明天去南京出差”, “我明天去厦门开庭”, or “今天去常州开庭” is useful daily-report content by itself: tomorrow trips belong in 明日计划, already-happened/today trips belong in 今日工作. It may also be travel_coordination/case_progress sidecar, but legacy daily should be allowed; do not mark it needs_confirmation merely because there is no active daily context.
- Explicit current-report display requests such as “展示日报/发我看下/当前日报” are daily_report read-only requests and may allow legacy daily routing.
- For each message, decide whether the legacy daily executor should be allowed:
  - allow_legacy_daily: safe for old daily executor to receive it.
  - block_legacy_daily: should not enter old daily executor.
  - needs_confirmation: cross-workflow or ambiguous ownership requiring explicit confirmation.
- Daily-report edits are intentionally lightweight: explicit daily clear/delete/revoke/cross-date edits should be allowed without extra confirmation. Daily reports are reversible and audited; do not mark them as needs_confirmation only because they are destructive-looking.

Return strict JSON:
{
  "judgments": [
    {
      "id": "same id",
      "primary_workflow": "one workflow",
      "matched_workflows": ["workflow", "..."],
      "gate_decision": "allow_legacy_daily|block_legacy_daily|needs_confirmation",
      "has_multi_intent": true|false,
      "segments": [
        {"index": 1, "workflow": "workflow", "intent": "short label"}
      ],
      "confidence": "high|medium|low",
      "reason": "brief Chinese explanation"
    }
  ]
}
""".strip()


def _normalize_judgment(judgment: dict[str, Any]) -> dict[str, Any]:
    primary = str(judgment.get("primary_workflow") or "unknown_or_help")
    if primary not in WORKFLOWS:
        primary = "unknown_or_help"
    matched = [
        str(workflow)
        for workflow in judgment.get("matched_workflows") or []
        if str(workflow) in WORKFLOWS
    ]
    if primary != "small_talk" and primary not in matched and primary in WORKFLOWS:
        matched.insert(0, primary)
    gate = str(judgment.get("gate_decision") or "block_legacy_daily")
    if gate not in GATE_DECISIONS:
        gate = "block_legacy_daily"
    segments = judgment.get("segments") if isinstance(judgment.get("segments"), list) else []
    return {
        "primary_workflow": primary,
        "matched_workflows": matched,
        "gate_decision": gate,
        "has_multi_intent": bool(judgment.get("has_multi_intent")),
        "segments": segments,
        "confidence": str(judgment.get("confidence") or ""),
        "reason": str(judgment.get("reason") or ""),
    }


def _compare(row: dict[str, Any], judgment: dict[str, Any]) -> dict[str, Any]:
    agent_primary = str(row.get("primary_workflow") or "unknown_or_help")
    llm_primary = str(judgment.get("primary_workflow") or "unknown_or_help")
    if llm_primary == "small_talk":
        llm_primary_for_compare = "unknown_or_help"
    else:
        llm_primary_for_compare = llm_primary

    agent_allow = bool((row.get("gate") or {}).get("allow_legacy_daily"))
    llm_gate = str(judgment.get("gate_decision") or "block_legacy_daily")
    llm_allow = llm_gate == "allow_legacy_daily"

    return {
        "primary_mismatch": agent_primary != llm_primary_for_compare,
        "gate_mismatch": agent_allow != llm_allow,
        "gate_allow_llm_block": agent_allow and not llm_allow,
        "gate_block_llm_allow": (not agent_allow) and llm_allow,
        "agent_primary": agent_primary,
        "llm_primary": llm_primary,
        "agent_allow_legacy_daily": agent_allow,
        "llm_gate_decision": llm_gate,
    }


def _truncate(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    main()
