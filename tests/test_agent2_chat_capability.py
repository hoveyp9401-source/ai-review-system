from types import SimpleNamespace

from app.agent2.chat_capability import build_chat_reply
from app.agent2.personal_memory import build_personal_memory_profile
from app.workflows.intake import WORKFLOW_CHAT


def test_chat_capability_never_declares_write_effects():
    reply = build_chat_reply("\u4f60\u90fd\u8bb0\u5f55\u7684\u662f\u4ec0\u4e48\u554a")
    payload = reply.as_payload()

    assert reply.workflow == WORKFLOW_CHAT
    assert reply.write_effects == ()
    assert payload["write_effects"] == []
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in reply.text
    assert "\u53ea\u8bfb\u53c2\u8003" in reply.text


def test_chat_capability_uses_personal_memory_without_authorizing_writes():
    profile = build_personal_memory_profile(
        user=SimpleNamespace(id="user-1", dingtalk_user_id="dt-1", name="\u5e9e\u6d69"),
        user_habits=[
            SimpleNamespace(
                habit_type="previous_plan_rollover",
                trigger_text="\u6628\u5929\u8ba1\u5212\u5df2\u5b8c\u6210",
                meaning="\u53c2\u8003\u6628\u5929\u660e\u65e5\u8ba1\u5212\u8f6c\u4eca\u65e5\u5de5\u4f5c",
                confidence=0.91,
                evidence_count=5,
            )
        ],
    )
    context_pack = SimpleNamespace(personal_memory=profile)

    reply = build_chat_reply(
        "agent2\u6d4b\u8bd5\u7ed3\u679c\u4e00\u5768\u5c4e",
        context_pack=context_pack,
    )

    assert "\u5e9e\u6d69" in reply.text
    assert "\u7cfb\u7edf\u4f53\u9a8c" in reply.text
    assert "\u5386\u53f2\u4e60\u60ef" in reply.text
    assert reply.write_effects == ()
    assert "user_has_previous_plan_rollover_habit" in reply.memory_notes


def test_chat_capability_keeps_greeting_read_only():
    reply = build_chat_reply("\u65e9\u4e0a\u597d")

    assert reply.workflow == WORKFLOW_CHAT
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in reply.text
    assert reply.write_effects == ()


def test_chat_capability_replies_like_conversation_for_chat_request_and_vent():
    chat = build_chat_reply("\u53ef\u4ee5\u548c\u6211\u804a\u804a\u5417\uff1f")
    vent = build_chat_reply("\u5988\u7684")

    assert "\u53ef\u4ee5\u8bf4\u4e24\u53e5" in chat.text
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in chat.text
    assert any(marker in vent.text for marker in ("\u6211\u542c\u5230\u4e86", "\u61c2", "\u6536\u5230"))
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in vent.text
    assert chat.write_effects == ()
    assert vent.write_effects == ()


def test_chat_capability_uses_rotating_task_redirects_without_llm():
    replies = {build_chat_reply("\u54ce").text for _ in range(12)}

    assert len(replies) >= 2
    assert all("\u4e0d\u5199\u5165\u65e5\u62a5" in reply for reply in replies)
    assert build_chat_reply("\u54c8\u54e6").write_effects == ()


def test_chat_capability_daily_meta_status_is_lightweight_and_read_only():
    reply = build_chat_reply("\u5199\u65e5\u62a5\u4e86")

    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in reply.text or "\u4e0d\u5165\u5e93" in reply.text
    assert any(marker in reply.text for marker in ("\u4f60\u8bf4", "\u5177\u4f53", "\u4eca\u5929\u505a\u4e86\u4ec0\u4e48", "\u4eca\u65e5\u5de5\u4f5c"))
    assert "\u5199\u65e5\u62a5\u4e86" not in reply.write_effects
    assert reply.write_effects == ()


def test_chat_capability_vulgar_joke_is_random_but_never_written():
    replies = {build_chat_reply("\u4eca\u5929\u5403\u5c4e").text for _ in range(12)}

    assert len(replies) >= 2
    assert all("\u4e0d\u5199\u5165\u65e5\u62a5" in reply or "\u4e0d\u5165\u5e93" in reply for reply in replies)
    assert all(any(marker in reply for marker in ("\u4eca\u5929", "\u6b63\u7ecf", "\u5de5\u4f5c", "\u65e5\u62a5")) for reply in replies)
    assert build_chat_reply("\u4eca\u5929\u5403\u5c4e").write_effects == ()
