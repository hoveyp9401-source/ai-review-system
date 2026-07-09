from __future__ import annotations

import argparse
import copy
import json
import random
from pathlib import Path
from typing import Any


WORK_ITEMS = [
    "合同审核",
    "诉讼材料整理",
    "律师函起草",
    "项目评审",
    "案件台账更新",
    "印章资料核对",
    "用印流程沟通",
    "债权资料归档",
]
ISSUES = [
    "客户资料回收较慢",
    "供应商回函资料缺失",
    "内部审批节点滞后",
    "业务部门反馈不完整",
    "案件材料口径需统一",
]
PLANS = [
    "继续跟进归档",
    "整理案件材料",
    "完善合同台账",
    "跟进用印流程",
    "推进资料补充",
]
CITIES = ["南京", "扬州", "苏州", "常州", "嘉兴"]
TRAVEL_PURPOSES = ["开庭", "盖章", "处理讨薪", "沟通案件材料"]
MATTERS = ["恒大破产案", "福耀项目案", "工人讨薪案", "苏建院借章事项"]
LEGAL_TOPICS = ["建设工程价款优先受偿权", "破产债权申报逾期", "诉讼时效中断", "执行异议"]
CHAT = ["早啊，今天有点困", "哈哈这个系统终于像点样子了", "辛苦了，先这样", "咖啡救我一下"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate reproducible random Agent2 dialogue tests.")
    parser.add_argument("--seed", type=int, default=2026070301)
    parser.add_argument("--output", default="evals/agent2/dialogues/random_generated_20260703.jsonl")
    parser.add_argument("--mixed", type=int, default=20)
    parser.add_argument("--daily-edit", type=int, default=20)
    parser.add_argument("--other", type=int, default=20)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    cases: list[dict[str, Any]] = []
    cases.extend(_mixed_cases(rng, args.mixed))
    cases.extend(_daily_edit_cases(rng, args.daily_edit))
    cases.extend(_other_cases(rng, args.other))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "seed": args.seed, "cases": len(cases)}, ensure_ascii=False))


