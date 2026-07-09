import asyncio
from types import SimpleNamespace
import json

from app.agent2.assistant_responder import AssistantReply
from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.context_pack import KnowledgeEvidenceFrame, build_agent2_context_pack
from app.agent2.personal_memory import build_personal_memory_profile
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_CHAT, WORKFLOW_INTERNAL_QA, WORKFLOW_LEGAL_RESEARCH


class FakeLLMClient:
    def __init__(self, output: str | Exception):
        self.output = output
        self.calls: list[dict] = []
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
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def test_tool_reply_uses_llm_for_legal_research_and_prefixes_non_daily_notice():
    client = FakeLLMClient('{"reply":"\\u521d\\u6b65\\u770b\\uff0c\\u9700\\u5148\\u6838\\u5bf9\\u6743\\u5229\\u57fa\\u7840\\u3002"}')
    assistant_reply = AssistantReply(
        reply_type="legal_research",
        workflow=WORKFLOW_LEGAL_RESEARCH,
        text="\u8fd9\u53e5\u6211\u6309\u3010\u6cd5\u5f8b\u7814\u7a76\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002",
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u5e2e\u6211\u7814\u7a76\u4e00\u4e0b\u4f18\u5148\u53d7\u507f\u6743",
            assistant_reply=assistant_reply,
            llm_client=client,
        )
    )

    assert result.source == "llm"
    assert result.fallback_used is False
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in result.text
    assert "\u6743\u5229\u57fa\u7840" in result.text
    assert client.calls[0]["model"] == "pro"
    assert client.calls[0]["timeout_seconds"] == 30.0


def test_tool_reply_uses_llm_for_internal_qa_with_flash_model():
    client = FakeLLMClient('{"reply":"\\u8fd9\\u53e5\\u4e0d\\u5199\\u5165\\u65e5\\u62a5\\u3002\\n\\u5efa\\u8bae\\u5148\\u627e\\u884c\\u653f\\u786e\\u8ba4\\u5236\\u5ea6\\u53e3\\u5f84\\u3002"}')
    assistant_reply = AssistantReply(
        reply_type="internal_qa",
        workflow=WORKFLOW_INTERNAL_QA,
        text="\u8fd9\u4e2a\u6211\u6309\u5185\u90e8\u95ee\u7b54\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002",
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u5370\u7ae0\u501f\u7528\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f",
            assistant_reply=assistant_reply,
            llm_client=client,
        )
    )

    assert result.source == "llm"
    assert result.fallback_used is False
    assert "\u884c\u653f" in result.text
    assert client.calls[0]["model"] == "flash"


def test_tool_reply_prompt_includes_context_pack_when_provided():
    client = FakeLLMClient('{"reply":"\\u8fd9\\u53e5\\u4e0d\\u5199\\u5165\\u65e5\\u62a5\\u3002\\n\\u5f53\\u524d\\u8349\\u7a3f\\u4e2d\\u7b2c5\\u6761\\u662f\\u95ee\\u9898/\\u98ce\\u9669\\u7b2c2\\u6761\\u3002"}')
    assistant_reply = AssistantReply(
        reply_type="internal_qa",
        workflow=WORKFLOW_INTERNAL_QA,
        text="\u8fd9\u4e2a\u6211\u6309\u5185\u90e8\u95ee\u7b54\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002",
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="\u5e9e\u6d69",
        dingtalk_user_id="0515246015778891",
        source="test",
        raw_text="\u7b2c5\u6761\u662f\u4ec0\u4e48\uff1f",
    )
    report = SimpleNamespace(
        id="report-1",
        report_date="2026-07-03",
        status="collecting",
        today_work=["\u5408\u540c\u5ba1\u6838", "\u51fd\u4ef6\u8d77\u8349", "\u6848\u4ef6\u6c9f\u901a"],
        problems=["\u6750\u6599\u7f3a\u5931", "\u6d41\u7a0b\u5361\u70b9"],
        tomorrow_plan=["\u7ee7\u7eed\u8ddf\u8fdb"],
        section_status={
            "_draft_item_ids": {
                "today_work": ["tw-1", "tw-2", "tw-3"],
                "problems": ["pb-1", "pb-2"],
                "tomorrow_plan": ["tp-1"],
            }
        },
    )
    context_pack = build_agent2_context_pack(envelope, daily_report=report)

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u7b2c5\u6761\u662f\u4ec0\u4e48\uff1f",
            assistant_reply=assistant_reply,
            llm_client=client,
            context_pack=context_pack,
        )
    )

    prompt = json.loads(client.calls[0]["user_prompt"])
    items = prompt["context_pack"]["daily_draft"]["items"]
    assert result.source == "llm"
    assert prompt["context_pack"]["knowledge_status"] == "not_retrieved_or_no_match"
    assert items[4]["field"] == "problems"
    assert items[4]["field_index"] == 2
    assert items[4]["global_index"] == 5
    assert items[4]["item_id"] == "pb-2"


