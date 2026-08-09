from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.agent2.tool_calling.turn_batching import (
    CanaryTurnBatchCoordinator,
    SealedTurnBatch,
    TurnFragment,
)
from app.stream_runner import (
    StreamBatchRequiresSerialProcessing,
    StreamJob,
    StreamJobBatchCoordinator,
    _initial_stream_timings,
    _require_agent2_batch_scope,
    _worker,
)


def _job(
    *,
    message_id: str,
    text: str,
    received_at: datetime,
    user_id: str = "user-1",
    conversation_id: str = "conversation-1",
    message_type: str = "text",
) -> StreamJob:
    return StreamJob(
        message=SimpleNamespace(
            message_id=message_id,
            sender_staff_id=user_id,
            sender_id="",
            conversation_id=conversation_id,
            message_type=message_type,
        ),
        text=text,
        payload={},
        event_id=uuid.uuid4(),
        idempotency_key=f"source:{message_id}",
        persisted_received_at=received_at,
        message_type=message_type,
    )


def _sealed_batch(base: datetime) -> SealedTurnBatch:
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    return SealedTurnBatch(
        batch_id="batch-1",
        leader_event_id=first_id,
        fragments=(
            TurnFragment(
                event_id=first_id,
                source_message_id="source-1",
                dingtalk_user_id="user-1",
                conversation_id="conversation-1",
                text="first",
                received_at=base,
            ),
            TurnFragment(
                event_id=second_id,
                source_message_id="source-2",
                dingtalk_user_id="user-1",
                conversation_id="conversation-1",
                text="second",
                received_at=base + timedelta(milliseconds=1),
            ),
        ),
    )


def test_queue_wait_does_not_double_count_the_turn_batch_window() -> None:
    job = _job(
        message_id="message-queue-timing",
        text="first",
        received_at=datetime.now(timezone.utc),
    )
    job = replace(
        job,
        queued_at_monotonic=10.0,
        worker_dequeued_at_monotonic=12.0,
    )

    timings = _initial_stream_timings(job, started_at=14.0)

    assert timings["queue_wait_seconds"] == 2.0


@pytest.mark.asyncio
async def test_same_conversation_fragments_reach_the_same_batch_before_execution(
    monkeypatch,
) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    coordinator = StreamJobBatchCoordinator(
        CanaryTurnBatchCoordinator(
            quiet_seconds=0.03,
            max_window_seconds=0.06,
        )
    )
    base = datetime.now(timezone.utc)
    observed: list[tuple[str, tuple[str, ...]]] = []

    async def fake_handle_job(**kwargs) -> None:
        job = kwargs["job"]
        batch = kwargs["turn_batch"]
        observed.append(
            (job.text, tuple(fragment.text for fragment in batch.fragments))
        )

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    await queue.put(
        _job(
            message_id="message-1",
            text="first",
            received_at=base,
            message_type="voice",
        )
    )
    await queue.put(
        _job(
            message_id="message-2",
            text="second",
            received_at=base + timedelta(milliseconds=1),
        )
    )
    locks: dict[str, asyncio.Lock] = {}
    workers = [
        asyncio.create_task(
            _worker(
                index,
                queue,
                None,
                None,
                None,
                None,
                None,
                coordinator,
                locks,
            )
        )
        for index in (1, 2)
    ]
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    assert observed == [("first", ("first", "second"))]


@pytest.mark.asyncio
async def test_five_quick_fragments_execute_as_one_ordered_model_turn(
    monkeypatch,
) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    coordinator = StreamJobBatchCoordinator(
        CanaryTurnBatchCoordinator(
            quiet_seconds=0.03,
            max_window_seconds=0.06,
        )
    )
    base = datetime.now(timezone.utc)
    observed: list[tuple[str, ...]] = []

    async def fake_handle_job(**kwargs) -> None:
        observed.append(
            tuple(
                fragment.text
                for fragment in kwargs["turn_batch"].fragments
            )
        )

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    texts = tuple(f"fragment-{index}" for index in range(1, 6))
    for index, text in enumerate(texts):
        await queue.put(
            _job(
                message_id=f"message-{index}",
                text=text,
                received_at=base + timedelta(milliseconds=index),
                message_type="voice" if index == 0 else "text",
            )
        )
    locks: dict[str, asyncio.Lock] = {}
    workers = [
        asyncio.create_task(
            _worker(
                index,
                queue,
                None,
                None,
                None,
                None,
                None,
                coordinator,
                locks,
            )
        )
        for index in range(1, 6)
    ]
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    assert observed == [texts]


