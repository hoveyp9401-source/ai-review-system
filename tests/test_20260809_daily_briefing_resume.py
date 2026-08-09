from __future__ import annotations

from uuid import UUID

import pytest

from app.scheduler import runner


RECIPIENT_ID = UUID("11111111-1111-1111-1111-111111111111")
MESSAGE_PARTS = ("part-1", "part-2", "part-3")


class _ScalarRows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class _MemorySession:
    def __init__(self) -> None:
        self.events = []

    def add(self, event) -> None:
        self.events.append(event)

    async def scalars(self, _query):
        return _ScalarRows(self.events)


def _briefings(*, text: str = "完整晨报正文") -> dict:
    return {
        "date": "2026-08-08",
        "team_messages": [
            {
                "scope": "team",
                "team_id": "team-01",
                "team_name": "综合管理部",
                "text": text,
                "recipients": [
                    {
                        "id": str(RECIPIENT_ID),
                        "dingtalk_user_id": "user-1",
                    }
                ],
            }
        ],
    }


def _fix_three_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner,
        "_split_daily_briefing_text",
        lambda _text: MESSAGE_PARTS,
    )


@pytest.mark.asyncio
async def test_segmented_briefing_retry_resumes_after_provider_accepted_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_three_parts(monkeypatch)

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.failed_part_two_once = False

        async def send_robot_direct_markdown_verified(self, **kwargs):
            text = kwargs["text"]
            self.calls.append(text)
            if text == "part-2" and not self.failed_part_two_once:
                self.failed_part_two_once = True
                raise RuntimeError("pre-acceptance failure")
            return {
                "processQueryKey": f"provider-{len(self.calls)}",
                "deliveryVerified": True,
            }

    briefings = _briefings()
    robot = Robot()
    session = _MemorySession()

    first_sent = await runner._send_daily_briefings(
        robot,
        briefings,
        session=session,
    )
    second_sent = await runner._send_daily_briefings(
        robot,
        briefings,
        session=session,
    )

    assert first_sent == 0
    assert second_sent == 1
    assert robot.calls == ["part-1", "part-2", "part-2", "part-3"]
    assert [event.backend_action for event in session.events] == [
        "daily_briefing_failed",
        "daily_briefing_sent",
    ]
    first_audit = session.events[0].llm_decision_json
    second_audit = session.events[1].llm_decision_json
    assert first_audit["dispatch_key"] == second_audit["dispatch_key"]
    assert first_audit["accepted_part_indexes"] == [1]
    assert first_audit["completed_part_count"] == 1
    assert second_audit["accepted_part_indexes"] == [2, 3]
    assert second_audit["resumed_from_part_index"] == 2
    assert second_audit["completed_part_count"] == 3


@pytest.mark.asyncio
async def test_completed_segmented_briefing_is_not_sent_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_three_parts(monkeypatch)

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def send_robot_direct_markdown_verified(self, **kwargs):
            self.calls.append(kwargs["text"])
            return {
                "processQueryKey": f"provider-{len(self.calls)}",
                "deliveryVerified": True,
            }

    robot = Robot()
    session = _MemorySession()

    first_sent = await runner._send_daily_briefings(
        robot,
        _briefings(),
        session=session,
    )
    second_sent = await runner._send_daily_briefings(
        robot,
        _briefings(),
        session=session,
    )

    assert first_sent == 1
    assert second_sent == 0
    assert robot.calls == list(MESSAGE_PARTS)
    assert len(session.events) == 1


@pytest.mark.asyncio
async def test_changed_briefing_is_blocked_after_any_segment_was_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_three_parts(monkeypatch)

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def send_robot_direct_markdown_verified(self, **kwargs):
            text = kwargs["text"]
            self.calls.append(text)
            if text == "part-2":
                raise RuntimeError("pre-acceptance failure")
            return {
                "processQueryKey": f"provider-{len(self.calls)}",
                "deliveryVerified": True,
            }

    robot = Robot()
    session = _MemorySession()

    first_sent = await runner._send_daily_briefings(
        robot,
        _briefings(text="初版正文"),
        session=session,
    )
    second_sent = await runner._send_daily_briefings(
        robot,
        _briefings(text="修改后的正文"),
        session=session,
    )

    assert first_sent == 0
    assert second_sent == 0
    assert robot.calls == ["part-1", "part-2"]
    assert len(session.events) == 1


