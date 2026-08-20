from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent2.tool_calling.canary_service import (
    _compose_failure_explanation,
    _failure_explanation_facts,
)


class _ModelFailure(RuntimeError):
    def __init__(self, message: str, *, finish_reason: str) -> None:
        super().__init__(message)
        self.model_turns = (
            SimpleNamespace(
                response_metadata={"finish_reason": finish_reason}
            ),
        )


def test_failure_facts_explain_an_incomplete_model_response_plainly() -> None:
    facts = _failure_explanation_facts(
        _ModelFailure(
            "DeepSeek returned an invalid focused Daily decision",
            finish_reason="length",
        ),
        retry_available=False,
    )

    assert facts == {
        "message_received": True,
        "actual_write": False,
        "failure_kind": "response_incomplete",
        "plain_cause": "整理过程在返回完整结果前中断",
        "retry_available": False,
        "next_step_kind": "resend_or_split",
    }


def test_failure_facts_explain_source_mismatch_without_error_codes() -> None:
    facts = _failure_explanation_facts(
        RuntimeError(
            "focused Daily source validation failed: "
            "DAILY_ITEM_EXACT_QUOTE_MISMATCH"
        ),
        retry_available=True,
    )

    assert facts["failure_kind"] == "source_mismatch"
    assert "原话" in facts["plain_cause"]
    assert "DAILY_ITEM" not in facts["plain_cause"]
    assert facts["next_step_kind"] == "continue_retry"


@pytest.mark.asyncio
async def test_flash_composes_a_plain_actionable_failure_reply() -> None:
    class FakeClient:
        async def complete_json(self, **kwargs):
            assert kwargs["thinking_enabled"] is False
            assert kwargs["max_retries"] == 0
            facts = json.loads(kwargs["user_prompt"])
            assert facts["failure_kind"] == "review_incomplete"
            return json.dumps(
                {
                    "acknowledgement": "我已收到这条日报。",
                    "explanation": "最后检查没有完整结束。",
                },
                ensure_ascii=False,
            )

    reply = await _compose_failure_explanation(
        FakeClient(),
        facts={
            "message_received": True,
            "actual_write": False,
            "failure_kind": "review_incomplete",
            "plain_cause": "内容已经整理出来，但保存前的最终检查没有给出完整结论",
            "retry_available": True,
            "next_step_kind": "continue_retry",
        },
    )

    assert "我已收到这条日报" in reply
    assert "本次没有写入任何内容" in reply
    assert "不用重新发送" in reply
    assert "继续重试" in reply
    assert "JSON" not in reply


@pytest.mark.asyncio
async def test_unsafe_failure_claim_falls_back_to_server_facts() -> None:
    class FakeClient:
        async def complete_json(self, **kwargs):
            del kwargs
            return json.dumps(
                {
                    "acknowledgement": "消息已经收到。",
                    "explanation": "内容已经保存。",
                },
                ensure_ascii=False,
            )

    reply = await _compose_failure_explanation(
        FakeClient(),
        facts={
            "message_received": True,
            "actual_write": False,
            "failure_kind": "processing_interrupted",
            "plain_cause": "本次处理在完成保存前意外中断",
            "retry_available": False,
            "next_step_kind": "resend_or_split",
        },
    )

    assert "已经保存" not in reply
    assert "本次没有写入任何内容" in reply
    assert "分成两三段" in reply
