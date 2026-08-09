from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.turn_batching import (
    CanaryTurnBatchCoordinator,
    canonical_turn_batch_source_id,
)
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.llm.extractor import DailyReportExtractor
from app.models import PerformanceSubmission, PerformanceTask, User, WebhookEvent
from app.services.performance_service import (
    ACTIVE_PERFORMANCE_STATUSES,
    PerformanceTaskService,
)
from app.services.report_service import DailyReportService
import app.stream_runner as stream_runner


PANG_USER_ID = UUID("222b1eeb-4faa-40cf-a193-e1892c9377b0")


class CountingHttpClient:
    def __init__(self, client) -> None:
        self._client = client
        self.post_count = 0

    async def post(self, *args, **kwargs):
        self.post_count += 1
        return await self._client.post(*args, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    def __getattr__(self, name: str):
        return getattr(self._client, name)


class NoSendRobot:
    def __init__(self) -> None:
        self.send_calls = 0

    async def send_session_webhook_text(self, **kwargs) -> None:
        self.send_calls += 1
        raise AssertionError("real DingTalk send is forbidden in rollback smoke")

    async def send_robot_direct_text(self, **kwargs) -> None:
        self.send_calls += 1
        raise AssertionError("real DingTalk send is forbidden in rollback smoke")


def message(*, message_id: str, user: User, conversation_id: str):
    return SimpleNamespace(
        message_id=message_id,
        sender_staff_id=user.dingtalk_user_id,
        sender_id="",
        conversation_id=conversation_id,
        session_webhook="",
        message_type="text",
    )


async def main() -> None:
    settings = get_settings()
    llm_client = LLMClient(settings)
    counting_http = CountingHttpClient(llm_client.native_http_client)
    llm_client._client = counting_http
    report_service = DailyReportService(
        settings,
        DailyReportExtractor(llm_client),
    )
    performance_service = PerformanceTaskService(settings)
    robot = NoSendRobot()
    reply_texts: list[str] = []
    original_session_factory = stream_runner.AsyncSessionLocal
    original_reply = stream_runner._reply_with_observability
    event_ids: tuple[UUID, UUID] | None = None
    batch_source_id = ""
    control_before: dict[str, object] | None = None
    performance_before: dict[UUID, str] = {}
    active_performance_user_count = 0

    async def capture_reply(_handler, _robot, _job, text):
        reply_texts.append(str(text))
        return stream_runner.StreamReplyObservation(
            elapsed_seconds=0.0,
            transport_status="rollback_smoke_suppressed",
            provider_accepted=False,
            delivery_verified=False,
        )

    result: dict[str, object] = {}
    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()
            sandbox_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            stream_runner.AsyncSessionLocal = sandbox_factory
            stream_runner._reply_with_observability = capture_reply
            try:
                async with sandbox_factory() as seed:
                    user = await seed.get(User, PANG_USER_ID)
                    if user is None or not user.dingtalk_user_id:
                        raise AssertionError("Pang Hao user binding is unavailable")
                    active_performance_user_count = int(
                        await seed.scalar(
                            select(
                                func.count(
                                    func.distinct(
                                        PerformanceSubmission.user_id
                                    )
                                )
                            )
                            .join(
                                PerformanceTask,
                                PerformanceSubmission.task_id
                                == PerformanceTask.id,
                            )
                            .where(
                                PerformanceSubmission.status.in_(
                                    ACTIVE_PERFORMANCE_STATUSES
                                ),
                                PerformanceTask.status == "active",
                            )
                        )
                        or 0
                    )
                    active_submissions = list(
                        (
                            await seed.scalars(
                                select(PerformanceSubmission)
                                .join(
                                    PerformanceTask,
                                    PerformanceSubmission.task_id
                                    == PerformanceTask.id,
                                )
                                .where(
                                    PerformanceSubmission.user_id == user.id,
                                    PerformanceSubmission.status.in_(
                                        ACTIVE_PERFORMANCE_STATUSES
                                    ),
                                    PerformanceTask.status == "active",
                                )
                            )
                        ).all()
                    )
                    performance_before = {
                        submission.id: submission.status
                        for submission in active_submissions
                    }

                    control = await seed.scalar(
                        select(ToolCallCanaryControl).where(
                            ToolCallCanaryControl.user_id == str(user.id)
                        )
                    )
                    if control is None:
                        raise AssertionError("Pang Hao Agent2 control is unavailable")
                    control_before = {
                        "enabled": bool(control.enabled),
                        "messages_enabled": bool(control.messages_enabled),
                        "registry_digest": control.registry_digest,
                        "prompt_sha256": control.prompt_sha256,
                        "model_name": control.model_name,
                    }
                    control.enabled = True
                    control.messages_enabled = True
                    control.registry_digest = runtime_registry_contract_digest(
                        settings
                    )
                    control.prompt_sha256 = canary_prompt_sha256()
                    control.model_name = CANARY_MODEL_NAME

                    conversation_id = f"rollback-stream-batch-{uuid4()}"
                    now = stream_runner.now_in_timezone(
                        user.timezone or settings.timezone
                    )
                    first_event = WebhookEvent(
                        idempotency_key=f"rollback-stream-batch-1-{uuid4()}",
                        external_message_id=f"rollback-message-1-{uuid4()}",
                        dingtalk_user_id=user.dingtalk_user_id,
                        payload={},
                        status="processing",
                        received_at=now,
                    )
                    second_event = WebhookEvent(
                        idempotency_key=f"rollback-stream-batch-2-{uuid4()}",
                        external_message_id=f"rollback-message-2-{uuid4()}",
                        dingtalk_user_id=user.dingtalk_user_id,
                        payload={},
                        status="processing",
                        received_at=now + timedelta(milliseconds=1),
                    )
                    seed.add_all((first_event, second_event))
                    await seed.commit()
                    event_ids = (first_event.id, second_event.id)
                    batch_source_id = canonical_turn_batch_source_id(
                        (
                            first_event.idempotency_key,
                            second_event.idempotency_key,
                        )
                    )

                    jobs = (
                        stream_runner.StreamJob(
                            message=message(
                                message_id=first_event.external_message_id,
                                user=user,
                                conversation_id=conversation_id,
                            ),
                            text="帮我总结一下庞浩最近的工作。",
                            payload={},
                            received_at_monotonic=time.perf_counter(),
                            queued_at_monotonic=time.perf_counter(),
                            event_id=first_event.id,
                            idempotency_key=first_event.idempotency_key,
                            persisted_received_at=first_event.received_at,
                        ),
                        stream_runner.StreamJob(
                            message=message(
                                message_id=second_event.external_message_id,
                                user=user,
                                conversation_id=conversation_id,
                            ),
                            text="重点看本周，并告诉我依据的日报范围。",
                            payload={},
                            received_at_monotonic=time.perf_counter(),
                            queued_at_monotonic=time.perf_counter(),
                            event_id=second_event.id,
                            idempotency_key=second_event.idempotency_key,
                            persisted_received_at=second_event.received_at,
                        ),
                    )

                queue: asyncio.Queue[stream_runner.StreamJob] = asyncio.Queue()
                for job in jobs:
                    await queue.put(job)
                coordinator = stream_runner.StreamJobBatchCoordinator(
                    CanaryTurnBatchCoordinator(
                        quiet_seconds=0.05,
                        max_window_seconds=0.1,
                    )
                )
                locks: dict[str, asyncio.Lock] = {}
                handler = SimpleNamespace(settings=settings)
                workers = [
                    asyncio.create_task(
                        stream_runner._worker(
                            index,
                            queue,
                            handler,
                            settings,
                            performance_service,
                            report_service,
                            robot,
                            coordinator,
                            locks,
                        )
                    )
                    for index in (1, 2)
                ]
                try:
                    await asyncio.wait_for(
                        queue.join(),
                        timeout=max(30.0, settings.stream_processing_timeout_seconds),
                    )
                finally:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)

                async with sandbox_factory() as inspection:
                    events = [
                        await inspection.get(WebhookEvent, event_id)
                        for event_id in event_ids
                    ]
                    leader_payload = dict(events[0].response_payload or {})
                    follower_payload = dict(events[1].response_payload or {})
                    leader_text = str(
                        (leader_payload.get("text") or {}).get("content") or ""
                    )
                    follower_marker = follower_payload.get(
                        "_agent2_tool_call_canary"
                    )
                    result = {
                        "status": "pass",
                        "batch_size": 2,
                        "ordered_user_messages": [job.text for job in jobs],
                        "event_statuses": [event.status for event in events],
                        "leader_reply_nonempty": bool(leader_text),
                        "follower_delivery": (
                            follower_marker.get("delivery")
                            if isinstance(follower_marker, dict)
                            else None
                        ),
                        "captured_reply_count": len(reply_texts),
                        "model_http_call_count": counting_http.post_count,
                        "active_performance_submissions_observed": len(
                            active_submissions
                        ),
                        "active_performance_user_count": (
                            active_performance_user_count
                        ),
                        "dingtalk_send_calls": robot.send_calls,
                    }
                    if [event.status for event in events] != [
                        "processed",
                        "processed",
                    ]:
                        raise AssertionError(result)
                    if not leader_text or result["follower_delivery"] != (
                        "batched_follower"
                    ):
                        raise AssertionError(result)
                    if len(reply_texts) != 1 or robot.send_calls != 0:
                        raise AssertionError(result)
            finally:
                await outer_transaction.rollback()

        if event_ids is None:
            raise AssertionError("rollback event ids were not created")
        async with AsyncSessionLocal() as verification:
            persisted = [
                await verification.get(WebhookEvent, event_id)
                for event_id in event_ids
            ]
            restored_control = await verification.scalar(
                select(ToolCallCanaryControl).where(
                    ToolCallCanaryControl.user_id == str(PANG_USER_ID)
                )
            )
            restored_submissions = {
                submission.id: submission.status
                for submission in (
                    await verification.scalars(
                        select(PerformanceSubmission).where(
                            PerformanceSubmission.id.in_(
                                tuple(performance_before)
                            )
                        )
                    )
                ).all()
            }
            persisted_receipts = list(
                (
                    await verification.scalars(
                        select(ToolCallCanaryReceipt).where(
                            ToolCallCanaryReceipt.source_message_id
                            == batch_source_id
                        )
                    )
                ).all()
            )
        restored_control_snapshot = (
            {
                "enabled": bool(restored_control.enabled),
                "messages_enabled": bool(
                    restored_control.messages_enabled
                ),
                "registry_digest": restored_control.registry_digest,
                "prompt_sha256": restored_control.prompt_sha256,
                "model_name": restored_control.model_name,
            }
            if restored_control is not None
            else None
        )
        result["event_rollback_verified"] = all(
            event is None for event in persisted
        )
        result["control_rollback_verified"] = (
            restored_control_snapshot == control_before
        )
        result["performance_rollback_verified"] = (
            restored_submissions == performance_before
        )
        result["receipt_rollback_verified"] = not persisted_receipts
        result["rollback_verified"] = all(
            result[key] is True
            for key in (
                "event_rollback_verified",
                "control_rollback_verified",
                "performance_rollback_verified",
                "receipt_rollback_verified",
            )
        )
        if result["rollback_verified"] is not True:
            raise AssertionError(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        stream_runner.AsyncSessionLocal = original_session_factory
        stream_runner._reply_with_observability = original_reply
        await llm_client.close()


if __name__ == "__main__":
    asyncio.run(main())
