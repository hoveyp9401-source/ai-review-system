from types import SimpleNamespace

from app.agent2.personal_memory import build_personal_memory_profile


def test_personal_memory_profile_is_user_scoped_and_carries_active_habits():
    user = SimpleNamespace(id="user-1", dingtalk_user_id="dt-1", name="庞浩")
    habits = [
        SimpleNamespace(
            habit_type="previous_plan_rollover",
            trigger_text="昨天计划已完成",
            meaning="用户说昨天计划完成时，通常要参考昨天明日计划转今日工作",
            confidence=0.91,
            evidence_count=5,
        )
    ]

    profile = build_personal_memory_profile(user=user, user_habits=habits)
    payload = profile.as_payload()

    assert payload["is_user_scoped"] is True
    assert payload["user_id"] == "user-1"
    assert payload["dingtalk_user_id"] == "dt-1"
    assert payload["display_name"] == "庞浩"
    assert payload["active_habits"][0]["habit_type"] == "previous_plan_rollover"
    assert "user_has_previous_plan_rollover_habit" in payload["notes"]


def test_personal_memory_profile_does_not_share_between_users():
    first = build_personal_memory_profile(
        user=SimpleNamespace(id="user-1", dingtalk_user_id="dt-1", name="庞浩"),
        user_habits=[SimpleNamespace(habit_type="phrase_meaning", trigger_text="没事", meaning="暂无问题")],
    )
    second = build_personal_memory_profile(
        user=SimpleNamespace(id="user-2", dingtalk_user_id="dt-2", name="刘聪"),
        user_habits=[],
    )

    assert first.user_id != second.user_id
    assert first.active_habits
    assert second.active_habits == ()
