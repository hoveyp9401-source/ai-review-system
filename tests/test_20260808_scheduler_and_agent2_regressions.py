from __future__ import annotations

import ast
import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.agent2.tool_calling.contracts import AddDailyItemsArgs
from app.agent2.tool_calling.production_store import _text_content
from app.agent2.tool_calling.receipt_reply import _render_report_snapshot
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.scheduler.jobs import (
    reconcile_pending_scheduler_deliveries,
    send_user_message,
)
from app.scheduler.runner import _send_daily_briefings
from app.services.dingtalk import DingTalkDeliveryError
from app.stream_runner import StreamJob, _worker


def test_typed_daily_commands_contains_no_language_phrase_router() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "agent2"
        / "typed_daily_commands.py"
    )
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "_contains_forbidden_payload" not in source
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            any(alias.name == "re" for alias in node.names)
            if isinstance(node, ast.Import)
            else node.module == "re"
        )
        for node in ast.walk(tree)
    )


def _command(
    *,
    command_type: str,
    owner_id: UUID,
    report_id: UUID,
    version: int,
    patch: dict,
) -> TypedDailyCommand:
    return TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type=command_type,
        report_id=report_id,
        report_version=version,
        target_item_ids=(),
        patch=patch,
        idempotency_key=f"test:{uuid4()}",
    )


def test_agent2_contract_carries_model_decided_empty_field_without_phrase_rules() -> None:
    arguments = AddDailyItemsArgs(
        date_expression="today",
        proposed_date=date(2026, 8, 8),
        items=(
            {
                "field": "today_work",
                "content": "完成合同审核",
                "source_evidence": {
                    "source_message_index": 1,
                },
            },
            {
                "field": "tomorrow_plan",
                "content": "继续跟进案件",
                "source_evidence": {
                    "source_message_index": 1,
                },
            },
        ),
        acknowledged_empty_fields=("problems",),
        empty_field_evidence=(
            {
                "field": "problems",
                "source_evidence": {
                    "source_message_index": 1,
                },
            },
        ),
    )

    assert arguments.acknowledged_empty_fields == ("problems",)
    assert [item.content for item in arguments.items] == [
        "完成合同审核",
        "继续跟进案件",
    ]


def test_model_decided_empty_field_is_complete_and_can_be_submitted() -> None:
    owner_id = uuid4()
    report_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=2,
        status="collecting",
        today_work=("完成合同审核",),
        tomorrow_plan=("继续跟进案件",),
        item_ids={
            "today_work": ("today-1",),
            "problems": (),
            "tomorrow_plan": ("tomorrow-1",),
        },
    )
    acknowledged = execute_typed_daily_command(
        _command(
            command_type="acknowledge_empty_section",
            owner_id=owner_id,
            report_id=report_id,
            version=2,
            patch={"field": "problems"},
        ),
        snapshot=snapshot,
        actor_user_id=owner_id,
    )
    assert acknowledged.changed is True
    assert acknowledged.after.acknowledged_empty_fields == frozenset(
        {"problems"}
    )

    submitted = execute_typed_daily_command(
        _command(
            command_type="submit_report",
            owner_id=owner_id,
            report_id=report_id,
            version=3,
            patch={},
        ),
        snapshot=acknowledged.after,
        actor_user_id=owner_id,
    )
    assert submitted.validation.status == "authorized"
    assert submitted.after.status == "completed"


def test_empty_acknowledgement_renders_as_no_problem_not_unfilled() -> None:
    rendered = _render_report_snapshot(
        {
            "report_date": "2026-08-08",
            "fields": {
                "today_work": ["完成合同审核"],
                "problems": [],
                "tomorrow_plan": ["继续跟进案件"],
            },
            "acknowledged_empty_fields": ["problems"],
        }
    )

    assert "问题风险\n暂无明显问题" in rendered
    assert "问题风险\n（未填写）" not in rendered


def test_voice_recent_context_contains_only_recognition_text() -> None:
    payload = {
        "msgtype": "audio",
        "content": {
            "recognition": "整理刚才这份日报",
            "downloadCode": "provider-private-download-code",
        },
    }

    assert _text_content(payload) == "整理刚才这份日报"


