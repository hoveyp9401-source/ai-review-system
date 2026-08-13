from dataclasses import replace
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.config import Settings
from app.services.dingtalk import (
    DingTalkPayloadError,
    normalize_dingtalk_conversation_kind,
    parse_incoming_message,
)


def _settings(**changes):
    return replace(
        Settings(_env_file=None),
        **changes,
    )


def test_dingtalk_parser_preserves_provider_conversation_kind():
    direct = parse_incoming_message(
        {
            "senderStaffId": "ding-user",
            "msgId": "m-1",
            "conversationId": "cid-1",
            "conversationType": "1",
            "isInAtList": True,
            "msgtype": "text",
            "text": {"content": "填写下周计划"},
        }
    )

    assert direct.conversation_kind == "direct"
    assert direct.bot_was_mentioned is True


def test_dingtalk_parser_marks_group_without_guessing_it_is_private():
    group = parse_incoming_message(
        {
            "senderStaffId": "ding-user",
            "conversationType": "2",
            "msgtype": "text",
            "text": {"content": "填写下周计划"},
        }
    )

    assert group.conversation_kind == "group"


def test_dingtalk_parser_keeps_unknown_when_provider_omits_scene():
    unknown = parse_incoming_message(
        {
            "senderStaffId": "ding-user",
            "msgtype": "text",
            "text": {"content": "填写下周计划"},
        }
    )

    assert unknown.conversation_kind == "unknown"


def test_unknown_conversation_type_is_rejected_instead_of_guessed():
    with pytest.raises(DingTalkPayloadError, match="conversation type"):
        parse_incoming_message(
            {
                "senderStaffId": "ding-user",
                "conversationType": "unexpected",
                "msgtype": "text",
                "text": {"content": "填写下周计划"},
            }
        )


def test_stream_conversation_kind_uses_the_same_normalizer():
    assert normalize_dingtalk_conversation_kind(1) == "direct"
    assert normalize_dingtalk_conversation_kind("2") == "group"
    assert normalize_dingtalk_conversation_kind(None) == "unknown"


def test_trusted_principal_preserves_private_transport_fact():
    request = TrustedContextRequest(
        tenant_id="tenant-a",
        user_id=uuid4(),
        conversation_id="cid-1",
        source_message_id="msg-1",
        timezone="Asia/Shanghai",
        server_now=datetime(
            2026,
            8,
            13,
            9,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        conversation_kind="direct",
    )

    assert request.principal().conversation_kind == "direct"


def test_weekly_plan_flags_are_closed_and_allowlists_are_empty_by_default():
    settings = Settings(_env_file=None)

    assert settings.agent2_weekly_plan_enabled is False
    assert settings.agent2_weekly_plan_write_enabled is False
    assert settings.agent2_weekly_plan_send_enabled is False
    assert settings.agent2_weekly_plan_tenant_allowlist == ""
    assert settings.agent2_weekly_plan_user_allowlist == ""
    assert settings.agent2_weekly_plan_send_user_allowlist == ""