def _mixed_cases(rng: random.Random, count: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        work = rng.choice(WORK_ITEMS)
        city = rng.choice(CITIES)
        purpose = rng.choice(TRAVEL_PURPOSES)
        matter = rng.choice(MATTERS)
        legal_topic = rng.choice(LEGAL_TOPICS)
        today: list[str] = []
        problems: list[str] = []
        tomorrow: list[str] = []
        turns: list[dict[str, Any]] = []

        chat_text = rng.choice(CHAT)
        turns.append(
            _turn(
                "t01_chat",
                chat_text,
                {
                    "primary_workflow": "unknown_or_help",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                    "assistant_reply_type": "small_talk",
                    "report_today_work": today,
                },
            )
        )

        daily_text = f"今天完成{work}"
        today.append(daily_text)
        turns.append(
            _turn(
                "t02_daily",
                daily_text,
                _daily_expected(today, problems, tomorrow, target_field="today_work"),
            )
        )

        qa_text = rng.choice(["公司印章借用流程怎么走？", "合同归档流程是什么？", "用印审批需要谁确认？"])
        turns.append(
            _turn(
                "t03_qa",
                qa_text,
                {
                    "primary_workflow": "internal_qa",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                    "assistant_reply_type": "internal_qa",
                    "report_today_work": today,
                    "forbidden_today_work_contains": [qa_text.rstrip("？")],
                },
            )
        )

        travel_text = f"明天去{city}出差{purpose}"
        tomorrow.append(travel_text)
        turns.append(
            _turn(
                "t04_travel",
                travel_text,
                _daily_expected(
                    today,
                    problems,
                    tomorrow,
                    primary_workflow="travel_coordination",
                    target_field="tomorrow_plan",
                    coordination=["daily_entry", "travel_event"],
                    sandbox=["travel_coordination_candidate"],
                ),
            )
        )

        case_text = f"{matter}今天和法院沟通了执行进展"
        today.append(case_text)
        turns.append(
            _turn(
                "t05_case",
                case_text,
                _daily_expected(
                    today,
                    problems,
                    tomorrow,
                    primary_workflow="case_progress",
                    target_field="today_work",
                    coordination=["daily_entry", "case_progress_entry"],
                    sandbox=["case_progress_candidate"],
                ),
            )
        )

        legal_text = f"帮我研究一下{legal_topic}的裁判规则"
        turns.append(
            _turn(
                "t06_legal",
                legal_text,
                {
                    "primary_workflow": "legal_research",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                    "assistant_reply_type": "legal_research",
                    "report_today_work": today,
                    "report_tomorrow_plan": tomorrow,
                    "forbidden_today_work_contains": [legal_topic],
                },
            )
        )

        old_value = work
        new_value = f"{work}及风险条款复核"
        today[0] = today[0].replace(old_value, new_value, 1)
        turns.append(
            _turn(
                "t07_replace",
                f"把{old_value}改成{new_value}",
                _edit_expected(today, problems, tomorrow),
            )
        )

        removed = today.pop(1)
        turns.append(
            _turn(
                "t08_delete",
                "删除第二条",
                _edit_expected(today, problems, tomorrow, forbidden_today=[removed]),
            )
        )

        turns.append(
            _turn(
                "t09_query",
                "发我看下",
                _read_expected(today, problems, tomorrow),
            )
        )

        cases.append({"dialogue_id": f"random_mixed_{index:02d}", "source": "random_generated_20260703", "turns": turns})
    return cases


def _daily_edit_cases(rng: random.Random, count: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        base = rng.sample(WORK_ITEMS, 3)
        today = [f"今天完成{item}" for item in base]
        problems = [rng.choice(ISSUES)]
        tomorrow = [f"明天{rng.choice(PLANS)}"]
        previous = {
            "today_work": [f"昨天完成{item}" for item in rng.sample(WORK_ITEMS, 3)],
            "problems": [rng.choice(ISSUES)],
            "tomorrow_plan": [f"今天{rng.choice(PLANS)}"],
        }
        turns: list[dict[str, Any]] = []

        if index % 4 == 0:
            copy_text = rng.choice(["复制昨天的日报", "把昨天的带过来", "今天和昨天一样"])
            if copy_text == "今天和昨天一样":
                today = list(previous["today_work"])
                copy_target_field = "today_work"
            else:
                today = list(previous["today_work"])
                problems = list(previous["problems"])
                tomorrow = list(previous["tomorrow_plan"])
                copy_target_field = "all"
            turns.append(
                _turn(
                    "t01_copy",
                    copy_text,
                    {
                        "primary_workflow": "daily_report",
                        "agent2_direct_write": True,
                        "expected_commands": ["copy_previous"],
                        "target_field": copy_target_field,
                        "report_today_work": today,
                        "report_problems": problems,
                        "report_tomorrow_plan": tomorrow,
                    },
                )
            )
        else:
            turns.append(
                _turn(
                    "t01_query",
                    "发我下",
                    _read_expected(today, problems, tomorrow),
                )
            )

        replace_item = rng.choice(WORK_ITEMS)
        today[1] = f"今天完成{replace_item}并发送业务部门确认"
        turns.append(
            _turn(
                "t02_replace_second",
                f"把第二条改成{today[1]}",
                _edit_expected(today, problems, tomorrow),
            )
        )

        moved = today.pop(2)
        problems.append(moved)
        turns.append(
            _turn(
                "t03_move_third",
                "把第三条移到问题风险",
                _edit_expected(today, problems, tomorrow),
            )
        )

        today = [f"{today[0]}，{today[1]}"]
        turns.append(
            _turn(
                "t04_merge",
                "合并第一条和第二条",
                _edit_expected(today, problems, tomorrow),
            )
        )

        new_plan = f"明天{rng.choice(PLANS)}"
        while new_plan in tomorrow:
            new_plan = f"明天{rng.choice(PLANS)}"
        tomorrow.append(new_plan)
        turns.append(
            _turn(
                "t05_add_plan",
                new_plan,
                _daily_expected(today, problems, tomorrow, target_field="tomorrow_plan"),
            )
        )

        if index % 3 == 0:
            completed_previous_plan = _completed_previous_plan_items(previous["tomorrow_plan"])
            today = _merge_unique(today, completed_previous_plan)
            turns.append(
                _turn(
                    "t06_previous_plan_done",
                    rng.choice(["昨天的明日计划已完成", "昨天待办都完成了", "昨日安排全部搞定"]),
                    {
                        "primary_workflow": "daily_report",
                        "agent2_direct_write": True,
                        "expected_commands": ["complete_previous_plan"],
                        "target_field": "today_work",
                        "report_today_work": today,
                        "report_problems": problems,
                        "report_tomorrow_plan": tomorrow,
                    },
                )
            )
            turns.append(_turn("t07_query", "发我看下", _read_expected(today, problems, tomorrow)))
        elif index % 5 == 0:
            turns.append(
                _turn(
                    "t06_revoke_collecting",
                    "撤回今日日报",
                    {
                        "primary_workflow": "daily_report",
                        "agent2_direct_write": False,
                        "execution_status": "no_change",
                        "expected_commands": ["revoke"],
                        "report_status": "collecting",
                        "report_today_work": today,
                        "report_problems": problems,
                        "report_tomorrow_plan": tomorrow,
                    },
                )
            )
        else:
            turns.append(_turn("t06_query", "发我看下", _read_expected(today, problems, tomorrow)))

        cases.append(
            {
                "dialogue_id": f"random_daily_edit_{index:02d}",
                "source": "random_generated_20260703",
                "metadata": {"initial_report": {"today_work": [f"今天完成{item}" for item in base], "problems": [problems[0]], "tomorrow_plan": [tomorrow[0]]}, "previous_report": previous},
                "active_tasks": [_daily_task(f"random_daily_edit_{index:02d}")],
                "turns": turns,
            }
        )
    return cases


def _other_cases(rng: random.Random, count: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    builders = [
        _other_legal,
        _other_internal_qa,
        _other_weekly,
        _other_monthly,
        _other_small_talk,
        _other_travel,
        _other_case,
        _other_product_daily,
    ]
    for index in range(1, count + 1):
        case = builders[(index - 1) % len(builders)](rng, index)
        cases.append(case)
    return cases


def _other_legal(rng: random.Random, index: int) -> dict[str, Any]:
    topic = rng.choice(LEGAL_TOPICS)
    return {
        "dialogue_id": f"random_other_legal_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                f"再问个法律问题，{topic}有什么后果？",
                {
                    "primary_workflow": "legal_research",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                    "assistant_reply_type": "legal_research",
                    "forbidden_today_work_contains": [topic],
                },
            )
        ],
    }


def _other_internal_qa(rng: random.Random, index: int) -> dict[str, Any]:
    text = rng.choice(["印章借用流程是什么？", "合同归档需要哪些材料？", "用印审批谁来确认？"])
    return {
        "dialogue_id": f"random_other_qa_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                text,
                {
                    "primary_workflow": "internal_qa",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                    "assistant_reply_type": "internal_qa",
                },
            )
        ],
    }


