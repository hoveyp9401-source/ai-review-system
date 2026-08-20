from __future__ import annotations

import asyncio
import json

from app.agent2.tool_calling.canary_service import (
    _compose_failure_explanation,
)
from app.config import get_settings
from app.llm.client import LLMClient


CASES = (
    {
        "message_received": True,
        "actual_write": False,
        "failure_kind": "source_mismatch",
        "plain_cause": "拆分后的某条内容与用户原话没有完全对应，为避免写错而停止保存",
        "retry_available": True,
        "next_step_kind": "continue_retry",
    },
    {
        "message_received": True,
        "actual_write": False,
        "failure_kind": "response_incomplete",
        "plain_cause": "整理过程在返回完整结果前中断",
        "retry_available": False,
        "next_step_kind": "resend_or_split",
    },
    {
        "message_received": True,
        "actual_write": False,
        "failure_kind": "report_state_changed",
        "plain_cause": "处理期间日报状态发生了变化，为避免覆盖较新的内容而停止操作",
        "retry_available": False,
        "next_step_kind": "reload_then_continue",
    },
)


async def main() -> None:
    client = LLMClient(get_settings())
    try:
        replies = [
            await _compose_failure_explanation(client, facts=facts)
            for facts in CASES
        ]
    finally:
        await client.close()
    for reply in replies:
        if "本次没有写入任何内容" not in reply:
            raise AssertionError({"missing_no_write_fact": reply})
        if any(
            word in reply
            for word in ("JSON", "schema", "tool_call", "错误码")
        ):
            raise AssertionError({"technical_reply": reply})
    print(
        json.dumps(
            {
                "status": "pass",
                "model": "deepseek-v4-flash",
                "replies": replies,
                "dingtalk_send_calls": 0,
                "database_writes": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