@pytest.mark.asyncio
async def test_changed_briefing_can_retry_when_no_segment_was_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_three_parts(monkeypatch)

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.failed_once = False

        async def send_robot_direct_markdown_verified(self, **kwargs):
            text = kwargs["text"]
            self.calls.append(text)
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("pre-acceptance failure")
            return {
                "processQueryKey": f"provider-{len(self.calls)}",
                "deliveryVerified": True,
            }

    robot = Robot()
    session = _MemorySession()

    first_sent = await runner._send_daily_briefings(
        robot,
        _briefings(text="尚未发出的初版正文"),
        session=session,
    )
    second_sent = await runner._send_daily_briefings(
        robot,
        _briefings(text="可安全替换的新正文"),
        session=session,
    )

    assert first_sent == 0
    assert second_sent == 1
    assert robot.calls == ["part-1", *MESSAGE_PARTS]
    assert session.events[0].llm_decision_json[
        "completed_part_count"
    ] == 0
    assert session.events[1].llm_decision_json[
        "completed_part_count"
    ] == 3


@pytest.mark.asyncio
async def test_resumed_audit_keeps_pending_evidence_from_prior_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix_three_parts(monkeypatch)

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.failed_part_two_once = False

        async def send_robot_direct_markdown_verified(self, **kwargs):
            text = kwargs["text"]
            self.calls.append(text)
            if text == "part-2" and not self.failed_part_two_once:
                self.failed_part_two_once = True
                raise RuntimeError("pre-acceptance failure")
            return {
                "processQueryKey": f"provider-{len(self.calls)}",
                "deliveryVerified": text != "part-1",
            }

    robot = Robot()
    session = _MemorySession()

    await runner._send_daily_briefings(
        robot,
        _briefings(),
        session=session,
    )
    second_sent = await runner._send_daily_briefings(
        robot,
        _briefings(),
        session=session,
    )
    third_sent = await runner._send_daily_briefings(
        robot,
        _briefings(),
        session=session,
    )

    assert second_sent == 1
    assert third_sent == 0
    assert robot.calls == ["part-1", "part-2", "part-2", "part-3"]
    final_event = session.events[-1]
    assert final_event.backend_action == "daily_briefing_delivery_pending"
    assert [
        dispatch["part_index"]
        for dispatch in final_event.llm_decision_json["dispatches"]
    ] == [1, 2, 3]
    assert [
        dispatch["delivery_verified"]
        for dispatch in final_event.llm_decision_json["dispatches"]
    ] == [False, True, True]


@pytest.mark.asyncio
async def test_entire_briefing_batch_loads_resume_evidence_once() -> None:
    second_recipient_id = UUID("22222222-2222-2222-2222-222222222222")

    class Robot:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def send_robot_direct_markdown_verified(self, **kwargs):
            self.calls.append(kwargs["user_ids"][0])
            return {
                "processQueryKey": f"provider-{len(self.calls)}",
                "deliveryVerified": True,
            }

    class Session(_MemorySession):
        def __init__(self) -> None:
            super().__init__()
            self.scalar_queries = 0

        async def scalars(self, _query):
            self.scalar_queries += 1
            return _ScalarRows(self.events)

    briefings = _briefings()
    briefings["team_messages"][0]["recipients"].append(
        {
            "id": str(second_recipient_id),
            "dingtalk_user_id": "user-2",
        }
    )
    robot = Robot()
    session = Session()

    sent = await runner._send_daily_briefings(
        robot,
        briefings,
        session=session,
    )

    assert sent == 2
    assert session.scalar_queries == 1
    assert robot.calls == ["user-1", "user-2"]


@pytest.mark.asyncio
async def test_resume_evidence_query_failure_happens_before_any_send() -> None:
    class Robot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_robot_direct_markdown_verified(self, **_kwargs):
            self.calls += 1
            raise AssertionError("must not send after audit query failure")

    class Session:
        async def scalars(self, _query):
            raise RuntimeError("audit database unavailable")

    robot = Robot()

    with pytest.raises(RuntimeError, match="audit database unavailable"):
        await runner._send_daily_briefings(
            robot,
            _briefings(),
            session=Session(),
        )

    assert robot.calls == 0