def test_tool_reply_lists_small_case_count_names_even_when_llm_omits_them():
    client = FakeLLMClient('{"reply":"\\u8fd9\\u53e5\\u4e0d\\u5199\\u5165\\u65e5\\u62a5\\u3002\\n\\u6839\\u636e\\u6848\\u4ef6\\u5e95\\u8868\\u7edf\\u8ba1\\uff0c\\u76ee\\u524d\\u672a\\u7ed3\\u6848\\u6848\\u4ef6\\u6570\\u91cf\\u4e3a4\\u4ef6\\u3002"}')
    assistant_reply = AssistantReply(
        reply_type="internal_qa",
        workflow=WORKFLOW_INTERNAL_QA,
        text="\u8fd9\u4e2a\u6211\u6309\u5185\u90e8\u95ee\u7b54\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002",
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="\u5e9e\u6d69",
        dingtalk_user_id="0515246015778891",
        source="test",
        raw_text="\u76ee\u524d\u672a\u7ed3\u6848\u7684\u6709\u51e0\u4ef6\uff1f",
    )
    context_pack = build_agent2_context_pack(
        envelope,
        knowledge=(
            KnowledgeEvidenceFrame(
                source_type="case_table_rag",
                source_id="count:defendant:\u4f55\u7389\u6210:unclosed",
                title="\u4f55\u7389\u6210\u88ab\u544a\u672a\u7ed3\u6848\u6848\u4ef6\u6570\u91cf\u7edf\u8ba1",
                summary="\u6848\u4ef6\u5e95\u8868\u7edf\u8ba1\uff1a\u4f55\u7389\u6210\u88ab\u544a\u672a\u7ed3\u6848\u6848\u4ef6 4 \u4ef6\u3002",
                facts={
                    "assignee_name": "\u4f55\u7389\u6210",
                    "table_label": "\u88ab\u544a",
                    "case_count": 4,
                    "total_case_count": 42,
                    "unclosed_only": True,
                    "sample_case_names": [
                        "\u4e07\u6b23\u57ce\u9879\u76ee\u5408\u540c\u7ea0\u7eb7",
                        "\u52b1\u8302\u5f00\u6267\u884c\u5f02\u8bae\u4e4b\u8bc9",
                        "\u66fe\u7965\u660e\u6267\u884c\u5f02\u8bae\u4e4b\u8bc9",
                        "\u6d77\u5357\u6d77\u82b1\u5c9b\u4e2d\u4ecb\u5408\u540c\u7ea0\u7eb7",
                    ],
                },
                confidence=0.93,
            ),
        ),
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u76ee\u524d\u672a\u7ed3\u6848\u7684\u6709\u51e0\u4ef6\uff1f",
            assistant_reply=assistant_reply,
            llm_client=client,
            context_pack=context_pack,
        )
    )
    assert result.source == "rag_qa"
    assert result.fallback_used is False
    assert "\u5177\u4f53\u6848\u4ef6" in result.text
    assert "1. \u4e07\u6b23\u57ce\u9879\u76ee\u5408\u540c\u7ea0\u7eb7" in result.text
    assert "4. \u6d77\u5357\u6d77\u82b1\u5c9b\u4e2d\u4ecb\u5408\u540c\u7ea0\u7eb7" in result.text
    assert client.calls == []


