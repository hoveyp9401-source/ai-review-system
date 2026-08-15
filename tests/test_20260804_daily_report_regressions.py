import asyncio
import inspect
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.canary_service import (
    _canary_execution_failure_reason,
    process_tool_call_canary_ingress,
)
from app.agent2.tool_calling.context import TrustedRecentMessage
from app.agent2.tool_calling.production_store import (
    _select_recent_messages_with_scheduled_outbound,
)
from app.agent2.tool_calling.receipt_reply import canary_block_message
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.config import Settings
from app.scheduler.jobs import (
    _load_preferred_salutations,
    build_report_reminder_text,
)
from app.scheduler.runner import _send_daily_briefings, run_scheduler


def _message(role: str, content: str, source: str) -> TrustedRecentMessage:
    return TrustedRecentMessage(
        role=role,
        content=content,
        source_message_id=source,
    )


def test_reminder_uses_preferred_salutation_without_changing_body() -> None:
    user = SimpleNamespace(name="刘聪")
    normal = build_report_reminder_text(date(2026, 8, 3), user, None)
    preferred = build_report_reminder_text(
        date(2026, 8, 3),
        user,
        None,
        preferred_salutation="四哥",
    )

    assert normal.startswith("刘聪，")
    assert preferred.startswith("四哥，")
    assert preferred[len("四哥") :] == normal[len("刘聪") :]


def test_preferred_salutation_loader_uses_only_one_valid_value() -> None:
    first_user = uuid4()
    conflicting_user = uuid4()

    class Result:
        def all(self):
            return [
                (first_user, {"salutation": "四哥"}),
                (conflicting_user, {"salutation": "庞总"}),
                (conflicting_user, {"salutation": "浩哥"}),
                (uuid4(), {"salutation": "不合规！"}),
            ]

    class Session:
        async def execute(self, statement):
            del statement
            return Result()

    values = asyncio.run(
        _load_preferred_salutations(
            Session(),
            [first_user, conflicting_user],
            now=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        )
    )

    assert values == {first_user: "四哥"}


def test_prompt_and_registry_allow_safe_historical_continuity() -> None:
    prompt = canary_system_prompt()
    assert "recent_operations" in prompt
    assert "report_reference" in prompt
    assert "daily-briefing:" in prompt
    assert "historical report" in TOOL_REGISTRY["confirm_report"].description
    assert "conversation_report_date" not in prompt
    assert "conversation_report_date" not in TOOL_REGISTRY[
        "query_managed_daily_reports"
    ].description


def test_agent2_ingress_has_no_pre_model_phrase_based_report_focus() -> None:
    source = inspect.getsource(process_tool_call_canary_ingress)

    assert "_conversation_report_date" not in source
    assert "_turn_requests_historical_confirmation" not in source
    assert "_attach_conversation_report_date" not in source


def test_dated_historical_edit_uses_agent2_read_then_write_contract() -> None:
    description = TOOL_REGISTRY["query_report_by_date"].description
    prompt = canary_system_prompt()

    assert "explicitly asks to change content in a dated owned report" in description
    assert "call this read tool first" in description
    assert "query_report_by_date first" in prompt
    assert "next model loop" in prompt


def test_recent_context_reserves_latest_scheduled_briefing() -> None:
    started = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
    outbound = (
        started,
        2,
        _message("assistant", "8月3日晨报", "daily-briefing:1"),
        True,
    )
    newer = [
        (
            started + timedelta(minutes=index + 1),
            0,
            _message("user", f"消息{index}", f"u{index}"),
            False,
        )
        for index in range(13)
    ]

    selected = _select_recent_messages_with_scheduled_outbound(
        [outbound, *newer],
        limit=12,
    )

    assert len(selected) == 12
    assert any(
        item.source_message_id == "daily-briefing:1" for item in selected
    )


def test_daily_briefing_send_persists_full_outbound_evidence() -> None:
    recipient_id = UUID("11111111-1111-1111-1111-111111111111")

    class Robot:
        async def send_robot_direct_markdown(self, **kwargs):
            assert kwargs["user_ids"] == ["55264"]
            return {
                "processQueryKey": "briefing-provider-ref",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": kwargs["user_ids"],
            }

    class Session:
        def __init__(self):
            self.events = []

        def add(self, event):
            self.events.append(event)

        async def scalars(self, _query):
            events = self.events

            class Rows:
                def all(self):
                    return list(events)

            return Rows()

    session = Session()
    sent = asyncio.run(
        _send_daily_briefings(
            Robot(),
            {
                "date": "2026-08-03",
                "team_messages": [
                    {
                        "scope": "team",
                        "team_id": "team-01",
                        "team_name": "综合管理部",
                        "recipients": [
                            {
                                "id": str(recipient_id),
                                "name": "刘聪",
                                "dingtalk_user_id": "55264",
                            }
                        ],
                        "text": "8月3日综合管理部晨报",
                    }
                ],
            },
            session=session,
        )
    )

    assert sent == 1
    assert len(session.events) == 1
    event = session.events[0]
    assert event.user_id == recipient_id
    assert event.backend_action == "daily_briefing_sent"
    assert event.message_text == "8月3日综合管理部晨报"
    assert event.llm_decision_json["provider_references"] == [
        "briefing-provider-ref"
    ]


def test_summary_commits_auto_submit_before_new_briefing_projection_session() -> None:
    source = inspect.getsource(run_scheduler)
    auto_submit = source.index("await auto_submit_due_pending_reports(")
    commit = source.index("await state_session.commit()", auto_submit)
    new_read_session = source.index(
        "async with AsyncSessionLocal() as delivery_session", commit
    )
    build = source.index(
        "await summary_service.build_daily_briefings", auto_submit
    )

    assert auto_submit < commit < new_read_session < build


def test_default_auto_submit_time_is_next_morning_eight() -> None:
    assert Settings.model_fields["auto_submit_cron_hour"].default == 8


def test_incomplete_historical_confirmation_has_honest_guidance() -> None:
    reason = _canary_execution_failure_reason(
        RuntimeError(
            "production runtime failed closed: REPORT_INCOMPLETE"
        )
    )

    assert reason == "tool_call_canary_report_incomplete"
    message = canary_block_message(reason)
    assert "还没填完整" in message
    assert "直接自然说明即可" in message
    assert "今日工作、问题/风险和明日计划" not in message
    assert "照着固定句式" not in message
    assert "已经提交" not in message
