from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import PerformanceSubmission, PerformanceTask, User
from app.services.dingtalk import DingTalkRobotClient
from app.services.performance_service import PerformanceTaskService, build_performance_reply_prompt, submission_metrics
from app.utils.time import now_in_timezone


PAYLOAD = Path("/tmp/monthly_performance_formal_send_20260701.json")
RESULT = Path("outputs/monthly_performance_formal_send_results_20260701.json")
CREATED_BY = "codex_formal_send_20260701"


def build_overview_markdown(metrics: list[dict[str, Any]]) -> str:
    lines = ["【指标完成情况概览】", ""]
    for metric in metrics:
        lines.append(f"**{metric['name']}**")
        lines.append("")
        for display in metric.get("display_lines") or []:
            text = str(display).strip()
            if text:
                lines.append(text)
                lines.append("")
    return "\n".join(lines).rstrip()


async def main() -> None:
    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    items = payload["items"]
    settings = get_settings()
    robot = DingTalkRobotClient(settings)
    service = PerformanceTaskService(settings)
    send_results: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as session:
        uids = [item["dingtalk_user_id"] for item in items]
        active = (
            await session.execute(
                select(User, PerformanceSubmission, PerformanceTask)
                .join(PerformanceSubmission, PerformanceSubmission.user_id == User.id)
                .join(PerformanceTask, PerformanceSubmission.task_id == PerformanceTask.id)
                .where(
                    User.dingtalk_user_id.in_(uids),
                    PerformanceSubmission.status.in_(["collecting", "pending_confirmation"]),
                    PerformanceTask.status == "active",
                )
            )
        ).all()
        if active:
            conflicts = [
                {
                    "name": user.name,
                    "uid": user.dingtalk_user_id,
                    "title": task.title,
                    "submission_id": str(submission.id),
                }
                for user, submission, task in active
            ]
            raise RuntimeError("active task exists: " + json.dumps(conflicts, ensure_ascii=False))

        for item in items:
            user = (
                await session.execute(
                    select(User).where(User.dingtalk_user_id == item["dingtalk_user_id"], User.active.is_(True))
                )
            ).scalar_one_or_none()
            if user is None:
                send_results.append(
                    {
                        "unit_name": item["unit_name"],
                        "leader": item["leader"],
                        "uid": item["dingtalk_user_id"],
                        "sent": False,
                        "error": "user not found",
                    }
                )
                continue

            task = await service.create_blank_task(
                session,
                title=item["title"],
                period_label=item["period_label"],
                metrics=item["metrics"],
                recipients=[user],
                created_by=CREATED_BY,
            )
            await session.commit()

            submission = (
                await session.execute(
                    select(PerformanceSubmission).where(
                        PerformanceSubmission.task_id == task.id,
                        PerformanceSubmission.user_id == user.id,
                    )
                )
            ).scalar_one()
            metrics = submission_metrics(submission)
            overview = build_overview_markdown(metrics)
            reply = build_performance_reply_prompt(metrics)
            message_results: list[dict[str, Any]] = []

            try:
                if item.get("send_overview", True):
                    overview_result = await robot.send_robot_direct_markdown(
                        user_ids=[user.dingtalk_user_id],
                        title="指标完成情况概览",
                        text=overview,
                    )
                    message_results.append({"type": "overview_markdown", "sent": True, "result": overview_result})
                else:
                    message_results.append(
                        {
                            "type": "overview_markdown",
                            "sent": False,
                            "skipped": True,
                            "reason": "reply_prompt_only",
                        }
                    )

                reply_result = await robot.send_robot_direct_text(user_ids=[user.dingtalk_user_id], text=reply)
                message_results.append({"type": "reply_prompt_text", "sent": True, "result": reply_result})

                snapshot = dict(submission.sent_snapshot_json or {})
                snapshot.update(
                    {
                        "formal_send": True,
                        "unit_name": item["unit_name"],
                        "source_file": item.get("source_file", ""),
                        "defendant_source_file": item.get("defendant_source_file", ""),
                        "messages": {
                            "overview_markdown": overview if item.get("send_overview", True) else "",
                            "reply_prompt": reply,
                            "overview_skipped": not item.get("send_overview", True),
                        },
                    }
                )
                submission.sent_snapshot_json = snapshot
                submission.last_prompted_at = now_in_timezone(getattr(user, "timezone", "Asia/Shanghai"))
                await session.commit()
                send_results.append(
                    {
                        "unit_name": item["unit_name"],
                        "leader": user.name,
                        "uid": user.dingtalk_user_id,
                        "task_id": str(task.id),
                        "submission_id": str(submission.id),
                        "sent": True,
                        "messages": message_results,
                    }
                )
            except Exception as exc:
                await session.rollback()
                send_results.append(
                    {
                        "unit_name": item["unit_name"],
                        "leader": user.name,
                        "uid": user.dingtalk_user_id,
                        "task_id": str(task.id),
                        "submission_id": str(submission.id),
                        "sent": False,
                        "messages": message_results,
                        "error": str(exc)[:500],
                    }
                )

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(
        json.dumps({"created_by": CREATED_BY, "period_label": payload.get("period_label"), "results": send_results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "total": len(send_results),
                "sent": sum(1 for result in send_results if result.get("sent")),
                "failed": [result for result in send_results if not result.get("sent")],
                "result_file": str(RESULT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
