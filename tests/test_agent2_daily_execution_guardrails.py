from app.agent2.daily_commands import DailyCommand
from app.agent2.daily_execution import apply_commands_to_snapshot


def _apply_active_edit(text: str, *, existing_today: list[str] | None = None):
    return apply_commands_to_snapshot(
        today_work=list(existing_today or []),
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[
            DailyCommand(
                operation="edit",
                target_field="today_work",
                content=[text],
                should_write=True,
            )
        ],
    )


def test_active_context_casual_edit_does_not_append_to_report():
    result = _apply_active_edit("\u4f60\u591a\u5927\u4e86")

    assert result.changed is False
    assert result.today_work == []
    assert result.actions[0]["reason"] == "unsupported_edit"


def test_active_context_short_noise_edit_does_not_append_to_report():
    result = _apply_active_edit("\u6674\u7a7a")

    assert result.changed is False
    assert result.today_work == []
    assert result.actions[0]["reason"] == "unsupported_edit"


def test_active_context_meta_question_edit_does_not_append_to_report():
    result = _apply_active_edit("\u4f60\u90fd\u8bb0\u5f55\u7684\u662f\u5565\u554a", existing_today=["\u5408\u540c\u5ba1\u6838"])

    assert result.changed is False
    assert result.today_work == ["\u5408\u540c\u5ba1\u6838"]
    assert result.actions[0]["reason"] == "unsupported_edit"


def test_active_context_tomorrow_trip_edit_does_not_append_as_today_work():
    result = _apply_active_edit("\u4f30\u8ba1\u660e\u513f\u8981\u53bb\u5357\u4eac")

    assert result.changed is False
    assert result.today_work == []
    assert result.tomorrow_plan == []
    assert result.actions[0]["reason"] == "unsupported_edit"