@pytest.mark.asyncio
async def test_same_conversation_turns_cannot_overtake(monkeypatch) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    started: list[str] = []

    async def fake_handle_job(**kwargs) -> None:
        job = kwargs["job"]
        started.append(job.text)
        if job.text == "first":
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    message_one = SimpleNamespace(
        message_id="message-1",
        sender_staff_id="user-1",
        sender_id="",
        conversation_id="conversation-1",
    )
    message_two = SimpleNamespace(
        message_id="message-2",
        sender_staff_id="user-1",
        sender_id="",
        conversation_id="conversation-1",
    )
    await queue.put(StreamJob(message_one, "first", {}))
    await queue.put(StreamJob(message_two, "second", {}))
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
                None,
                locks,
            )
        )
        for index in (1, 2)
    ]
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert started == ["first"]
        release_first.set()
        await asyncio.wait_for(queue.join(), timeout=1)
        assert started == ["first", "second"]
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_different_conversations_still_run_in_parallel(monkeypatch) -> None:
    queue: asyncio.Queue[StreamJob] = asyncio.Queue()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_handle_job(**kwargs) -> None:
        job = kwargs["job"]
        if job.text == "first":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    monkeypatch.setattr("app.stream_runner._handle_job", fake_handle_job)
    await queue.put(
        StreamJob(
            SimpleNamespace(
                message_id="message-1",
                sender_staff_id="user-1",
                sender_id="",
                conversation_id="conversation-1",
            ),
            "first",
            {},
        )
    )
    await queue.put(
        StreamJob(
            SimpleNamespace(
                message_id="message-2",
                sender_staff_id="user-2",
                sender_id="",
                conversation_id="conversation-2",
            ),
            "second",
            {},
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
                None,
                locks,
            )
        )
        for index in (1, 2)
    ]
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        release_first.set()
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_provider_acceptance_without_verification_is_pending_not_resent() -> None:
    class Robot:
        async def send_robot_direct_text_verified(self, **kwargs):
            raise DingTalkDeliveryError(
                "verification unavailable",
                provider_reference="provider-reference-1",
            )

    evidence = await send_user_message(
        Robot(),
        ["user-1"],
        "日报提醒",
    )

    assert evidence.provider_reference == "provider-reference-1"
    assert evidence.message_status == "accepted_by_provider"
    assert evidence.delivery_verified is False


@pytest.mark.asyncio
async def test_unavailable_receipt_api_is_backed_off_for_the_whole_batch() -> None:
    now = datetime(2026, 8, 8, 2, 30, tzinfo=UTC)
    events = [
        SimpleNamespace(
            backend_action="daily_briefing_delivery_pending",
            dingtalk_user_id=f"user-{index}",
            llm_decision_json={
                "interaction_type": "daily_briefing",
                "dispatches": [
                    {
                        "transport": "direct_robot_markdown",
                        "provider_reference": f"provider-{index}",
                        "delivery_verified": False,
                    }
                ],
            },
        )
        for index in (1, 2)
    ]

    class Result:
        def all(self):
            return events

    class Session:
        async def scalars(self, _statement):
            return Result()

    class Robot:
        calls = 0

        async def wait_for_robot_direct_delivery(self, **_kwargs):
            self.calls += 1
            raise DingTalkDeliveryError(
                "receipt API unavailable",
                verification_unavailable=True,
            )

    robot = Robot()
    result = await reconcile_pending_scheduler_deliveries(
        Session(),
        robot,
        now=now,
    )

    assert robot.calls == 1
    assert result == {
        "loaded": 2,
        "checked": 1,
        "deferred": 1,
        "verified": 0,
        "still_pending": 2,
        "failed": 0,
    }
    assert all(
        event.llm_decision_json["next_delivery_check_at"]
        == "2026-08-08T08:30:00+00:00"
        for event in events
    )


@pytest.mark.asyncio
async def test_one_briefing_recipient_failure_does_not_stop_the_next() -> None:
    recipient_one = uuid4()
    recipient_two = uuid4()

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def send_robot_direct_markdown_verified(self, **kwargs):
            user_id = kwargs["user_ids"][0]
            self.calls.append(user_id)
            if user_id == "user-1":
                raise RuntimeError("pre-acceptance failure")
            return {
                "processQueryKey": "provider-reference-2",
                "deliveryVerified": True,
            }

    class Session:
        def __init__(self) -> None:
            self.events = []

        def add(self, event) -> None:
            self.events.append(event)

        async def scalars(self, _query):
            events = self.events

            class Rows:
                def all(self):
                    return list(events)

            return Rows()

    robot = Robot()
    session = Session()
    sent = await _send_daily_briefings(
        robot,
        {
            "date": "2026-08-08",
            "team_messages": [
                {
                    "scope": "team",
                    "team_name": "法务一部",
                    "text": "晨报正文",
                    "recipients": [
                        {
                            "id": str(recipient_one),
                            "dingtalk_user_id": "user-1",
                        },
                        {
                            "id": str(recipient_two),
                            "dingtalk_user_id": "user-2",
                        },
                    ],
                }
            ],
        },
        session=session,
    )

    assert sent == 1
    assert robot.calls == ["user-1", "user-2"]
    assert [event.backend_action for event in session.events] == [
        "daily_briefing_failed",
        "daily_briefing_sent",
    ]