def _other_weekly(rng: random.Random, index: int) -> dict[str, Any]:
    return {
        "dialogue_id": f"random_other_weekly_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                rng.choice(["帮我生成本周周报", "整理一下本周周报", "发我本周周报"]),
                {
                    "primary_workflow": "weekly_report",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                },
            )
        ],
    }


def _other_monthly(rng: random.Random, index: int) -> dict[str, Any]:
    text = "1. 诉讼案件收款（现金）\n未完成原因/存在问题：客户付款审批慢\n下月目标（万元）：100\n行动方案：每周跟进"
    return {
        "dialogue_id": f"random_other_monthly_{index:02d}",
        "source": "random_generated_20260703",
        "active_tasks": [
            {
                "workflow": "monthly_report",
                "task_id": f"monthly-{index:02d}",
                "status": "collecting",
                "reply_candidate": True,
                "awaiting_confirmation": False,
            }
        ],
        "turns": [
            _turn(
                "t01",
                text,
                {
                    "primary_workflow": "monthly_report",
                    "agent2_direct_write": False,
                    "forbidden_today_work_contains": ["客户付款审批慢"],
                },
            )
        ],
    }


def _other_small_talk(rng: random.Random, index: int) -> dict[str, Any]:
    text = rng.choice(CHAT)
    return {
        "dialogue_id": f"random_other_chat_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                text,
                {
                    "primary_workflow": "unknown_or_help",
                    "blocked_by_gate": True,
                    "agent2_direct_write": False,
                    "assistant_reply_type": "small_talk",
                    "forbidden_today_work_contains": [text],
                },
            )
        ],
    }


def _other_travel(rng: random.Random, index: int) -> dict[str, Any]:
    city = rng.choice(CITIES)
    text = f"明天去{city}出差{rng.choice(TRAVEL_PURPOSES)}"
    return {
        "dialogue_id": f"random_other_travel_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                text,
                _daily_expected(
                    [],
                    [],
                    [text],
                    primary_workflow="travel_coordination",
                    target_field="tomorrow_plan",
                    coordination=["daily_entry", "travel_event"],
                    sandbox=["travel_coordination_candidate"],
                ),
            )
        ],
    }


