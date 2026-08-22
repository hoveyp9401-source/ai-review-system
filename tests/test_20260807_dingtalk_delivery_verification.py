from types import SimpleNamespace

import pytest

from app.scheduler.jobs import send_user_message
from app.services.dingtalk import (
    DingTalkDeliveryError,
    DingTalkOutboundContentError,
    DingTalkRobotClient,
    validate_dingtalk_outbound_text,
)


@pytest.mark.asyncio
async def test_direct_message_is_recorded_only_after_delivery_confirmation():
    class Robot:
        async def send_robot_direct_text(self, **kwargs):
            return {
                "processQueryKey": "direct-1",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": kwargs["user_ids"],
            }

    evidence = await send_user_message(Robot(), ["user-1"], "hello")

    assert evidence.channel == "direct_robot"
    assert evidence.provider_reference == "direct-1"
    assert evidence.message_status == "delivered"


@pytest.mark.asyncio
async def test_scheduled_message_prefers_verified_send_without_changing_chat_send():
    class Robot:
        regular_send_called = False
        verified_send_called = False

        async def send_robot_direct_text(self, **kwargs):
            self.regular_send_called = True
            raise AssertionError("scheduled notifications must use verified delivery")

        async def send_robot_direct_text_verified(self, **kwargs):
            self.verified_send_called = True
            return {
                "processQueryKey": "direct-verified-1",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": kwargs["user_ids"],
            }

    robot = Robot()
    evidence = await send_user_message(robot, ["user-1"], "hello")

    assert evidence.provider_reference == "direct-verified-1"
    assert robot.verified_send_called is True
    assert robot.regular_send_called is False


@pytest.mark.asyncio
async def test_unverified_direct_message_does_not_fall_back_and_duplicate():
    class Robot:
        work_notification_called = False

        async def send_robot_direct_text(self, **kwargs):
            return {"processQueryKey": "direct-1"}

        async def send_work_notification(self, **kwargs):
            self.work_notification_called = True
            return {
                "task_id": "work-1",
                "deliveryVerified": True,
            }

    robot = Robot()

    evidence = await send_user_message(robot, ["user-1"], "hello")

    assert evidence.channel == "direct_robot"
    assert evidence.provider_reference == "direct-1"
    assert evidence.message_status == "accepted_by_provider"
    assert evidence.delivery_verified is False
    assert robot.work_notification_called is False


@pytest.mark.asyncio
async def test_failed_direct_request_uses_verified_work_notification():
    class Robot:
        async def send_robot_direct_text(self, **kwargs):
            raise RuntimeError("direct robot unavailable")

        async def send_work_notification(self, **kwargs):
            return {
                "task_id": "work-1",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": kwargs["user_ids"],
            }

    evidence = await send_user_message(Robot(), ["user-1"], "hello")

    assert evidence.channel == "work_notification"
    assert evidence.provider_reference == "work-1"
    assert evidence.message_status == "delivered"


def _settings():
    return SimpleNamespace(
        dingtalk_api_base_url="https://api.example.invalid",
        dingtalk_oapi_base_url="https://oapi.example.invalid",
        dingtalk_app_key="robot-code",
        dingtalk_robot_code="robot-code",
    )


@pytest.mark.asyncio
async def test_direct_message_uses_robot_code_separate_from_app_key():
    observed = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"processQueryKey": "accepted-separated-robot"}

    settings = _settings()
    settings.dingtalk_robot_code = "active-robot-code"
    client = DingTalkRobotClient(settings)
    client.get_enterprise_access_token = lambda: None

    async def access_token():
        return "token"

    async def post(*args, **kwargs):
        observed["send_json"] = kwargs["json"]
        return Response()

    async def get(*args, **kwargs):
        observed["status_params"] = kwargs["params"]
        return Response()

    client.get_enterprise_access_token = access_token
    client._client.post = post
    client._client.get = get
    try:
        await client.send_robot_direct_text(user_ids=["staff-1"], text="hello")
        await client.get_robot_direct_message_status(
            process_query_key="accepted-separated-robot"
        )
    finally:
        await client.close()

    assert observed["send_json"]["robotCode"] == "active-robot-code"
    assert observed["status_params"]["robotCode"] == "active-robot-code"


@pytest.mark.asyncio
async def test_direct_message_does_not_fall_back_to_app_key_as_robot_code():
    settings = _settings()
    settings.dingtalk_robot_code = ""
    client = DingTalkRobotClient(settings)

    async def unexpected_token():
        raise AssertionError("missing robot code must fail before token or provider call")

    client.get_enterprise_access_token = unexpected_token
    try:
        with pytest.raises(ValueError, match="robot code"):
            await client.send_robot_direct_text(
                user_ids=["staff-1"],
                text="hello",
            )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "text",
    [
        "????????",
        "????????\n\n正常正文",
        "通知标题\n\ufffd",
    ],
)
def test_outbound_content_guard_rejects_encoding_corruption(text):
    with pytest.raises(DingTalkOutboundContentError):
        validate_dingtalk_outbound_text(text)


def test_outbound_content_guard_allows_normal_chinese_and_questions():
    text = "【全员首句预览】\n\n你好，今天有什么进展？？"

    assert validate_dingtalk_outbound_text(text) == text


@pytest.mark.asyncio
async def test_corrupted_direct_message_is_blocked_before_provider_call():
    client = DingTalkRobotClient(_settings())

    async def unexpected_post(*args, **kwargs):
        raise AssertionError("corrupted content must not reach DingTalk")

    client._client.post = unexpected_post
    try:
        with pytest.raises(DingTalkOutboundContentError):
            await client.send_robot_direct_text(
                user_ids=["user-1"],
                text="????????",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_interactive_chat_send_does_not_wait_for_notification_receipt():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"processQueryKey": "accepted-chat-1"}

    client = DingTalkRobotClient(_settings())

    async def access_token():
        return "token"

    async def post(*args, **kwargs):
        return Response()

    async def unexpected_verification(*args, **kwargs):
        raise AssertionError("interactive replies must not wait for receipt polling")

    client.get_enterprise_access_token = access_token
    client._client.post = post
    client._attach_direct_delivery_verification = unexpected_verification
    try:
        result = await client.send_robot_direct_text(
            user_ids=["user-1"],
            text="hello",
        )
    finally:
        await client.close()

    assert result["processQueryKey"] == "accepted-chat-1"


@pytest.mark.asyncio
async def test_direct_delivery_requires_expected_recipient():
    client = DingTalkRobotClient(_settings())

    async def status(**kwargs):
        return {
            "sendStatus": "SUCCESS",
            "messageReadInfoList": [
                {"userId": "other-user", "readStatus": "UNREAD"}
            ],
        }

    client.get_robot_direct_message_status = status
    try:
        with pytest.raises(DingTalkDeliveryError, match="missing recipients"):
            await client.wait_for_robot_direct_delivery(
                process_query_key="query-1",
                expected_user_ids=["user-1"],
                attempts=1,
                interval_seconds=0,
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_work_notification_rejects_invalid_recipient():
    client = DingTalkRobotClient(_settings())

    async def result(**kwargs):
        return {
            "errcode": 0,
            "send_result": {
                "invalid_user_id_list": ["user-1"],
                "read_user_id_list": [],
                "unread_user_id_list": [],
            },
        }

    client.get_work_notification_send_result = result
    try:
        with pytest.raises(DingTalkDeliveryError, match="rejected recipients"):
            await client.wait_for_work_notification_delivery(
                task_id="work-1",
                expected_user_ids=["user-1"],
                attempts=1,
                interval_seconds=0,
            )
    finally:
        await client.close()
