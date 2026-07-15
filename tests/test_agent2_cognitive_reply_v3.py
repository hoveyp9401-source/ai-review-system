from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agent2.cognitive_reply_v3 import build_cognitive_side_reply_v3


class _Client:
    def __init__(self):
        self.calls = []
        self.settings = SimpleNamespace(
            llm_model="base",
            llm_intent_model="flash",
            llm_high_risk_model="pro",
            llm_intent_thinking=False,
            llm_high_risk_thinking=True,
            llm_intent_timeout_seconds=8.0,
            llm_high_risk_timeout_seconds=15.0,
        )

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return '{"reply":"辛苦了，先休息一下，稍后再继续。"}'


def test_cognitive_side_reply_uses_only_agent2_chat_segment():
    client = _Client()
    decision = SimpleNamespace(
        segments=(
            SimpleNamespace(text="日报记录完成合同审核", intents=("daily_append",)),
            SimpleNamespace(text="今天有点累", intents=("chat",)),
        )
    )

    reply = asyncio.run(
        build_cognitive_side_reply_v3(
            decision=decision,
            llm_client=client,
        )
    )

    assert "辛苦了" in reply
    assert "闲聊" not in reply
    assert "不写入日报" not in reply
    assert len(client.calls) == 1
    assert "今天有点累" in client.calls[0]["user_prompt"]
    assert "完成合同审核" not in client.calls[0]["user_prompt"]


def test_cognitive_side_reply_is_empty_without_a_read_only_side_intent():
    client = _Client()
    decision = SimpleNamespace(
        segments=(SimpleNamespace(text="记录日报", intents=("daily_append",)),)
    )

    assert asyncio.run(
        build_cognitive_side_reply_v3(decision=decision, llm_client=client)
    ) == ""
    assert client.calls == []


def test_daily_report_opening_gets_deterministic_guidance_without_context_hallucination():
    client = _Client()
    decision = SimpleNamespace(
        segments=(SimpleNamespace(text="我要填日报了", intents=("daily_report",)),)
    )

    reply = asyncio.run(
        build_cognitive_side_reply_v3(
            decision=decision,
            llm_client=client,
            context_pack=SimpleNamespace(as_payload=lambda: {"daily_report_date": "2026-07-09"}),
        )
    )

    assert "这句不会写进日报" in reply
    assert "今天具体完成的工作" in reply
    assert "7月9日" not in reply
    assert client.calls == []


def test_weekly_report_opening_never_falls_through_to_chat_or_daily():
    client = _Client()
    decision = SimpleNamespace(
        segments=(SimpleNamespace(text="我想写周报", intents=("weekly_report",)),)
    )

    reply = asyncio.run(
        build_cognitive_side_reply_v3(decision=decision, llm_client=client)
    )

    assert "周报" in reply
    assert "本周完成" in reply
    assert "日报" not in reply
    assert client.calls == []


def test_monthly_report_opening_never_falls_through_to_chat_or_daily():
    client = _Client()
    decision = SimpleNamespace(
        segments=(SimpleNamespace(text="开始写月报", intents=("monthly_report",)),)
    )

    reply = asyncio.run(
        build_cognitive_side_reply_v3(decision=decision, llm_client=client)
    )

    assert "月报" in reply
    assert "本月完成" in reply
    assert "日报" not in reply
    assert client.calls == []


def test_monthly_report_task_exit_does_not_claim_submission_or_completion():
    client = _Client()
    decision = SimpleNamespace(
        segments=(SimpleNamespace(text="月报任务结束了", intents=("monthly_report_exit",)),)
    )

    reply = asyncio.run(
        build_cognitive_side_reply_v3(decision=decision, llm_client=client)
    )

    assert reply == "已退出月报填写。月报内容和提交状态都没有改变。"
    assert "顺利完成" not in reply
    assert client.calls == []
