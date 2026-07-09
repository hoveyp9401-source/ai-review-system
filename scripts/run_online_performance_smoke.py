from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.db import AsyncSessionLocal
from app.models import PerformanceSubmission, PerformanceTask, Team, User
from app.services.performance_service import initial_responses, is_performance_reply_candidate, normalize_metrics


TEAM_CODE = "__performance_smoke_team__"
USER_ID = "__performance_smoke_user__"


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value)[:80]


async def ensure_user() -> User:
    async with AsyncSessionLocal() as session:
        team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
        if team is None:
            team = Team(code=TEAM_CODE, name="Performance Smoke", department_name="Performance Smoke", active=True)
            session.add(team)
            await session.flush()
        user = (await session.execute(select(User).where(User.dingtalk_user_id == USER_ID))).scalar_one_or_none()
        if user is None:
            user = User(
                dingtalk_user_id=USER_ID,
                employee_no=f"performance-smoke-{_slug(USER_ID)}",
                name="绩效Smoke负责人",
                team_id=team.id,
                role="member",
                timezone="Asia/Shanghai",
                active=True,
            )
            session.add(user)
            await session.flush()
        await session.commit()
        await session.refresh(user)
        return user


async def cleanup() -> None:
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.dingtalk_user_id == USER_ID))).scalar_one_or_none()
        if user is not None:
            task_ids = (
                await session.execute(select(PerformanceSubmission.task_id).where(PerformanceSubmission.user_id == user.id))
            ).scalars().all()
            if task_ids:
                await session.execute(delete(PerformanceTask).where(PerformanceTask.id.in_(task_ids)))
            await session.delete(user)
        team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
        if team is not None:
            await session.delete(team)
        await session.commit()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_markdown_heading_signal() -> None:
    metrics = normalize_metrics(
        [
            {"metric_no": 1, "name": "国别市场研究及合同示范文本", "unit": "项"},
            {"metric_no": 2, "name": "海外风控及履约风险管理指引", "unit": "项"},
            {"metric_no": 3, "name": "评审效率", "unit": "%"},
            {"metric_no": 4, "name": "能力建设", "unit": "项"},
        ]
    )
    raw_input = """
### 1. 国别市场研究及合同示范文本
未完成原因/存在问题：近期合同评审工作量集中，调研时间被挤占。
下月目标（项）：3项
行动方案：每周固定安排2个工作日开展国别资料整理。

### 2. 海外风控及履约风险管理指引
未完成原因/存在问题：多项目并行推进，案例素材归集不齐全。
下月目标（项）：1项
行动方案：梳理近半年海外项目履约纠纷案例。
"""
    assert_true(
        is_performance_reply_candidate(
            metrics=metrics,
            responses=initial_responses(metrics),
            raw_input=raw_input,
            status="collecting",
        ),
        "markdown heading performance reply should be accepted before daily report routing",
    )


