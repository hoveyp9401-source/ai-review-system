from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings
from app.services.dingtalk import DingTalkRobotClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate DeepSeek Agent2 cases, replay them, and optionally notify DingTalk.")
    parser.add_argument("--count", type=int, default=6000)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--seed", type=int, default=2026070701)
    parser.add_argument("--output-dir", default="evals/agent2/llm_replay_loop")
    parser.add_argument("--model", default=os.getenv("EVAL_GEN_MODEL") or "deepseek-v4-pro")
    parser.add_argument("--base-url", default=os.getenv("EVAL_GEN_BASE_URL") or "https://api.deepseek.com")
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument("--input", default="", help="Existing dialogue JSONL to replay when skipping generation.")
    parser.add_argument("--require-gray-ready", action="store_true")
    parser.add_argument("--notify-user-ids", default=os.getenv("AGENT2_EVAL_NOTIFY_USER_IDS") or "")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_path = Path(args.input) if args.input else output_dir / "generated_dialogues.jsonl"
    reports_dir = output_dir / "daily_execution_reports"

    status = "started"
    summary: dict[str, Any] = {}
    error_text = ""
    try:
        if not args.skip_generation:
            _run(
                [
                    sys.executable,
                    "scripts/generate_agent2_llm_dialogues.py",
                    "--count",
                    str(args.count),
                    "--batch-size",
                    str(args.batch_size),
                    "--seed",
                    str(args.seed),
                    "--output",
                    str(generated_path),
                    "--model",
                    args.model,
                    "--base-url",
                    args.base_url,
                ]
            )
        _run(
            [
                sys.executable,
                "scripts/replay_agent2_daily_execution.py",
                str(generated_path),
                "--output-dir",
                str(reports_dir),
            ]
        )
        summary_path = reports_dir / "daily_execution_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        status = "passed" if summary.get("gray_ready") else "failed"
    except Exception as exc:
        status = "error"
        error_text = str(exc)

    text = build_notification_text(
        status=status,
        generated_path=generated_path,
        reports_dir=reports_dir,
        summary=summary,
        error_text=error_text,
    )
    (output_dir / "latest_loop_summary.txt").write_text(text + "\n", encoding="utf-8")
    (output_dir / "latest_loop_summary.json").write_text(
        json.dumps(
            {
                "status": status,
                "generated_path": str(generated_path),
                "reports_dir": str(reports_dir),
                "summary": summary,
                "error": error_text,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    user_ids = [user_id.strip() for user_id in str(args.notify_user_ids or "").split(",") if user_id.strip()]
    if user_ids:
        asyncio.run(_notify(user_ids, text))

    print(text)
    if args.require_gray_ready and status != "passed":
        return 1
    return 0 if status != "error" else 2


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True)
    if completed.returncode:
        raise RuntimeError(f"command failed with exit code {completed.returncode}: {' '.join(command)}")


def build_notification_text(
    *,
    status: str,
    generated_path: Path,
    reports_dir: Path,
    summary: dict[str, Any],
    error_text: str = "",
) -> str:
    lines = [
        "Agent2 生成式真实回放测试",
        f"状态：{_status_label(status)}",
        f"用例文件：{generated_path}",
        f"报告目录：{reports_dir}",
    ]
    if error_text:
        lines.extend(["", f"错误：{error_text}"])
        return "\n".join(lines)
    if summary:
        lines.extend(
            [
                "",
                f"对话数：{summary.get('total_dialogues', 0)}",
                f"轮次数：{summary.get('total_turns', 0)}",
                f"失败对话：{summary.get('failed_dialogues', 0)}",
                f"不匹配：{summary.get('mismatch_count', 0)}",
                f"风险轮次：{summary.get('risk_turn_count', 0)}",
                f"意外直接写入：{summary.get('unexpected_direct_write_count', 0)}",
                f"回退 legacy：{summary.get('fallback_to_legacy_count', 0)}",
                f"灰测条件：{'满足' if summary.get('gray_ready') else '不满足'}",
            ]
        )
    return "\n".join(lines)


def _status_label(status: str) -> str:
    if status == "passed":
        return "通过"
    if status == "failed":
        return "未通过，需要修复后重跑"
    if status == "error":
        return "执行异常"
    return status


async def _notify(user_ids: list[str], text: str) -> None:
    robot = DingTalkRobotClient(get_settings())
    try:
        await robot.send_robot_direct_text(user_ids=user_ids, text=text)
    finally:
        await robot.close()


if __name__ == "__main__":
    raise SystemExit(main())