@pytest.mark.asyncio
async def test_non_agent2_batch_replays_each_original_job_in_order(
    monkeypatch,
) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    coordinator = StreamJobBatchCoordinator(
        CanaryTurnBatchCoordinator(
            quiet_seconds=0.03,
            max_window_seconds=0.06,
        )
    )
    base = datetime.now(timezone.utc)
    attempts: list[tuple[str, int]] = []

    async def fake_handle_job(**kwargs) -> None:
        batch = kwargs.get("turn_batch")
        attempts.append(
            (kwargs["job"].text, len(batch.fragments) if batch else 0)
        )
        if batch is not None:
            raise StreamBatchRequiresSerialProcessing("serial workflow")

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    await queue.put(
        _job(message_id="message-1", text="first", received_at=base)
    )
    await queue.put(
        _job(
            message_id="message-2",
            text="second",
            received_at=base + timedelta(milliseconds=1),
        )
    )
    locks: dict[str, asyncio.Lock] = {}
    workers = [
        asyncio.create_task(
            _worker(
                index,
                queue,
                None,
                None,
                None,
                None,
                None,
                coordinator,
                locks,
            )
        )
        for index in (1, 2)
    ]
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    assert attempts == [
        ("first", 2),
        ("first", 0),
        ("second", 0),
    ]


@pytest.mark.asyncio
async def test_next_same_conversation_batch_waits_for_the_previous_execution(
    monkeypatch,
) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    coordinator = StreamJobBatchCoordinator(
        CanaryTurnBatchCoordinator(
            quiet_seconds=0.02,
            max_window_seconds=0.04,
        )
    )
    base = datetime.now(timezone.utc)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    observed: list[tuple[str, ...]] = []

    async def fake_handle_job(**kwargs) -> None:
        batch = kwargs["turn_batch"]
        texts = tuple(fragment.text for fragment in batch.fragments)
        observed.append(texts)
        if texts == ("first",):
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    await queue.put(
        _job(message_id="message-1", text="first", received_at=base)
    )
    locks: dict[str, asyncio.Lock] = {}
    workers = [
        asyncio.create_task(
            _worker(
                index,
                queue,
                None,
                None,
                None,
                None,
                None,
                coordinator,
                locks,
            )
        )
        for index in (1, 2, 3)
    ]
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await queue.put(
            _job(
                message_id="message-2",
                text="second",
                received_at=base + timedelta(seconds=1),
            )
        )
        await queue.put(
            _job(
                message_id="message-3",
                text="third",
                received_at=base + timedelta(seconds=1, milliseconds=1),
            )
        )
        await asyncio.sleep(0.08)
        assert observed == [("first",)]
        release_first.set()
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    assert observed == [("first",), ("second", "third")]