async def run_smoke(base_url: str) -> dict[str, Any]:
    await cleanup()
    assert_markdown_heading_signal()
    user = await ensure_user()
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        health = await client.get("/health")
        assert_true(health.status_code == 200 and health.json() == {"status": "ok"}, "health endpoint is not ok")

        create_resp = await client.post(
            "/performance/tasks/blank",
            json={
                "title": "6月团队绩效填报",
                "period_label": "2026-06",
                "recipient_dingtalk_user_ids": [user.dingtalk_user_id],
                "metrics": [
                    {
                        "metric_no": 1,
                        "name": "诉讼案件收款",
                        "unit": "万元",
                        "display_lines": [
                            "周目标：NA#万元，实际完成：NA#万元，完成率：NA#%；",
                            "月度目标：NA#万元，实际完成：NA#万元，完成率：NA#%；",
                            "年度目标：NA#万元，累计实际完成：NA#万元；",
                        ],
                    },
                    {
                        "metric_no": 2,
                        "name": "终本案件恢复执行到位率",
                        "unit": "%",
                        "display_lines": [
                            "周目标：NA#%，实际完成：NA#%，完成率：NA#%；",
                            "月度目标：NA#%，实际完成：NA#%，完成率：NA#%；",
                            "年度目标：NA#%，累计实际完成：NA#%；",
                        ],
                    },
                    {
                        "metric_no": 3,
                        "name": "未审定诉讼结算增加额",
                        "unit": "万元",
                        "display_lines": [
                            "周目标：NA#万元，实际完成：NA#万元，完成率：NA#%；",
                            "月度目标：NA#万元，实际完成：NA#万元，完成率：NA#%；",
                            "年度目标：NA#万元，累计实际完成：NA#万元；",
                        ],
                    },
                ],
                "send_messages": False,
            },
        )
        create_resp.raise_for_status()
        created = create_resp.json()
        assert_true(created["metric_count"] == 3, "task metric_count mismatch")
        assert_true(created["recipient_count"] == 1, "task recipient_count mismatch")

        one_shot = await client.post(
            "/performance/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": """
1、诉讼案件收款
未完成原因：重点客户付款审批延后。
下月目标：7000万元
行动方案：
1. 每周跟踪重点客户付款节点
2. 逾期回款逐案列清单

2、终本案件恢复执行到位率
原因：部分恢复执行线索仍在核验。
下月目标：0.8%
措施：1. 梳理可恢复案件 2. 推动执行法院沟通
""",
                "source": "performance_online_smoke",
            },
        )
        one_shot.raise_for_status()
        first = one_shot.json()
        assert_true(first["status"] == "collecting", "first reply should still be collecting")
        assert_true("3" in {str(key) for key in first["missing"].keys()}, "metric 3 should be missing after first reply")
        assert_true("当前已完成了【诉讼案件收款】【终本案件恢复执行到位率】的填写" in first["message"], "partial progress should name completed metrics")
        assert_true("请继续填写【未审定诉讼结算增加额】" in first["message"], "partial progress should name remaining metrics")

        third_resp = await client.post(
            "/performance/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": "3 未完成原因：项目结算资料补充较慢。下月目标：800万元。行动方案：1. 对接项目部补齐资料 2. 每周通报推进节点",
                "source": "performance_online_smoke",
            },
        )
        third_resp.raise_for_status()
        second = third_resp.json()
        assert_true(second["status"] == "pending_confirmation", "all metrics should be pending confirmation")
        assert_true(second["missing"] == {}, "no metric should be missing before confirmation")
        assert_true("完整绩效汇报预览" in second["message"], "complete reply should show full report preview")

        preview_replay_resp = await client.post(
            "/performance/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": second["message"],
                "source": "performance_online_smoke",
            },
        )
        preview_replay_resp.raise_for_status()
        preview_replay = preview_replay_resp.json()
        assert_true(preview_replay["status"] == "pending_confirmation", "preview replay should keep pending confirmation")
        assert_true(preview_replay["responses"][0]["next_target"] == "7000万元", "preview replay should preserve metric 1 target")
        assert_true("周目标" not in preview_replay["responses"][0]["next_target"], "preview replay must not parse completion lines as target")

        target_edit_resp = await client.post(
            "/performance/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": "把第2项下月目标改成1%",
                "source": "performance_online_smoke",
            },
        )
        target_edit_resp.raise_for_status()
        target_edit = target_edit_resp.json()
        assert_true(target_edit["status"] == "pending_confirmation", "field edit should keep pending confirmation")
        assert_true(target_edit["responses"][1]["next_target"] == "1%", "field edit should update only metric 2 target")

        replace_resp = await client.post(
            "/performance/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": "把每周通报推进节点改成每日通报推进节点",
                "source": "performance_online_smoke",
            },
        )
        replace_resp.raise_for_status()
        replaced = replace_resp.json()
        assert_true(replaced["status"] == "pending_confirmation", "replacement edit should keep pending confirmation")
        assert_true(
            "每日通报推进节点" in " ".join(replaced["responses"][2]["actions"]),
            "replacement edit should update existing action text",
        )

        confirm_resp = await client.post(
            "/performance/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": "【完整绩效汇报预览】提交",
                "source": "performance_online_smoke",
            },
        )
        confirm_resp.raise_for_status()
        done = confirm_resp.json()
        assert_true(done["status"] == "completed", "confirmation should complete submission")
        assert_true(done["confirmed_by_user"] is True, "submission should be user-confirmed")

    await cleanup()
    return {"task_id": created["task_id"], "status": "PASS"}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    started = time.perf_counter()
    result = await run_smoke(args.base_url)
    summary = {"pass": 1, "fail": 0, "total": 1, "seconds": round(time.perf_counter() - started, 1), **result}
    payload = {"summary": summary}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    print("ONLINE_PERFORMANCE_SMOKE_START")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    if args.output:
        print(f"OUTPUT {args.output}")
    print("ONLINE_PERFORMANCE_SMOKE_END")


if __name__ == "__main__":
    asyncio.run(main())