def test_tool_reply_prefixes_when_non_daily_notice_is_not_at_start():
    client = FakeLLMClient('{"reply":"\\u5efa\\u8bae\\u5148\\u627e\\u884c\\u653f\\u786e\\u8ba4\\u5236\\u5ea6\\u53e3\\u5f84\\u3002\\u8fd9\\u53e5\\u4e0d\\u5199\\u5165\\u65e5\\u62a5\\u3002"}')
    assistant_reply = AssistantReply(
        reply_type="internal_qa",
        workflow=WORKFLOW_INTERNAL_QA,
        text="\u8fd9\u4e2a\u6211\u6309\u5185\u90e8\u95ee\u7b54\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002",
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u5370\u7ae0\u501f\u7528\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f",
            assistant_reply=assistant_reply,
            llm_client=client,
        )
    )

    assert result.text.startswith("\u8fd9\u53e5\u6211\u6309\u3010\u5185\u90e8\u95ee\u7b54\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002")


def test_tool_reply_falls_back_when_llm_times_out():
    client = FakeLLMClient(TimeoutError("timeout"))
    fallback = "\u8fd9\u53e5\u6211\u6309\u3010\u6cd5\u5f8b\u7814\u7a76\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
    assistant_reply = AssistantReply(
        reply_type="legal_research",
        workflow=WORKFLOW_LEGAL_RESEARCH,
        text=fallback,
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u5e2e\u6211\u7814\u7a76\u6700\u65b0\u88c1\u5224\u89c2\u70b9",
            assistant_reply=assistant_reply,
            llm_client=client,
        )
    )

    assert result.text == fallback
    assert result.fallback_used is True
    assert result.error == "TimeoutError"


def test_tool_reply_uses_static_reply_for_small_talk_without_llm():
    client = FakeLLMClient('{"reply":"这句不写入日报。早上好，我在。"}')
    assistant_reply = AssistantReply(
        reply_type="chat",
        workflow=WORKFLOW_CHAT,
        text="\u65e9\uff0c\u8fd9\u53e5\u6211\u4e0d\u5199\u5165\u65e5\u62a5\u3002",
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u65e9\u4e0a\u597d",
            assistant_reply=assistant_reply,
            llm_client=client,
        )
    )

    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in result.text
    assert result.source == "static"
    assert result.fallback_used is True
    assert client.calls == []


def test_tool_reply_keeps_small_talk_static_with_personal_memory():
    client = FakeLLMClient(TimeoutError("timeout"))
    assistant_reply = AssistantReply(
        reply_type="chat",
        workflow=WORKFLOW_CHAT,
        text="我先把这句当闲聊/反馈处理，不写入日报。",
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="庞浩",
        dingtalk_user_id="0515246015778891",
        source="test",
        raw_text="agent2测试结果一坨屎",
    )
    context_pack = build_agent2_context_pack(
        envelope,
        personal_memory=build_personal_memory_profile(
            user=SimpleNamespace(id="user-1", dingtalk_user_id="0515246015778891", name="庞浩"),
            user_habits=[
                SimpleNamespace(
                    habit_type="previous_plan_rollover",
                    trigger_text="昨天计划已完成",
                    meaning="参考昨天明日计划转今日工作",
                    confidence=0.91,
                    evidence_count=5,
                )
            ],
        ),
    )

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="agent2测试结果一坨屎",
            assistant_reply=assistant_reply,
            llm_client=client,
            context_pack=context_pack,
        )
    )

    assert result.source == "static"
    assert result.fallback_used is True
    assert "庞浩" in result.text
    assert "系统体验" in result.text
    assert "不写入日报" in result.text
    assert "历史习惯" in result.text
    assert client.calls == []
