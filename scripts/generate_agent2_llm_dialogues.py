from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any
from urllib import request, error


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings
from app.workflows.relative_dates import has_previous_to_current_repeat_reference


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_SOURCE = "llm_generated_deepseek_v4_pro_v1"


CATEGORIES = (
    "single_daily_simple",
    "single_daily_complex",
    "multi_turn_daily_fill",
    "daily_edit_merge_delete_copy",
    "context_reference",
    "non_daily_chatter",
    "internal_qa_rag",
    "monthly_report_status",
    "travel_candidate",
    "case_progress_candidate",
    "mixed_intent",
    "ambiguous_expression",
    "temporal_boundary",
    "absurd_or_invalid_input",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Agent2 dialogue replay cases with DeepSeek V4-PRO.")
    parser.add_argument("--count", type=int, default=6000, help="Dialogue cases to generate.")
    parser.add_argument("--batch-size", type=int, default=20, help="Cases per LLM call.")
    parser.add_argument("--seed", type=int, default=2026070701)
    parser.add_argument("--output", default="evals/agent2/dialogues/llm_generated_latest.jsonl")
    parser.add_argument("--base-url", default=os.getenv("EVAL_GEN_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--model", default=os.getenv("EVAL_GEN_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default="EVAL_GEN_API_KEY")
    parser.add_argument("--temperature", type=float, default=0.9)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="Build prompts and print config without calling the API.")
    args = parser.parse_args()

    api_key = _resolve_api_key(args.api_key_env)
    if not api_key and not args.dry_run:
        print(f"Missing API key env var: {args.api_key_env}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "provider": "deepseek",
                "base_url": args.base_url,
                "model": args.model,
                "count": args.count,
                "batch_size": args.batch_size,
                "output": str(output),
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    if args.dry_run:
        preview_categories = _next_categories(rng, min(args.batch_size, args.count))
        print(_build_prompt(preview_categories, batch_index=1, seed=args.seed))
        return 0

    generated = 0
    seen: set[str] = set()
    with output.open("w", encoding="utf-8") as handle:
        batch_index = 0
        while generated < args.count:
            batch_index += 1
            remaining = args.count - generated
            batch_count = min(args.batch_size, remaining)
            categories = _next_categories(rng, batch_count)
            prompt = _build_prompt(categories, batch_index=batch_index, seed=args.seed)
            payload = _call_chat_completion(
                base_url=args.base_url,
                model=args.model,
                api_key=api_key or "",
                prompt=prompt,
                temperature=args.temperature,
                timeout_seconds=args.timeout_seconds,
                max_retries=args.max_retries,
            )
            cases = _normalize_generated_cases(payload, source=DEFAULT_SOURCE, batch_index=batch_index)
            for case in cases:
                key = _case_dedupe_key(case)
                if key in seen:
                    continue
                seen.add(key)
                handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
                generated += 1
                if generated >= args.count:
                    break
            handle.flush()
            print(json.dumps({"batch": batch_index, "generated": generated}, ensure_ascii=False), flush=True)
    return 0


def _next_categories(rng: random.Random, count: int) -> list[str]:
    categories = list(CATEGORIES)
    rng.shuffle(categories)
    result: list[str] = []
    while len(result) < count:
        if not categories:
            categories = list(CATEGORIES)
            rng.shuffle(categories)
        result.append(categories.pop())
    return result


def _resolve_api_key(primary_env_name: str) -> str:
    if os.getenv(primary_env_name):
        return str(os.getenv(primary_env_name) or "")
    if os.getenv("LLM_API_KEY"):
        return str(os.getenv("LLM_API_KEY") or "")
    try:
        return str(get_settings().llm_api_key or "")
    except Exception:
        return ""


def _build_prompt(categories: list[str], *, batch_index: int, seed: int) -> str:
    category_lines = "\n".join(f"- {index + 1}. {category}" for index, category in enumerate(categories))
    return f"""
你是 Agent2.0 离线评测用例生成器。目标不是写好看的样例，而是生成真实、刁钻、像钉钉用户随手发出来的中文对话，用来压测“法务中心协同入口”。

系统目标：
- 先判断用户这句话到底想做什么，再路由到日报、月报、出差协同、案件进展、内部问答/RAG、轻闲聊反馈。
- 闲聊、测试、吐槽、问问题，永远不能污染日报。
- 明确的日报内容要写入正确字段：今日工作、问题/风险、明日计划。
- 出差、案件进展可以作为候选，但不要误写成日报，除非这句话本身明确也是日报内容。
- 多轮对话必须保留上下文，不能因为上一轮是日报就把下一轮闲聊写进日报。

请生成 {len(categories)} 个 dialogue replay JSON 用例，覆盖这些类别：
{category_lines}

输出必须是严格 JSON 对象，不要 Markdown，不要解释：
{{
  "cases": [
    {{
      "dialogue_id": "llm-{batch_index}-001",
      "source": "{DEFAULT_SOURCE}",
      "metadata": {{
        "category": "single_daily_simple",
        "risk": "p0|p1|p2",
        "why": "这个用例想打到什么问题"
      }},
      "turns": [
        {{
          "turn_id": "t01",
          "text": "用户原话",
          "expected": {{
            "agent2_direct_write": true,
            "fallback_to_legacy": false,
            "raw_text_written": false
          }}
        }}
      ]
    }}
  ]
}}

expected 字段只能使用以下键：
- agent2_direct_write: true/false，是否允许 Agent2 直接改日报草稿。
- fallback_to_legacy: false，除非确实必须回退，默认必须 false。
- blocked_by_gate: true/false，可选。
- raw_text_written: true/false；混合闲聊+工作时必须是 false，避免整句原文污染日报。
- assistant_reply_type: small_talk/internal_qa/clarify/report_preview，可选，只在非常确定时填。
- report_today_work / report_problems / report_tomorrow_plan：只有非常确定最终草稿精确内容时才填。
- forbidden_today_work_contains / forbidden_problems_contains / forbidden_tomorrow_plan_contains：用于阻止闲聊、问句、荒谬内容进入日报。

生成要求：
- 至少一半是多轮对话，每个多轮 2-6 轮。
- 用户话术要口语化，允许错别字、半句话、省略主语、带情绪。
- 要包含“今天和昨天一样”“还是昨天那些事”“昨天的明日计划已完成”“写日报了”“让我测试下”“大家月报填得怎样了”“后天去保利案件开庭”“明天穿啥出门”等相近变体，但不要只复读这些原句。
- 要包含单句话多个意图，如“今天完成合同审核，顺便问下用印流程，明天去南京盖章”。
- 要包含明显不该写日报的内容，如吃饭、天气、吐槽、问机器人能力、测试句、荒谬出差。
- 每个 case 的 expected 要和文本语义一致；不确定时宁愿只填 agent2_direct_write / forbidden_*，不要编造精确日报草稿。
- 不要生成真实姓名、手机号、身份证、真实商业秘密。案件名可以用“保利案、恒大案、XX酒店项目”等泛化称呼。

批次信息：batch_index={batch_index}, seed={seed}
""".strip()


def _call_chat_completion(
    *,
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    temperature: float,
    timeout_seconds: float,
    max_retries: int,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You generate strict JSON for offline test cases."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = request.Request(url, data=data, headers=headers, method="POST")
            with request.urlopen(req, timeout=timeout_seconds) as resp:
                response_payload = json.loads(resp.read().decode("utf-8"))
            content = str(response_payload["choices"][0]["message"]["content"])
            return _parse_json_object(content)
        except (KeyError, json.JSONDecodeError, error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"DeepSeek generation failed after retries: {last_error}") from last_error


def _parse_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    return payload


def _normalize_generated_cases(payload: dict[str, Any], *, source: str, batch_index: int) -> list[dict[str, Any]]:
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("LLM response missing cases list")
    normalized: list[dict[str, Any]] = []
    for case_index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            continue
        turns = _normalize_turns(raw_case.get("turns"))
        if not turns:
            continue
        dialogue_id = str(raw_case.get("dialogue_id") or raw_case.get("case_id") or f"llm-{batch_index}-{case_index:03d}")
        normalized.append(
            {
                "dialogue_id": dialogue_id,
                "source": str(raw_case.get("source") or source),
                "metadata": _normalize_metadata(raw_case.get("metadata")),
                "turns": turns,
            }
        )
    return normalized


def _normalize_turns(raw_turns: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_turns, list):
        return []
    turns: list[dict[str, Any]] = []
    for index, raw_turn in enumerate(raw_turns, start=1):
        if not isinstance(raw_turn, dict):
            continue
        text = str(raw_turn.get("text") or raw_turn.get("raw_text") or "").strip()
        if not text:
            continue
        expected = _normalize_expected(raw_turn.get("expected"), text=text)
        turns.append(
            {
                "turn_id": str(raw_turn.get("turn_id") or f"t{index:02d}"),
                "text": text,
                "expected": expected,
            }
        )
    return turns


def _normalize_expected(raw_expected: Any, *, text: str) -> dict[str, Any]:
    data = raw_expected if isinstance(raw_expected, dict) else {}
    strict_report_expectations = data.get("strict_report_expectations") is True
    allowed = {
        "agent2_direct_write",
        "fallback_to_legacy",
        "blocked_by_gate",
        "raw_text_written",
        "assistant_reply_type",
        "report_today_work",
        "report_problems",
        "report_tomorrow_plan",
        "forbidden_today_work_contains",
        "forbidden_problems_contains",
        "forbidden_tomorrow_plan_contains",
    }
    expected = {key: value for key, value in data.items() if key in allowed}
    if not strict_report_expectations:
        expected.pop("blocked_by_gate", None)
        expected.pop("assistant_reply_type", None)
        expected.pop("report_today_work", None)
        expected.pop("report_problems", None)
        expected.pop("report_tomorrow_plan", None)
        if expected.get("raw_text_written") is True:
            expected.pop("raw_text_written", None)
    if "agent2_direct_write" not in expected:
        expected["agent2_direct_write"] = False
    if _is_bare_confirmation_text(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _is_bare_daily_start(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_absurd_or_invalid(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_external_report_edit_request(text) or _looks_like_reimbursement_policy_question(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_historical_daily_mutation_request(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_yesterday_daily_target_without_current_content(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_yesterday_makeup_without_cutoff_context(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_history_daily_read_question(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_pure_case_progress_notice(text) or _looks_like_case_progress_reference_without_detail(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_case_progress_candidate_only(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_future_case_schedule_only(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_non_tomorrow_future_schedule(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_copy_previous_semantics(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_completed_previous_plan_without_content(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_assistant_lookup_request(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_meta_write_permission_question_without_new_content(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_non_substantive_daily_request(text) or _looks_like_business_reference_question(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_business_question_with_time(text) and not _looks_like_mixed_daily_write_with_question(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if (_looks_like_monthly_coordination_followup(text) or _looks_like_process_help_question(text)) and not _looks_like_mixed_daily_write_with_question(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_lifestyle_or_commute_chatter(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_done_or_end_without_business_content(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_case_candidate_detail_only(text):
        expected["agent2_direct_write"] = False
        expected.setdefault("raw_text_written", False)
    if _looks_like_explicit_tomorrow_plan_content(text):
        expected["agent2_direct_write"] = True
        expected.pop("raw_text_written", None)
    if _looks_like_explicit_field_write_request(text):
        expected["agent2_direct_write"] = True
        expected.setdefault("raw_text_written", False)
    if _looks_like_explicit_business_risk_statement(text):
        expected["agent2_direct_write"] = True
        expected.setdefault("raw_text_written", False)
    if _should_allow_daily_write_by_product_semantics(text):
        expected["agent2_direct_write"] = True
        if not _looks_like_mixed_daily_write_with_question(text):
            expected.pop("forbidden_today_work_contains", None)
            expected.pop("forbidden_problems_contains", None)
            expected.pop("forbidden_tomorrow_plan_contains", None)
        else:
            forbidden = _question_fragment_forbidden_expectation(text)
            if forbidden:
                values = list(expected.get("forbidden_today_work_contains") or [])
                if forbidden not in values:
                    values.append(forbidden)
                expected["forbidden_today_work_contains"] = values
        if not _looks_like_explicit_field_write_request(text):
            expected.pop("raw_text_written", None)
    if _looks_like_mixed_daily_write_with_weather_question(text):
        expected["agent2_direct_write"] = True
        expected.setdefault("raw_text_written", False)
        values = list(expected.get("forbidden_tomorrow_plan_contains") or [])
        if "\u5929\u6c14" not in values:
            values.append("\u5929\u6c14")
        expected["forbidden_tomorrow_plan_contains"] = values
    expected.setdefault("fallback_to_legacy", False)
    if expected.get("agent2_direct_write") is False:
        expected.setdefault("raw_text_written", False)
        forbidden_key = _default_forbidden_field(text)
        if forbidden_key:
            values = list(expected.get(forbidden_key) or [])
            if text not in values:
                values.append(text)
            expected[forbidden_key] = values
    _drop_overbroad_forbidden_tokens(expected)
    return expected


def _should_allow_daily_write_by_product_semantics(text: str) -> bool:
    value = str(text or "")
    if _is_bare_confirmation_text(value):
        return False
    if _looks_like_partial_daily_write_with_noise(value):
        return True
    if _looks_like_pure_case_progress_notice(value) or _looks_like_case_progress_reference_without_detail(value):
        return False
    if _looks_like_case_progress_candidate_only(value):
        return False
    if _looks_like_assistant_lookup_request(value):
        return False
    if _looks_like_meta_write_permission_question_without_new_content(value):
        return False
    if _looks_like_non_substantive_daily_request(value) or _looks_like_business_reference_question(value):
        return False
    if _looks_like_mixed_daily_write_with_question(value):
        return True
    if _looks_like_mixed_daily_write_with_weather_question(value):
        return True
    if _looks_like_business_question_with_time(value):
        return False
    if _looks_like_monthly_coordination_followup(value) or _looks_like_process_help_question(value):
        return False
    if _looks_like_lifestyle_or_commute_chatter(value):
        return False
    if _looks_like_done_or_end_without_business_content(value):
        return False
    if _is_bare_daily_start(value):
        return False
    if _looks_absurd_or_invalid(value):
        return False
    if _looks_like_external_report_edit_request(value) or _looks_like_reimbursement_policy_question(value):
        return False
    if _looks_like_historical_daily_mutation_request(value):
        return False
    if _looks_like_yesterday_daily_target_without_current_content(value):
        return False
    if _looks_like_yesterday_makeup_without_cutoff_context(value):
        return False
    if _looks_like_previous_daily_makeup_without_current_work(value):
        return False
    if _looks_like_history_daily_read_question(value):
        return False
    if _looks_like_case_progress_lookup_question(value):
        return False
    if _looks_like_case_progress_candidate_only(value):
        return False
    if _looks_like_non_tomorrow_future_schedule(value):
        return False
    if _looks_like_completed_previous_plan_without_content(value):
        return False
    if _looks_like_case_candidate_detail_only(value):
        return False
    if _looks_like_daily_edit_or_delete(value):
        return True
    if _looks_like_explicit_tomorrow_plan_content(value):
        return True
    if _looks_like_explicit_field_write_request(value):
        return True
    if _looks_like_explicit_business_risk_statement(value):
        return True
    has_time = any(token in value for token in ("今天", "今日", "明天", "明日", "明儿"))
    has_time = has_time or _has_real_chinese_time_signal(value)
    if _looks_like_copy_previous_semantics(value):
        return False
    if not has_time and _has_followup_business_signal(value):
        return True
    if not has_time:
        return False
    if _has_daily_business_signal(value):
        return True
    if any(token in value for token in ("穿啥", "穿什么", "天气", "降温", "吃", "喝", "测试", "机器人")):
        return False
    if any(token in value for token in ("昨天", "昨日", "前一天", "上一个工作日")) and any(
        token in value for token in ("明日计划", "明天计划", "计划", "待办", "安排", "事项")
    ) and any(token in value for token in ("已完成", "完成了", "完成", "做完", "搞定", "审完", "处理完")):
        return True
    if _looks_like_completed_previous_plan_with_content(value):
        return True
    if "出差" in value:
        return True
    if any(token in value for token in ("开庭", "盖章", "用印")) and any(token in value for token in ("案", "法院", "南京", "上海", "材料")):
        return True
    if _has_daily_business_signal(value):
        return True
    return False


def _looks_like_partial_daily_write_with_noise(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if _looks_absurd_or_invalid(value) or _is_bare_daily_start(value):
        return False
    if _looks_like_process_help_question(value) and not _has_clear_daily_statement(value):
        return False
    if any(token in compact for token in ("\u662f\u4e0d\u662f\u8981", "\u8981\u4e0d\u8981", "\u80fd\u4e0d\u80fd", "\u53ef\u4e0d\u53ef\u4ee5")) and not _has_clear_daily_statement(value):
        return False
    has_daily_piece = _has_real_chinese_time_signal(value) and _has_clear_daily_statement(value) and (
        _has_real_chinese_business_signal(value)
        or _has_daily_business_signal(value)
        or any(token in value for token in ("\u7b54\u8fa9\u72b6", "\u5408\u540c", "\u6848\u5377", "\u65b9\u6848", "\u6750\u6599", "\u4f1a\u8bae", "\u8c03\u89e3"))
    )
    if not has_daily_piece:
        return False
    noise_markers = (
        "\u987a\u4fbf\u95ee",
        "\u95ee\u4e0b",
        "\u662f\u4e0d\u662f",
        "\u5565\u65f6\u5019",
        "\u80fd\u4fee\u597d",
        "\u65e5\u62a5\u52a9\u624b",
        "\u633a\u597d\u7528",
        "\u54c8\u54c8",
        "\u6478\u9c7c",
        "\u7cfb\u7edf\u53c8\u5d29",
        "\u7cfb\u7edfbug",
        "IT",
    )
    if any(token in value for token in noise_markers):
        return True
    if "\uff1f" in value or "?" in value:
        return True
    return False


def _has_clear_daily_statement(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    today_markers = ("\u4eca\u5929", "\u4eca\u65e5", "\u4e0a\u5348", "\u4e0b\u5348", "\u665a\u4e0a")
    done_markers = (
        "\u5199\u5b8c",
        "\u5b8c\u6210",
        "\u5904\u7406",
        "\u5ba1",
        "\u5ba1\u6838",
        "\u6838\u4e86",
        "\u5f00\u4e86",
        "\u6c9f\u901a",
        "\u8ddf\u8fdb",
        "\u67e5\u4e86",
        "\u67e5\u9605",
        "\u6574\u7406",
        "\u63d0\u4ea4",
    )
    if any(marker in compact for marker in today_markers) and any(marker in compact for marker in done_markers):
        return True
    tomorrow_plan_markers = ("\u660e\u5929\u8ba1\u5212", "\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u6253\u7b97", "\u660e\u5929\u5f97", "\u660e\u5929\u8981", "\u660e\u65e5\u8981")
    return any(marker in compact for marker in tomorrow_plan_markers)


def _has_followup_business_signal(text: str) -> bool:
    value = str(text or "")
    if not _has_daily_business_signal(value):
        return False
    return any(
        token in value
        for token in (
            "\u8fd8\u6709",
            "\u5bf9\u4e86",
            "\u53e6\u5916",
            "\u63a5\u7740",
            "\u7ee7\u7eed",
            "\u5c31\u662f\u63a5\u7740",
            "\u987a\u4fbf",
            "\u4e0a\u5348",
            "\u4e0b\u5348",
            "\u4e2d\u5348",
            "\u665a\u4e0a",
        )
    )


def _looks_like_assistant_lookup_request(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("帮我查", "帮忙查", "替我查", "帮我找", "查一下", "发链接", "发给我")):
        return False
    return not _has_real_chinese_time_signal(value)


def _is_bare_confirmation_text(text: str) -> bool:
    compact = str(text or "").replace(" ", "").strip("。.!！?？")
    return compact in {"确认", "确定", "好的", "好", "是", "行", "可以"}


def _looks_like_monthly_coordination_followup(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    if any(token in compact for token in ("催一下", "催下", "提醒一下", "提醒下")) and any(
        token in compact for token in ("截止", "到期", "deadline", "没交", "没填")
    ):
        return not _has_real_chinese_business_signal(text)
    return any(token in compact for token in ("催一下没交", "催一下没填", "催下没交", "催下没填", "deadline", "扣绩效"))


def _looks_like_process_help_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if any(token in compact for token in ("\u600e\u4e48\u8d70", "\u600e\u4e48\u63d0", "\u600e\u4e48\u7533\u8bf7", "\u5565\u6750\u6599", "\u4ec0\u4e48\u6750\u6599")) and any(
        token in compact for token in ("\u7528\u5370", "\u7533\u8bf7", "\u7cfb\u7edf", "\u6d41\u7a0b", "\u5408\u540c\u5ba1\u6838", "\u57f9\u8bad")
    ):
        return True
    if not ("?" in value or "？" in value or any(token in compact for token in ("怎么", "如何", "怕搞错"))):
        return False
    return any(token in compact for token in ("怎么提", "怎么申请", "怎么操作", "流程怎么", "系统怎么", "怕搞错")) and any(
        token in compact for token in ("用印", "申请", "系统", "流程", "电子章", "培训资料")
    )


def _looks_like_mixed_daily_write_with_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not value:
        return False
    has_question = (
        "?" in value
        or "\uff1f" in value
        or any(token in compact for token in ("\u600e\u4e48\u8d70", "\u600e\u4e48\u63d0", "\u600e\u4e48\u7533\u8bf7", "\u5565\u6750\u6599", "\u4ec0\u4e48\u6750\u6599"))
        or any(token in compact for token in ("\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b", "\u7ebf\u4e0b\u8fd8\u662f\u7ebf\u4e0a", "\u53bb\u4e0d\u53bb", "\u8981\u4e0d\u8981"))
    )
    if not has_question:
        return False
    has_question_intro = any(token in compact for token in ("\u987a\u4fbf\u95ee", "\u53e6\u5916\u95ee", "\u95ee\u4e0b", "\u95ee\u4e00\u4e0b"))
    has_daily_piece = _has_real_chinese_time_signal(value) and _has_real_chinese_business_signal(value)
    return has_daily_piece and (has_question_intro or _contains_multiple_clauses(value))


def _looks_like_mixed_daily_write_with_weather_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u5929\u6c14", "\u4e0b\u96e8", "\u964d\u6e29")):
        return False
    if not any(token in compact for token in ("\u987a\u4fbf\u95ee", "\u95ee\u4e0b", "\u95ee\u4e00\u4e0b")):
        return False
    return _has_real_chinese_time_signal(value) and _has_real_chinese_business_signal(value)


def _looks_like_external_report_edit_request(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not compact or "\u65e5\u62a5" in compact or "\u65e5\u5fd7" in compact:
        return False
    return any(token in compact for token in ("\u6628\u5929\u7684\u62a5\u544a", "\u6628\u65e5\u7684\u62a5\u544a", "\u524d\u5929\u7684\u62a5\u544a", "\u62a5\u544a")) and any(
        token in compact for token in ("\u7b2c\u4e09\u9875", "\u7b2c3\u9875", "\u6570\u636e\u66f4\u65b0", "\u66f4\u65b0\u6570\u636e", "\u6539\u4e00\u4e0b", "\u4fee\u6539\u4e00\u4e0b")
    )


def _looks_like_yesterday_daily_target_without_current_content(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u6628\u5929\u7684\u65e5\u62a5", "\u6628\u65e5\u7684\u65e5\u62a5", "\u6628\u5929\u65e5\u62a5", "\u6628\u65e5\u65e5\u62a5")):
        return False
    if any(token in compact for token in ("\u4eca\u5929\u7684\u5de5\u4f5c\u8fd8\u6ca1\u5f00\u59cb", "\u4eca\u65e5\u7684\u5de5\u4f5c\u8fd8\u6ca1\u5f00\u59cb", "\u4eca\u5929\u8fd8\u6ca1\u5f00\u59cb")):
        return True
    return any(token in compact for token in ("\u5199\u6628\u5929", "\u5199\u5230\u6628\u5929", "\u52a0\u5230\u6628\u5929")) and not _has_real_chinese_business_signal(value)


def _looks_like_yesterday_makeup_without_cutoff_context(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    return any(token in compact for token in ("\u6628\u5929\u65e5\u62a5\u5fd8\u8bb0\u5199", "\u6628\u65e5\u65e5\u62a5\u5fd8\u8bb0\u5199", "\u8865\u6628\u5929\u65e5\u62a5", "\u8865\u6628\u65e5\u65e5\u62a5")) and any(
        token in compact for token in ("\u8865\u4e00\u4e0b", "\u8865\u4e0b", "\u5fd8\u8bb0\u5199")
    )


def _looks_like_meta_write_permission_question_without_new_content(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u53ef\u4ee5\u5199\u5165\u65e5\u62a5\u5417", "\u80fd\u5199\u5165\u65e5\u62a5\u5417", "\u8981\u4e0d\u8981\u5199\u65e5\u62a5")):
        return False
    return any(token in compact for token in ("\u4eca\u5929\u4e3b\u8981\u5c31\u662f\u8fd9\u4ef6\u4e8b", "\u5c31\u8fd9\u4ef6\u4e8b", "\u8fd9\u4e2a\u4e8b"))


def _looks_like_reimbursement_policy_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    has_question = "?" in value or "\uff1f" in value or any(token in compact for token in ("\u80fd\u62a5\u591a\u5c11", "\u62a5\u591a\u5c11", "\u591a\u5c11"))
    return has_question and any(token in compact for token in ("\u5dee\u65c5\u6807\u51c6", "\u5dee\u65c5", "\u62a5\u9500\u6807\u51c6", "\u62a5\u9500"))


def _looks_like_previous_daily_makeup_without_current_work(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u6628\u5929", "\u6628\u65e5")):
        return False
    if not any(token in compact for token in ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    if not any(token in compact for token in ("\u8865", "\u5fd8\u5199", "\u5fd8\u4e86\u5199", "\u6f0f\u5199")):
        return False
    return not any(token in compact for token in ("\u4eca\u5929", "\u4eca\u65e5", "\u5199\u5230\u4eca\u5929", "\u8bb0\u5230\u4eca\u5929"))


def _looks_like_case_progress_lookup_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u8fdb\u5c55", "\u6848\u5b50", "\u6848\u4ef6", "\u6848")):
        return False
    if not any(token in compact for token in ("\u67e5\u4e0b", "\u67e5\u4e00\u4e0b", "\u80fd\u67e5", "\u770b\u4e0b", "\u770b\u4e00\u4e0b")):
        return False
    return "?" in value or "\uff1f" in value or any(token in compact for token in ("\u80fd\u67e5", "\u67e5\u4e0b\u4e0d", "\u67e5\u4e00\u4e0b\u4e0d"))


def _looks_like_explicit_tomorrow_plan_content(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    return any(token in compact for token in ("\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u660e\u5929\u7684\u8ba1\u5212", "\u660e\u65e5\u7684\u8ba1\u5212")) and any(
        token in compact for token in ("\u7ee7\u7eed", "\u50ac", "\u8ddf\u8fdb", "\u63a8\u8fdb", "\u5904\u7406", "\u6c9f\u901a", "\u786e\u8ba4")
    )


def _looks_like_explicit_field_write_request(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if any(token in compact for token in ("\u4f5c\u4e3a\u95ee\u9898\u8bb0\u4e0a", "\u4f5c\u4e3a\u98ce\u9669\u8bb0\u4e0a", "\u5f53\u6210\u95ee\u9898\u8bb0\u4e0a", "\u5f53\u6210\u98ce\u9669\u8bb0\u4e0a")):
        return True
    return any(token in compact for token in ("\u8bb0\u5230\u4eca\u65e5\u5de5\u4f5c", "\u8bb0\u5230\u4eca\u5929\u5de5\u4f5c", "\u5199\u5230\u660e\u65e5\u8ba1\u5212", "\u5199\u5230\u660e\u5929\u8ba1\u5212"))


def _looks_like_explicit_business_risk_statement(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u98ce\u9669", "\u95ee\u9898", "\u5361\u70b9")):
        return False
    return any(token in compact for token in ("\u5408\u540c", "\u6a21\u677f", "\u5ba2\u6237", "\u7532\u65b9", "\u6cd5\u9662", "\u9879\u76ee", "\u6750\u6599", "\u65b9\u6848", "\u9700\u6c42", "\u5ef6\u671f", "\u672a\u5b9a", "\u6ca1\u5b9a", "\u8981\u6539"))


def _question_fragment_forbidden_expectation(text: str) -> str:
    value = str(text or "")
    parts = [part.strip() for part in re.split(r"(?<=[\uff1f?])|[\uff0c,;\uff1b\u3002\r\n]+", value) if part.strip()]
    for part in parts:
        compact = part.replace(" ", "")
        if "?" in part or "\uff1f" in part or any(token in compact for token in ("\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b", "\u7ebf\u4e0b\u8fd8\u662f\u7ebf\u4e0a", "\u53bb\u4e0d\u53bb", "\u8981\u4e0d\u8981")):
            return part
    return value


def _contains_multiple_clauses(text: str) -> bool:
    parts = [part for part in re.split(r"[\uff0c,;\uff1b\u3002\r\n]+", str(text or "")) if part.strip()]
    return len(parts) >= 2


def _drop_overbroad_forbidden_tokens(expected: dict[str, Any]) -> None:
    for key in ("forbidden_today_work_contains", "forbidden_problems_contains", "forbidden_tomorrow_plan_contains"):
        value = expected.get(key)
        if isinstance(value, str):
            if len(value.strip()) <= 1:
                expected.pop(key, None)
            continue
        if isinstance(value, list):
            filtered = [item for item in value if len(str(item).strip()) > 1]
            if filtered:
                expected[key] = filtered
            else:
                expected.pop(key, None)


def _looks_like_non_substantive_daily_request(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if any(token in compact for token in ("算了不写", "不写了", "不填了")) and any(
        token in compact for token in ("明天写", "明日写", "回头写", "下次写")
    ) and not any(token in value for token in ("合同", "案件", "材料", "报告", "函件", "意见书", "方案", "会议")):
        return True
    if "日报" not in compact:
        return False
    if any(
        token in compact
        for token in (
            "日报都不想写",
            "日报不想写",
            "不想写日报",
            "不写日报",
            "日报明天再说",
            "日报明日再说",
            "明天再说日报",
            "明日再说日报",
            "日报回头再说",
            "今天日报先不写",
            "日报先不写",
            "今天日报不写",
            "随便写",
            "随便填",
            "交差",
            "没啥事",
            "没什么事",
            "没啥内容",
        )
    ):
        return True
    return any(token in compact for token in ("今天写日报了没", "日报写了没")) and any(
        token in compact for token in ("帮我把今天的活儿记一下", "帮我把今天的活记一下", "把今天的活儿记一下")
    )


def _looks_like_business_reference_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not ("?" in value or "？" in value or any(token in compact for token in ("是不是", "是否", "有没有", "有无", "吗"))):
        return False
    if _has_real_chinese_time_signal(value):
        return False
    if _looks_like_process_help_question(value):
        return False
    if not any(token in compact for token in ("是不是", "是否", "有没有", "有无", "吗", "改过", "写的是", "什么情况", "怎么回事")):
        return False
    return any(token in value for token in ("合同", "条款", "协议", "案件", "案", "仲裁", "法院", "材料", "流程"))


def _looks_like_business_question_with_time(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not ("?" in value or "？" in value or any(token in compact for token in ("是不是", "是否", "吗", "要不要", "需不需要"))):
        return False
    return any(token in value for token in ("今天", "今日", "明天", "明日", "后天", "下周")) and any(
        token in value for token in ("合同", "报告", "案件", "案", "法院", "材料", "流程", "客户")
    )


def _has_real_chinese_time_signal(text: str) -> bool:
    value = str(text or "")
    return any(
        token in value
        for token in (
            "\u4eca\u5929",
            "\u4eca\u65e5",
            "\u660e\u5929",
            "\u660e\u65e5",
            "\u660e\u513f",
            "\u660e\u65e9",
            "\u4e0a\u5348",
            "\u4e0b\u5348",
            "\u4e2d\u5348",
            "\u665a\u4e0a",
        )
    )


def _looks_like_historical_daily_mutation_request(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    historical_markers = ("昨天", "昨日", "前天", "前日", "鏄ㄥぉ", "鏄ㄦ棩", "鍓嶅ぉ", "鍓嶆棩")
    daily_markers = ("日报", "日志", "鏃ユ姤", "鏃ュ織")
    mutation_markers = ("写进", "写到", "记进", "记到", "补到", "补进", "修改", "改", "加到", "加进", "加入", "追加", "加", "鍐欒繘", "璁拌繘")
    return (
        any(marker in compact for marker in historical_markers)
        and any(marker in compact for marker in daily_markers)
        and any(marker in compact for marker in mutation_markers)
    )


def _looks_like_history_daily_read_question(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("昨天", "昨日", "前天", "前日")):
        return False
    if not any(token in compact for token in ("写的什么", "写了什么", "日报", "日志")):
        return False
    return any(token in value for token in ("?", "？")) or any(token in compact for token in ("什么", "啥", "看下", "发我"))


def _looks_like_completed_previous_plan_without_content(text: str) -> bool:
    value = str(text or "")
    if any(token in value for token in ("\u4eca\u5929", "\u4eca\u65e5")) and _has_real_chinese_business_signal(value):
        return False
    compact = value.replace(" ", "")
    previous_markers = ("昨天", "昨日", "鏄ㄥぉ", "鏄ㄦ棩")
    plan_markers = ("明日计划", "明天计划", "计划", "待办", "安排", "鏄庢棩璁″垝", "鏄庡ぉ璁″垝", "璁″垝")
    completion_markers = ("完成", "搞定", "做完", "已完成", "已经搞定", "宸插畬鎴", "鎼炲畾")
    explicit_content_markers = ("是", "：", ":", "审", "审核", "处理", "跟进", "沟通", "写", "瀹", "澶勭悊", "璺熻繘")
    return (
        any(marker in compact for marker in previous_markers)
        and any(marker in compact for marker in plan_markers)
        and any(marker in compact for marker in completion_markers)
        and not any(marker in compact for marker in explicit_content_markers)
    )


def _looks_like_completed_previous_plan_with_content(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(marker in compact for marker in ("昨天我说今天要", "昨天说今天要", "昨天提到今天要", "昨日我说今天要", "昨日说今天要")):
        return False
    if not any(marker in compact for marker in ("已经", "已", "完了", "完成", "搞定", "做完", "见完", "审完", "处理完")):
        return False
    return (
        _has_real_chinese_business_signal(value)
        or _has_daily_business_signal(value)
        or any(token in value for token in ("客户", "合同", "案件", "案", "法院", "材料", "律师", "会议", "项目", "业务", "调解", "开庭"))
    )


def _looks_like_non_tomorrow_future_schedule(text: str) -> bool:
    value = str(text or "")
    if any(token in value for token in ("今天", "今日")):
        return False
    if re.search(r"(明天|明日|明儿|明早).{0,24}(出差|去|拜访|盖章|用印|开庭|见|碰面|交|提交|回公司|开会)", value):
        return False
    if not any(token in value for token in ("后天", "大后天", "下周", "下星期", "下礼拜")):
        return False
    return any(token in value for token in ("开庭", "出差", "去", "拜访", "盖章", "用印"))


def _looks_like_case_candidate_detail_only(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if any(token in value for token in ("今天", "今日", "明天", "明日", "上午", "下午", "晚上")):
        return False
    if not any(token in value for token in ("案", "案件")):
        return False
    if not any(token in compact for token in ("开庭", "庭审", "案卷材料", "案卷", "材料")):
        return False
    return any(token in compact for token in ("需要带", "要带", "得带", "带案卷", "带材料", "那个", "就是那个", "对就是"))


def _looks_like_case_progress_candidate_only(text: str) -> bool:
    value = str(text or "")
    if _has_real_chinese_time_signal(value):
        return False
    if any(token in value for token in ("今天", "今日", "明天", "明日", "上午", "下午", "晚上")):
        return False
    if not any(token in value for token in ("案", "案件")):
        return False
    return any(token in value for token in ("新进展", "判决", "裁定", "执行进展", "传票", "排期", "还不上钱", "对方律师"))


def _looks_like_pure_case_progress_notice(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("案", "案件")):
        return False
    if not any(token in compact for token in ("收到法院传票", "收到传票", "法院传票", "开庭时间定", "排期", "判决", "裁定", "执行进展")):
        return False
    return not any(token in compact for token in ("我去", "要去", "出差", "准备", "整理", "写", "提交", "今天处理", "今天跟进"))


def _looks_like_case_progress_reference_without_detail(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    if not any(token in compact for token in ("案的进展", "案件进展")):
        return False
    if not any(token in compact for token in ("写进去", "记进去", "记上", "写上")):
        return False
    return not any(token in compact for token in ("判决", "裁定", "传票", "排期", "开庭", "和解", "调解", "证据", "提交", "收到"))


def _looks_like_future_case_schedule_only(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if not any(token in compact for token in ("\u660e\u5929", "\u660e\u65e5", "\u540e\u5929", "\u4e0b\u5468", "\u5468\u4e00", "\u5468\u4e8c", "\u5468\u4e09", "\u5468\u56db", "\u5468\u4e94")):
        return False
    if not any(token in compact for token in ("\u6848", "\u6848\u5b50", "\u6848\u4ef6")):
        return False
    if not any(token in compact for token in ("\u5f00\u5ead", "\u5ead\u5ba1", "\u6392\u671f")):
        return False
    return not any(token in compact for token in ("\u6211\u53bb", "\u8981\u53bb", "\u51fa\u5dee", "\u53bb\u6cd5\u9662", "\u51c6\u5907", "\u6574\u7406"))


def _looks_absurd_or_invalid(text: str) -> bool:
    if any(token in str(text or "") for token in ("\u5403\u996d\u7761\u89c9\u6253\u8c46\u8c46", "\u6253\u8c46\u8c46", "\u5f53\u795e\u4ed9", "\u4fee\u4ed9", "\u795e\u4ed9")):
        return True
    return any(
        token in str(text or "")
        for token in (
            "火星",
            "月球",
            "外星人",
            "飞船",
            "火箭",
            "宇宙",
            "太空",
            "拯救地球",
            "迪拜塔",
            "直升机",
            "变成了一只",
            "变成一只",
        )
    )


def _looks_like_copy_previous_semantics(text: str) -> bool:
    value = str(text or "")
    if not any(token in value for token in ("昨天", "昨日", "前天", "前日")):
        return False
    if has_previous_to_current_repeat_reference(value):
        return True
    if any(token in value for token in ("复制", "照", "带过来", "拷贝")) and any(token in value for token in ("日报", "计划", "明日计划", "明天计划", "工作")):
        return True
    return any(
        token in value
        for token in (
            "和昨天一样",
            "跟昨天一样",
            "和昨日一样",
            "跟昨日一样",
            "还是昨天",
            "还是昨日",
            "还是那些事",
            "还是那几个",
            "就是昨天那些",
            "昨天那些",
            "没变化",
            "同昨天",
            "同昨日",
            "照昨天",
            "照昨日",
            "接着干",
            "没啥变化",
            "没什么变化",
        )
    )


def _has_daily_business_signal(text: str) -> bool:
    value = str(text or "")
    if _has_real_chinese_business_signal(value):
        return True
    if "审合同" in value or "审了合同" in value:
        return True
    actions = (
        "完成",
        "做了",
        "做",
        "去",
        "写",
        "开了",
        "开会",
        "沟通",
        "讨论",
        "整理",
        "跟进",
        "处理",
        "审核",
        "评审",
        "调研",
        "培训",
        "交",
        "发",
        "看",
        "收到",
        "拟",
        "拟定",
        "同步",
        "扫尾",
        "推进",
    )
    objects = (
        "合同",
        "案件",
        "案",
        "材料",
        "法院",
        "律师",
        "和解",
        "方案",
        "项目",
        "会议",
        "会",
        "纪要",
        "报告",
        "调研",
        "市场",
        "需求",
        "变更",
        "开发",
        "邮件",
        "合规",
        "律所",
        "培训",
        "付款",
        "流程",
        "工作",
    )
    return any(action in value for action in actions) and any(obj in value for obj in objects)


def _has_real_chinese_business_signal(text: str) -> bool:
    value = str(text or "")
    actions = (
        "\u5b8c\u6210",
        "\u505a\u4e86",
        "\u505a",
        "\u53bb",
        "\u5199",
        "\u5f00\u4f1a",
        "\u6c9f\u901a",
        "\u8ba8\u8bba",
        "\u6574\u7406",
        "\u8ddf\u8fdb",
        "\u5904\u7406",
        "\u5e26",
        "\u5e26\u4e0a",
        "\u5ba1",
        "\u5ba1\u6838",
        "\u8bc4\u5ba1",
        "\u5bf9\u63a5",
        "\u63a8\u8fdb",
        "\u6838\u5bf9",
        "\u590d\u6838",
        "\u5bf9\u4e00\u904d",
        "\u67e5",
        "\u67e5\u4e86",
        "\u67e5\u9605",
        "\u67e5\u8be2",
        "\u68c0\u7d22",
        "\u8c03\u53d6",
        "\u6838\u67e5",
        "\u770b",
        "\u770b\u4e86",
        "\u5bc4",
        "\u5bc4\u51fa",
        "\u6536\u5230",
        "\u62df",
        "\u62df\u5b9a",
        "\u786e\u8ba4",
        "\u53d1",
        "\u540c\u6b65",
        "\u63a5\u7740",
        "\u7ee7\u7eed",
        "\u5b9a\u7a3f",
        "\u5f00\u5ead",
        "\u51fa\u5ead",
        "\u6c47\u62a5",
        "\u626b\u5c3e",
    )
    objects = (
        "\u5408\u540c",
        "\u6848\u4ef6",
        "\u6848",
        "\u6750\u6599",
        "\u8d44\u6599",
        "\u5ba1\u8ba1",
        "\u4ef2\u88c1",
        "\u6d89\u5916\u4ef2\u88c1",
        "\u88c1\u5b9a\u4e66",
        "\u4fdd\u5168\u88c1\u5b9a\u4e66",
        "\u987a\u4e30\u5355\u53f7",
        "\u6cd5\u9662",
        "\u5f8b\u5e08",
        "\u65b9\u6848",
        "\u9879\u76ee",
        "\u4f1a\u8bae",
        "\u7eaa\u8981",
        "\u62a5\u544a",
        "\u8c03\u7814",
        "\u5e02\u573a",
        "\u9700\u6c42",
        "\u534f\u8bae",
        "\u8865\u5145\u534f\u8bae",
        "\u5408\u89c4",
        "\u5f8b\u6240",
        "\u4ed8\u6b3e",
        "\u6d41\u7a0b",
        "\u5de5\u4f5c",
        "\u6570\u636e",
        "\u5ba2\u6237",
        "\u53d8\u66f4",
        "\u5f00\u53d1",
        "\u90ae\u4ef6",
    )
    return any(action in value for action in actions) and any(obj in value for obj in objects)


def _looks_like_daily_edit_or_delete(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if _looks_like_historical_daily_mutation_request(value):
        return False
    if not any(token in compact for token in ("改成", "改为", "替换成", "替换为", "删掉", "删除", "清空", "去掉")):
        return False
    return any(token in value for token in ("日报", "草稿", "今日工作", "明日计划", "明天计划", "问题", "风险", "‘", "’", "\"", "“", "”"))


def _looks_like_lifestyle_or_commute_chatter(text: str) -> bool:
    value = str(text or "")
    compact = value.replace(" ", "")
    if _has_real_chinese_business_signal(value):
        return False
    return any(
        token in compact
        for token in (
            "地铁",
            "迟到",
            "挤死",
            "热死",
            "热死了",
            "不想动",
            "烦",
            "天气",
            "吃饭",
            "吃",
            "喝",
            "穿啥",
            "穿什么",
            "出去玩",
        )
    )


def _looks_like_done_or_end_without_business_content(text: str) -> bool:
    value = str(text or "")
    compact = re.sub(r"[\s\u3000，,。.!！?？；;：:]+", "", value)
    if not compact:
        return False
    if any(token in compact for token in ("别写", "不要写", "别记", "不要记", "摸鱼", "测试一下")) and any(
        token in compact for token in ("没啥事", "没什么事", "没事", "别写", "不要写", "摸鱼")
    ):
        return True
    if any(token in compact for token in ("没啥特别", "没什么特别", "还是那些事", "就这些", "没了")) and not any(
        token in compact for token in ("合同", "案件", "案", "材料", "资料", "客户", "项目", "会议", "代码", "模块", "调研", "进度")
    ):
        return True
    if _has_real_chinese_business_signal(value) or _has_daily_business_signal(value):
        return False
    if any(token in value for token in ("合同", "案件", "案", "材料", "资料", "客户", "项目", "会议", "代码", "模块", "调研", "进度")):
        return False
    return compact in {
        "没了",
        "没啦",
        "没有了",
        "就这些",
        "没了就这些",
        "就这样",
        "今天完事",
        "今天完事了",
        "搞定了今天完事",
        "今天就这样",
    }


def _is_bare_daily_start(text: str) -> bool:
    compact = re.sub(r"[\s。！!？?，,、；;：:]+", "", str(text or ""))
    compact = re.sub(r"^(好|好的|行|可以|嗯|额|那|那就)", "", compact)
    return compact in {"日报", "写日报", "写个日报", "写日报了", "今天写日报", "今天写日报了", "写日志", "写日志了", "好写日报了", "帮我写日报", "帮我填日报", "开始写日报", "填日报", "填个日报"}


def _normalize_metadata(raw_metadata: Any) -> dict[str, Any]:
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    result = dict(metadata)
    result.setdefault("generated_by", DEFAULT_SOURCE)
    return result


def _default_forbidden_field(text: str) -> str:
    value = str(text or "")
    if any(token in value for token in ("明天", "明日", "后天", "下周", "周一", "周二", "周三", "周四", "周五")):
        return "forbidden_tomorrow_plan_contains"
    if any(token in value for token in ("风险", "问题", "困难", "卡住", "卡点")):
        return "forbidden_problems_contains"
    return "forbidden_today_work_contains"


def _case_dedupe_key(case: dict[str, Any]) -> str:
    return "\n".join(str(turn.get("text") or "") for turn in case.get("turns") or [])


if __name__ == "__main__":
    raise SystemExit(main())