def _other_case(rng: random.Random, index: int) -> dict[str, Any]:
    text = f"{rng.choice(MATTERS)}今天和法院沟通了执行进展"
    return {
        "dialogue_id": f"random_other_case_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                text,
                _daily_expected(
                    [text],
                    [],
                    [],
                    primary_workflow="case_progress",
                    target_field="today_work",
                    coordination=["daily_entry", "case_progress_entry"],
                    sandbox=["case_progress_candidate"],
                ),
            )
        ],
    }


def _other_product_daily(rng: random.Random, index: int) -> dict[str, Any]:
    today = "今天做日报系统优化"
    plan = "明天开始做案件进展与出差协同模块"
    return {
        "dialogue_id": f"random_other_product_daily_{index:02d}",
        "source": "random_generated_20260703",
        "turns": [
            _turn(
                "t01",
                f"{today}，{plan}",
                {
                    "primary_workflow": "daily_report",
                    "agent2_direct_write": True,
                    "expected_commands": ["fill"],
                    "forbidden_sandbox_candidates": ["travel_coordination_candidate", "case_progress_candidate"],
                    "report_today_work": [today],
                    "report_tomorrow_plan": [plan],
                },
            )
        ],
    }


def _turn(turn_id: str, text: str, expected: dict[str, Any]) -> dict[str, Any]:
    return {"turn_id": turn_id, "text": text, "expected": copy.deepcopy(expected)}


def _daily_task(task_id: str) -> dict[str, Any]:
    return {
        "workflow": "daily_report",
        "task_id": task_id,
        "status": "collecting",
        "reply_candidate": True,
        "awaiting_confirmation": False,
    }


def _daily_expected(
    today: list[str],
    problems: list[str],
    tomorrow: list[str],
    *,
    primary_workflow: str = "daily_report",
    target_field: str = "today_work",
    coordination: list[str] | None = None,
    sandbox: list[str] | None = None,
) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "primary_workflow": primary_workflow,
        "agent2_direct_write": True,
        "expected_commands": ["fill"],
        "target_field": target_field,
        "report_today_work": list(today),
        "report_problems": list(problems),
        "report_tomorrow_plan": list(tomorrow),
    }
    if coordination:
        expected["expected_coordination_actions"] = coordination
    if sandbox:
        expected["expected_sandbox_candidates"] = sandbox
    return expected


def _edit_expected(
    today: list[str],
    problems: list[str],
    tomorrow: list[str],
    *,
    forbidden_today: list[str] | None = None,
) -> dict[str, Any]:
    expected = {
        "primary_workflow": "daily_report",
        "agent2_direct_write": True,
        "expected_commands": ["edit"],
        "report_today_work": list(today),
        "report_problems": list(problems),
        "report_tomorrow_plan": list(tomorrow),
    }
    if forbidden_today:
        expected["forbidden_today_work_contains"] = forbidden_today
    return expected


def _read_expected(today: list[str], problems: list[str], tomorrow: list[str]) -> dict[str, Any]:
    return {
        "primary_workflow": "daily_report",
        "agent2_direct_write": False,
        "execution_status": "read_only",
        "expected_commands": ["query_current"],
        "report_today_work": list(today),
        "report_problems": list(problems),
        "report_tomorrow_plan": list(tomorrow),
    }


def _completed_previous_plan_items(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _completion_text_from_previous_plan_item(value)
        if text and text not in result:
            result.append(text)
    return result


def _completion_text_from_previous_plan_item(value: str) -> str:
    text = str(value or "").strip(" \t\r\n ：:，,。；;、\"'“”‘’")
    for prefix in ("明天", "明日", "明儿", "明个", "次日", "后续", "今天", "今日"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    for prefix in ("计划", "安排", "待办", "事项"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    for prefix in ("继续", "持续", "开始做", "开始", "做"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = text.strip(" \t\r\n ：:，,。；;、\"'“”‘’")
    if not text:
        return ""
    if text.startswith("完成") or text.startswith("已完成"):
        return text
    return f"完成{text}"


def _merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    result = list(existing)
    seen = {item for item in result if item}
    for item in incoming:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


if __name__ == "__main__":
    main()