@pytest.mark.asyncio
async def test_different_conversation_batches_execute_in_parallel(
    monkeypatch,
) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    coordinator = StreamJobBatchCoordinator(
        CanaryTurnBatchCoordinator(
            quiet_seconds=0.02,
            max_window_seconds=0.04,
        )
    )
    base = datetime.now(timezone.utc)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_handle_job(**kwargs) -> None:
        text = kwargs["job"].text
        (first_started if text == "first" else second_started).set()
        await release.wait()

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    await queue.put(
        _job(
            message_id="message-1",
            text="first",
            received_at=base,
            user_id="user-1",
            conversation_id="conversation-1",
        )
    )
    await queue.put(
        _job(
            message_id="message-2",
            text="second",
            received_at=base,
            user_id="user-2",
            conversation_id="conversation-2",
        )
    )
    locks: dict[str, asyncio.Lock] = {}
    workers = [
        asyncio.create_task(
            _worker(
                index,
                queue,
                None,
                None,
                None,
                None,
                None,
                coordinator,
                locks,
            )
        )
        for index in (1, 2)
    ]
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_matching_performance_fragment_keeps_the_original_serial_path(
    monkeypatch,
) -> None:
    batch = _sealed_batch(datetime.now(timezone.utc))

    class ScalarResult:
        def all(self):
            return [
                SimpleNamespace(
                    id=fragment.event_id,
                    status="processing",
                    dingtalk_user_id="user-1",
                )
                for fragment in batch.fragments
            ]

    class Session:
        async def scalars(self, _statement):
            return ScalarResult()

    class PerformanceService:
        async def get_active_submissions(self, _session, _user_id):
            return [
                SimpleNamespace(
                    id="active-performance",
                    sent_snapshot_json={"metrics": []},
                    responses_json=[],
                    status="collecting",
                )
            ]

    route_called = False

    async def fake_route(*args, **kwargs):
        nonlocal route_called
        route_called = True
        return SimpleNamespace(decision=SimpleNamespace(owner="tool_call_core"))

    monkeypatch.setattr(
        "app.stream_runner.resolve_tool_call_canary_route",
        fake_route,
    )
    monkeypatch.setattr(
        "app.stream_runner.is_performance_reply_candidate",
        lambda **_kwargs: True,
    )

    with pytest.raises(StreamBatchRequiresSerialProcessing):
        await _require_agent2_batch_scope(
            session=Session(),
            user=SimpleNamespace(id=uuid.uuid4()),
            dingtalk_user_id="user-1",
            conversation_id="conversation-1",
            turn_batch=batch,
            performance_service=PerformanceService(),
            settings=SimpleNamespace(),
            now=datetime.now(timezone.utc),
        )

    assert route_called is False


@pytest.mark.asyncio
async def test_unrelated_agent2_messages_can_batch_with_an_active_performance_task(
    monkeypatch,
) -> None:
    batch = _sealed_batch(datetime.now(timezone.utc))

    class ScalarResult:
        def all(self):
            return [
                SimpleNamespace(
                    id=fragment.event_id,
                    status="processing",
                    dingtalk_user_id="user-1",
                )
                for fragment in batch.fragments
            ]

    class Session:
        async def scalars(self, _statement):
            return ScalarResult()

    class PerformanceService:
        async def get_active_submissions(self, _session, _user_id):
            return [
                SimpleNamespace(
                    id="active-performance",
                    sent_snapshot_json={"metrics": []},
                    responses_json=[],
                    status="collecting",
                )
            ]

    async def fake_route(*args, **kwargs):
        return SimpleNamespace(
            decision=SimpleNamespace(owner="tool_call_core")
        )

    monkeypatch.setattr(
        "app.stream_runner.resolve_tool_call_canary_route",
        fake_route,
    )
    monkeypatch.setattr(
        "app.stream_runner.is_performance_reply_candidate",
        lambda **_kwargs: False,
    )

    await _require_agent2_batch_scope(
        session=Session(),
        user=SimpleNamespace(id=uuid.uuid4()),
        dingtalk_user_id="user-1",
        conversation_id="conversation-1",
        turn_batch=batch,
        performance_service=PerformanceService(),
        settings=SimpleNamespace(),
        now=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_blocked_agent2_route_is_replayed_serially_without_model_entry(
    monkeypatch,
) -> None:
    batch = _sealed_batch(datetime.now(timezone.utc))

    class ScalarResult:
        def all(self):
            return [
                SimpleNamespace(
                    id=fragment.event_id,
                    status="processing",
                    dingtalk_user_id="user-1",
                )
                for fragment in batch.fragments
            ]

    class Session:
        async def scalars(self, _statement):
            return ScalarResult()

    class PerformanceService:
        async def get_active_submissions(self, _session, _user_id):
            return []

    async def fake_route(*args, **kwargs):
        return SimpleNamespace(decision=SimpleNamespace(owner="blocked"))

    monkeypatch.setattr(
        "app.stream_runner.resolve_tool_call_canary_route",
        fake_route,
    )

    with pytest.raises(StreamBatchRequiresSerialProcessing):
        await _require_agent2_batch_scope(
            session=Session(),
            user=SimpleNamespace(id=uuid.uuid4()),
            dingtalk_user_id="user-1",
            conversation_id="conversation-1",
            turn_batch=batch,
            performance_service=PerformanceService(),
            settings=SimpleNamespace(),
            now=datetime.now(timezone.utc),
        )
