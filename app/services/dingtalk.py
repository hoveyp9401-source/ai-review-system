from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import Settings
from app.message_identity import canonical_dingtalk_idempotency_key


@dataclass(frozen=True)
class DingTalkIncomingMessage:
    dingtalk_user_id: str
    text: str
    message_id: str | None
    conversation_id: str | None
    source: str
    session_webhook: str | None = None
    conversation_kind: Literal["direct", "group", "unknown"] = "unknown"
    bot_was_mentioned: bool | None = None


class DingTalkPayloadError(ValueError):
    pass


class DingTalkDeliveryError(RuntimeError):
    """The provider accepted a request but did not confirm every recipient."""

    def __init__(
        self,
        message: str,
        *,
        provider_reference: str = "",
        accepted_payload: dict[str, Any] | None = None,
        terminal_failure: bool = False,
        verification_unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider_reference = str(provider_reference or "").strip()
        self.accepted_payload = dict(accepted_payload or {})
        self.terminal_failure = bool(terminal_failure)
        self.verification_unavailable = bool(verification_unavailable)


class DingTalkOutboundContentError(ValueError):
    """Outbound text appears to have been corrupted before DingTalk delivery."""


def validate_dingtalk_outbound_text(text: str, *, field: str = "text") -> str:
    """Reject common transport-corruption markers before any provider call."""

    value = str(text or "")
    if "\ufffd" in value:
        raise DingTalkOutboundContentError(
            f"DingTalk outbound {field} contains a Unicode replacement character."
        )
    for line in value.splitlines() or [value]:
        compact = "".join(line.split())
        if len(compact) >= 4 and set(compact) == {"?"}:
            raise DingTalkOutboundContentError(
                f"DingTalk outbound {field} contains a question-mark-only line; "
                "the source text may have been corrupted."
            )
    return value


def _object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _first_text_value(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list, tuple)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _user_ids(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    user_ids: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            raw = (
                item.get("userId")
                or item.get("userid")
                or item.get("user_id")
            )
        else:
            raw = item
        text = str(raw or "").strip()
        if text:
            user_ids.add(text)
    return user_ids


def _delivery_user_ids(payload: dict[str, Any]) -> set[str]:
    direct = _user_ids(payload.get("messageReadInfoList"))
    result = payload.get("send_result") if isinstance(payload.get("send_result"), dict) else {}
    return direct | _user_ids(result.get("read_user_id_list")) | _user_ids(
        result.get("unread_user_id_list")
    )


def extract_voice_text(payload: dict[str, Any]) -> str:
    content = _object_to_dict(payload.get("content") or payload.get("Content"))
    audio = _object_to_dict(payload.get("audio") or payload.get("voice"))
    result = _object_to_dict(payload.get("result"))

    return _first_text_value(
        payload.get("recognition"),
        payload.get("recognitionText"),
        payload.get("recognitionResult"),
        payload.get("speechText"),
        payload.get("asrText"),
        payload.get("text"),
        content.get("recognition"),
        content.get("recognitionText"),
        content.get("recognitionResult"),
        content.get("speechText"),
        content.get("asrText"),
        content.get("content"),
        content.get("text"),
        audio.get("recognition"),
        audio.get("recognitionText"),
        audio.get("recognitionResult"),
        audio.get("speechText"),
        audio.get("asrText"),
        result.get("recognition"),
        result.get("text"),
    )


def extract_voice_download_code(payload: dict[str, Any]) -> str:
    content = _object_to_dict(payload.get("content") or payload.get("Content"))
    audio = _object_to_dict(payload.get("audio") or payload.get("voice"))
    return _first_text_value(
        payload.get("downloadCode"),
        payload.get("download_code"),
        content.get("downloadCode"),
        content.get("download_code"),
        audio.get("downloadCode"),
        audio.get("download_code"),
    )


def verify_incoming_token(settings: Settings, token: str | None) -> None:
    if settings.dingtalk_incoming_token and token != settings.dingtalk_incoming_token:
        raise DingTalkPayloadError("Invalid DingTalk incoming token.")


def parse_incoming_message(payload: dict[str, Any]) -> DingTalkIncomingMessage:
    event_type = payload.get("EventType") or payload.get("eventType")
    sender_id = (
        payload.get("senderStaffId")
        or payload.get("senderId")
        or payload.get("userId")
        or payload.get("userid")
        or payload.get("fromUserId")
        or payload.get("senderUserId")
        or payload.get("operatorUserId")
        or payload.get("dingtalk_user_id")
    )
    if not sender_id:
        raise DingTalkPayloadError("Missing sender user id.")

    msg_type = payload.get("msgtype") or payload.get("msgType") or payload.get("msgtypeText") or "text"
    text = ""
    if msg_type == "text":
        text_obj = payload.get("text") or {}
        text = text_obj.get("content") if isinstance(text_obj, dict) else str(text_obj)
    elif msg_type in {"audio", "voice"}:
        text = extract_voice_text(payload)
    else:
        text = payload.get("content") or payload.get("text", {}).get("content", "")

    if not text:
        content_obj = payload.get("content") or payload.get("Content")
        if isinstance(content_obj, dict):
            text = content_obj.get("content") or content_obj.get("text") or ""
        elif isinstance(content_obj, str):
            try:
                content_json = json.loads(content_obj)
            except json.JSONDecodeError:
                text = content_obj
            else:
                if isinstance(content_json, dict):
                    text = content_json.get("content") or content_json.get("text") or ""
                else:
                    text = content_obj

    text = str(text).strip()
    if not text and msg_type not in ("audio", "voice"):
        raise DingTalkPayloadError("Message text is empty.")

    conversation_kind = normalize_dingtalk_conversation_kind(
        payload.get("conversationType")
        or payload.get("conversation_type")
    )
    mention_value = payload.get("isInAtList")
    bot_was_mentioned = (
        mention_value
        if isinstance(mention_value, bool)
        else None
    )

    return DingTalkIncomingMessage(
        dingtalk_user_id=str(sender_id),
        text=text,
        message_id=payload.get("msgId") or payload.get("messageId") or payload.get("message_id"),
        conversation_id=payload.get("conversationId") or payload.get("conversation_id") or payload.get("openConversationId"),
        source=f"dingtalk_{event_type or msg_type}",
        session_webhook=payload.get("sessionWebhook"),
        conversation_kind=conversation_kind,
        bot_was_mentioned=bot_was_mentioned,
    )


def normalize_dingtalk_conversation_kind(
    provider_value: Any,
) -> Literal["direct", "group", "unknown"]:
    """Keep DingTalk's scene as a trusted transport fact.

    DingTalk uses 1 for a one-to-one conversation and 2 for a group.  Missing
    metadata stays unknown so a private-only feature can fail closed instead
    of guessing from a conversation ID or reply webhook.
    """

    if provider_value is None or str(provider_value).strip() == "":
        return "unknown"
    normalized = str(provider_value).strip().lower()
    if normalized in {"1", "single", "direct", "private"}:
        return "direct"
    if normalized in {"2", "group"}:
        return "group"
    raise DingTalkPayloadError("Unsupported DingTalk conversation type.")


def build_idempotency_key(payload: dict[str, Any], message: DingTalkIncomingMessage) -> str:
    return canonical_dingtalk_idempotency_key(
        message_id=message.message_id,
        user_id=message.dingtalk_user_id,
        conversation_id=message.conversation_id,
        text=message.text,
        created_at=payload.get("createAt") or payload.get("timestamp"),
    )


class DingTalkRobotClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=10, trust_env=False)
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._api_base_url = settings.dingtalk_api_base_url.rstrip("/")
        self._oapi_base_url = settings.dingtalk_oapi_base_url.rstrip("/")

    async def close(self) -> None:
        await self._client.aclose()

    def has_enterprise_app(self) -> bool:
        return bool(self.settings.dingtalk_app_key and self.settings.dingtalk_app_secret and self.settings.dingtalk_agent_id)

    def enterprise_access_token_seconds_remaining(self) -> int:
        return max(0, int(self._access_token_expires_at - time.time()))

    async def get_enterprise_access_token(self) -> str:
        if not self.settings.dingtalk_app_key or not self.settings.dingtalk_app_secret:
            raise ValueError("DingTalk enterprise app credentials are not configured.")

        now = time.time()
        if self._access_token and now < self._access_token_expires_at - 300:
            return self._access_token

        response = await self._client.post(
            f"{self._api_base_url}/v1.0/oauth2/accessToken",
            json={
                "appKey": self.settings.dingtalk_app_key,
                "appSecret": self.settings.dingtalk_app_secret,
            },
        )
        response.raise_for_status()
        data = response.json()
        access_token = data.get("accessToken")
        if not access_token:
            message = data.get("message") or data.get("errmsg") or "DingTalk access token response missing accessToken."
            raise RuntimeError(message)

        self._access_token = str(access_token)
        self._access_token_expires_at = now + int(data.get("expireIn") or data.get("expiresIn") or 7200)
        return self._access_token

    async def send_work_notification(self, *, user_ids: list[str], text: str) -> dict[str, Any]:
        text = validate_dingtalk_outbound_text(text)
        user_ids = [user_id for user_id in user_ids if user_id]
        if not user_ids:
            return {}
        if not self.settings.dingtalk_agent_id:
            raise ValueError("DingTalk agent id is not configured.")

        access_token = await self.get_enterprise_access_token()
        agent_id: int | str
        agent_id = int(self.settings.dingtalk_agent_id) if self.settings.dingtalk_agent_id.isdigit() else self.settings.dingtalk_agent_id
        payload = {
            "agent_id": agent_id,
            "userid_list": ",".join(user_ids),
            "msg": {
                "msgtype": "text",
                "text": {"content": text},
            },
        }
        response = await self._client.post(
            f"{self._oapi_base_url}/topapi/message/corpconversation/asyncsend_v2",
            params={"access_token": access_token},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errcode") not in (0, None):
            raise RuntimeError(data.get("errmsg") or "DingTalk work notification failed.")
        task_id = str(data.get("task_id") or data.get("taskId") or "").strip()
        if not task_id:
            raise DingTalkDeliveryError(
                "DingTalk work notification response is missing task_id."
            )
        try:
            verification = await self.wait_for_work_notification_delivery(
                task_id=task_id,
                expected_user_ids=user_ids,
            )
        except DingTalkDeliveryError as exc:
            raise DingTalkDeliveryError(
                str(exc),
                provider_reference=task_id,
                accepted_payload=data,
                terminal_failure=exc.terminal_failure,
                verification_unavailable=exc.verification_unavailable,
            ) from exc
        return {
            **data,
            "deliveryVerified": True,
            "deliveryStatus": "SUCCESS",
            "deliveryRecipientUserIds": sorted(_delivery_user_ids(verification)),
        }

    async def send_robot_direct_text(self, *, user_ids: list[str], text: str) -> dict[str, Any]:
        text = validate_dingtalk_outbound_text(text)
        user_ids = [user_id for user_id in user_ids if user_id]
        if not user_ids:
            return {"processQueryKey": None, "invalidStaffIdList": [], "filteredStaffIdList": [], "flowControlledStaffIdList": []}
        if not self.settings.dingtalk_app_key:
            raise ValueError("DingTalk app key is not configured.")

        access_token = await self.get_enterprise_access_token()
        response = await self._client.post(
            f"{self._api_base_url}/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": access_token},
            json={
                "robotCode": self.settings.dingtalk_app_key,
                "userIds": user_ids,
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") or data.get("errcode"):
            raise RuntimeError(data.get("message") or data.get("errmsg") or "DingTalk direct robot message failed.")
        return data

    async def send_robot_direct_text_verified(
        self,
        *,
        user_ids: list[str],
        text: str,
    ) -> dict[str, Any]:
        expected_user_ids = [user_id for user_id in user_ids if user_id]
        data = await self.send_robot_direct_text(
            user_ids=expected_user_ids,
            text=text,
        )
        return await self._attach_direct_delivery_verification(
            data,
            expected_user_ids=expected_user_ids,
        )

    async def send_robot_direct_markdown(self, *, user_ids: list[str], title: str, text: str) -> dict[str, Any]:
        title = validate_dingtalk_outbound_text(title, field="title")
        text = validate_dingtalk_outbound_text(text)
        user_ids = [user_id for user_id in user_ids if user_id]
        if not user_ids:
            return {"processQueryKey": None, "invalidStaffIdList": [], "filteredStaffIdList": [], "flowControlledStaffIdList": []}
        if not self.settings.dingtalk_app_key:
            raise ValueError("DingTalk app key is not configured.")

        access_token = await self.get_enterprise_access_token()
        response = await self._client.post(
            f"{self._api_base_url}/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": access_token},
            json={
                "robotCode": self.settings.dingtalk_app_key,
                "userIds": user_ids,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") or data.get("errcode"):
            raise RuntimeError(data.get("message") or data.get("errmsg") or "DingTalk direct robot markdown message failed.")
        return data

    async def send_robot_direct_markdown_verified(
        self,
        *,
        user_ids: list[str],
        title: str,
        text: str,
    ) -> dict[str, Any]:
        expected_user_ids = [user_id for user_id in user_ids if user_id]
        data = await self.send_robot_direct_markdown(
            user_ids=expected_user_ids,
            title=title,
            text=text,
        )
        return await self._attach_direct_delivery_verification(
            data,
            expected_user_ids=expected_user_ids,
        )

    async def _attach_direct_delivery_verification(
        self,
        data: dict[str, Any],
        *,
        expected_user_ids: list[str],
    ) -> dict[str, Any]:
        process_query_key = str(data.get("processQueryKey") or "").strip()
        if not process_query_key:
            raise DingTalkDeliveryError(
                "DingTalk direct robot response is missing processQueryKey."
            )
        try:
            verification = await self.wait_for_robot_direct_delivery(
                process_query_key=process_query_key,
                expected_user_ids=expected_user_ids,
            )
        except DingTalkDeliveryError as exc:
            raise DingTalkDeliveryError(
                str(exc),
                provider_reference=process_query_key,
                accepted_payload=data,
                terminal_failure=exc.terminal_failure,
                verification_unavailable=exc.verification_unavailable,
            ) from exc
        return {
            **data,
            "deliveryVerified": True,
            "deliveryStatus": str(verification.get("sendStatus") or "SUCCESS"),
            "deliveryRecipientUserIds": sorted(_delivery_user_ids(verification)),
        }

    async def get_robot_direct_message_status(
        self,
        *,
        process_query_key: str,
    ) -> dict[str, Any]:
        if not process_query_key:
            raise ValueError("process_query_key is required")
        access_token = await self.get_enterprise_access_token()
        response = await self._client.get(
            f"{self._api_base_url}/v1.0/robot/oToMessages/readStatus",
            headers={"x-acs-dingtalk-access-token": access_token},
            params={
                "robotCode": self.settings.dingtalk_app_key,
                "processQueryKey": process_query_key,
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") or data.get("errcode"):
            raise RuntimeError(
                data.get("message")
                or data.get("errmsg")
                or "DingTalk direct robot delivery query failed."
            )
        return data

    async def wait_for_robot_direct_delivery(
        self,
        *,
        process_query_key: str,
        expected_user_ids: list[str],
        attempts: int = 6,
        interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        expected = {str(user_id).strip() for user_id in expected_user_ids if str(user_id).strip()}
        last_error = "delivery result not ready"
        for attempt in range(max(1, attempts)):
            try:
                data = await self.get_robot_direct_message_status(
                    process_query_key=process_query_key,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise DingTalkDeliveryError(
                        "DingTalk direct robot delivery verification is unavailable.",
                        verification_unavailable=True,
                    ) from exc
                last_error = str(exc)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last_error = str(exc)
            else:
                status = str(data.get("sendStatus") or "").upper()
                delivered = _delivery_user_ids(data)
                if status == "SUCCESS" and expected.issubset(delivered):
                    return data
                if status and status != "SUCCESS":
                    raise DingTalkDeliveryError(
                        f"DingTalk direct robot delivery failed with status={status}.",
                        terminal_failure=True,
                    )
                missing = sorted(expected - delivered)
                last_error = f"missing recipients: {','.join(missing)}"
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(max(0.0, interval_seconds))
        raise DingTalkDeliveryError(
            "DingTalk direct robot delivery was not confirmed: " + last_error
        )

    async def get_work_notification_send_result(
        self,
        *,
        task_id: str,
    ) -> dict[str, Any]:
        if not task_id:
            raise ValueError("task_id is required")
        if not self.settings.dingtalk_agent_id:
            raise ValueError("DingTalk agent id is not configured.")
        access_token = await self.get_enterprise_access_token()
        agent_id: int | str
        agent_id = int(self.settings.dingtalk_agent_id) if self.settings.dingtalk_agent_id.isdigit() else self.settings.dingtalk_agent_id
        response = await self._client.post(
            f"{self._oapi_base_url}/topapi/message/corpconversation/getsendresult",
            params={"access_token": access_token},
            json={"agent_id": agent_id, "task_id": int(task_id) if task_id.isdigit() else task_id},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errcode") not in (0, None):
            raise RuntimeError(
                data.get("errmsg")
                or "DingTalk work notification delivery query failed."
            )
        return data

    async def wait_for_work_notification_delivery(
        self,
        *,
        task_id: str,
        expected_user_ids: list[str],
        attempts: int = 6,
        interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        expected = {str(user_id).strip() for user_id in expected_user_ids if str(user_id).strip()}
        last_error = "delivery result not ready"
        for attempt in range(max(1, attempts)):
            try:
                data = await self.get_work_notification_send_result(task_id=task_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise DingTalkDeliveryError(
                        "DingTalk work notification delivery verification is unavailable.",
                        verification_unavailable=True,
                    ) from exc
                last_error = str(exc)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last_error = str(exc)
            else:
                result = data.get("send_result") if isinstance(data.get("send_result"), dict) else {}
                rejected = set()
                for key in (
                    "failed_user_id_list",
                    "invalid_user_id_list",
                    "forbidden_list",
                ):
                    rejected.update(_user_ids(result.get(key)))
                rejected_targets = sorted(expected & rejected)
                if rejected_targets:
                    raise DingTalkDeliveryError(
                        "DingTalk work notification rejected recipients: "
                        + ",".join(rejected_targets),
                        terminal_failure=True,
                    )
                delivered = _delivery_user_ids(data)
                if expected.issubset(delivered):
                    return data
                missing = sorted(expected - delivered)
                last_error = f"missing recipients: {','.join(missing)}"
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(max(0.0, interval_seconds))
        raise DingTalkDeliveryError(
            "DingTalk work notification delivery was not confirmed: " + last_error
        )

    async def send_session_webhook_text(self, *, session_webhook: str, text: str) -> None:
        if not session_webhook:
            return
        response = await self._client.post(
            session_webhook,
            json={
                "msgtype": "text",
                "text": {"content": text},
            },
        )
        response.raise_for_status()

    async def recognize_audio(self, download_code: str) -> str:
        """Call DingTalk ASR API for one-sentence voice recognition."""
        access_token = await self.get_enterprise_access_token()
        response = await self._client.post(
            f"{self._api_base_url}/v1.0/robot/audio/asr",
            headers={"x-acs-dingtalk-access-token": access_token},
            json={"downloadCode": download_code},
        )
        response.raise_for_status()
        data = response.json()
        result = data.get("result", "")
        if not result:
            raise RuntimeError(data.get("message") or data.get("errmsg") or "DingTalk ASR returned empty result.")
        return str(result)

    async def send_text(self, *, webhook_url: str, secret: str | None, text: str, at_user_ids: list[str] | None = None) -> None:
        if not webhook_url:
            return
        url = webhook_url
        if secret:
            timestamp = str(round(time.time() * 1000))
            sign_text = f"{timestamp}\n{secret}"
            digest = hmac.new(secret.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest))
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}timestamp={timestamp}&sign={sign}"
        payload = {
            "msgtype": "text",
            "text": {"content": text},
            "at": {"atUserIds": at_user_ids or [], "isAtAll": False},
        }
        response = await self._client.post(url, json=payload)
        response.raise_for_status()


def dingtalk_text_response(text: str) -> dict[str, Any]:
    return {"msgtype": "text", "text": {"content": text}}
