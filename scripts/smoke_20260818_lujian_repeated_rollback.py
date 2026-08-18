from __future__ import annotations

import asyncio
import json
from datetime import date

from app.config import get_settings
from app.db import engine
from app.llm.client import LLMClient
from scripts.smoke_20260818_unified_daily_rollback import (
    _residue,
    _run_add_case,
)


SOURCE = (
    "今日工作：\n"
    "1. 日常用印的审核\n"
    "2. 未归档合同催收\n"
    "3. 进行绩效技能的测试\n"
    "4.施工合同归档闭环\n\n"
    "问题风险：暂无\n\n"
    "明日计划：\n"
    "1. 日常用印的审核\n"
    "2. 未归档合同催收\n"
    "3. 绩效技能数据源的梳理"
)

ZHANG_SOURCE = (
    "今日工作: 如东雨润开庭准备、标前评审、被告案件沟通、滁州合同审批、"
    "标前提疑、流程审批，问题无，明日计划：常州续封跟盯、如东雨润开庭、"
    "海安雨润开庭准备、建湖协议草拟、昆山合同评审。"
)

ZUO_SOURCE = (
    "今日工作\n"
    "深业健康城业主已经发起付款流程\n"
    "河南五建开票事宜沟通完毕\n"
    "游亿案件庭后补充材料准备完毕\n"
    "南京中冶案件付款流程，业主已经发起流程\n"
    "分公司晨会\n\n"
    "明天工作\n"
    "分公司晨会\n"
    "红石郡案件北京天润沟通\n"
    "高德款项跟盯\n\n\n"
    "把以上内容整理成今天日报"
)


async def main() -> None:
    initial = await _residue()
    if any(initial.values()):
        raise AssertionError({"preexisting_residue": initial})
    client = LLMClient(get_settings())
    results: list[dict[str, object]] = []
    try:
        for round_number, report_date in enumerate(
            (date(2026, 4, 30), date(2026, 5, 1), date(2026, 5, 2)),
            start=1,
        ):
            results.append(
                await _run_add_case(
                    client,
                    name=f"lujian_repeated_sections_{round_number}",
                    report_date=report_date,
                    text=SOURCE,
                    required={
                        "today_work": (
                            "日常用印",
                            "未归档合同",
                            "绩效技能",
                            "施工合同归档闭环",
                        ),
                        "tomorrow_plan": (
                            "日常用印",
                            "未归档合同",
                            "绩效技能数据源",
                        ),
                    },
                    expected_status="pending_confirmation",
                    expected_empty_fields=("problems",),
                    expected_field_counts={
                        "today_work": 4,
                        "problems": 0,
                        "tomorrow_plan": 3,
                    },
                )
            )
        results.append(
            await _run_add_case(
                client,
                name="zhang_comma_list_empty_problem",
                report_date=date(2026, 5, 3),
                text=ZHANG_SOURCE,
                required={
                    "today_work": (
                        "如东雨润开庭准备",
                        "标前评审",
                        "被告案件沟通",
                        "滁州合同审批",
                        "标前提疑",
                        "流程审批",
                    ),
                    "tomorrow_plan": (
                        "常州续封",
                        "如东雨润开庭",
                        "海安雨润开庭准备",
                        "建湖协议",
                        "昆山合同评审",
                    ),
                },
                expected_status="pending_confirmation",
                expected_empty_fields=("problems",),
                expected_field_counts={
                    "today_work": 6,
                    "problems": 0,
                    "tomorrow_plan": 5,
                },
            )
        )
        results.append(
            await _run_add_case(
                client,
                name="zuo_repeated_meeting",
                report_date=date(2026, 5, 4),
                text=ZUO_SOURCE,
                required={
                    "today_work": (
                        "深业健康城",
                        "河南五建",
                        "游亿案件",
                        "南京中冶",
                        "分公司晨会",
                    ),
                    "tomorrow_plan": (
                        "分公司晨会",
                        "红石郡",
                        "高德款项",
                    ),
                },
                expected_status="collecting",
                expected_field_counts={
                    "today_work": 5,
                    "problems": 0,
                    "tomorrow_plan": 3,
                },
            )
        )
    finally:
        await client.close()
    residue = await _residue()
    if any(residue.values()):
        raise AssertionError({"rollback_residue": residue})
    print(
        json.dumps(
            {
                "status": "pass",
                "passed": len(results),
                "results": results,
                "rollback_residue": residue,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
