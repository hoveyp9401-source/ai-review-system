import json

from app.agent2.daily_commands import DailyCommand
from app.agent_core.daily_capability import run_daily_capability
from app.agent_core.execution_policy import AuthorizedAction, ExecutionPolicy
from app.agent_core.types import DailySnapshot


def _command(operation: str, text: str = "", *, target_field: str = "all") -> DailyCommand:
    return DailyCommand(
        operation=operation,  # type: ignore[arg-type]
        target_field=target_field,  # type: ignore[arg-type]
        content=[text] if text else [],
        should_write=True,
    )


def _policy(turn_id: str, *operations: str) -> ExecutionPolicy:
    return ExecutionPolicy(
        turn_id=turn_id,
        plan_id=f"plan-{turn_id}",
        authorized_actions=[
            AuthorizedAction(
                authorization_id=f"auth-{turn_id}-{index}",
                plan_id=f"plan-{turn_id}",
                turn_id=turn_id,
                workflow="daily_report",
                capability="daily_report",
                operation=operation,
                allowed_target_fields=("today_work", "problems", "tomorrow_plan"),
                write_policy="dry_run",
                reason="test grant",
            )
            for index, operation in enumerate(operations, start=1)
        ],
    )


def test_daily_capability_exposes_stable_global_and_field_item_refs():
    result = run_daily_capability(
        turn_id="turn-refs",
        snapshot=DailySnapshot(
            today_work=["\u5408\u540c\u5ba1\u6838", "\u6750\u6599\u6574\u7406"],
            problems=["\u5ba2\u6237\u672a\u53cd\u9988"],
            tomorrow_plan=["\u5357\u4eac\u51fa\u5dee"],
        ),
        commands=[],
    )

    assert [
        (item.global_index, item.field, item.field_index, item.text)
        for item in result.before_items
    ] == [
        (1, "today_work", 1, "\u5408\u540c\u5ba1\u6838"),
        (2, "today_work", 2, "\u6750\u6599\u6574\u7406"),
        (3, "problems", 1, "\u5ba2\u6237\u672a\u53cd\u9988"),
        (4, "tomorrow_plan", 1, "\u5357\u4eac\u51fa\u5dee"),
    ]
    assert result.after_items == result.before_items
    assert result.operation_ledger == []


def test_daily_capability_edits_by_global_index_and_records_before_after():
    result = run_daily_capability(
        turn_id="turn-edit-global",
        snapshot=DailySnapshot(
            today_work=["\u5408\u540c\u5ba1\u6838", "\u6750\u6599\u6574\u7406", "\u51fd\u4ef6\u8d77\u8349"],
            problems=["\u5ba2\u6237\u672a\u53cd\u9988", "\u6d41\u7a0b\u5361\u70b9"],
            tomorrow_plan=["\u5357\u4eac\u51fa\u5dee"],
        ),
        commands=[
            _command(
                "edit",
                "\u7b2c5\u6761\u6539\u6210\u6d41\u7a0b\u5361\u70b9\u5df2\u534f\u8c03",
            )
        ],
        execution_policy=_policy("turn-edit-global", "edit"),
    )

    assert result.after.problems == ["\u5ba2\u6237\u672a\u53cd\u9988", "\u6d41\u7a0b\u5361\u70b9\u5df2\u534f\u8c03"]
    assert result.actions[0]["edit_action"] == "replace_item"
    assert result.actions[0]["target_field"] == "problems"
    assert result.operation_ledger[0].before_state["problems"] == [
        "\u5ba2\u6237\u672a\u53cd\u9988",
        "\u6d41\u7a0b\u5361\u70b9",
    ]
    assert result.operation_ledger[0].after_state["problems"] == [
        "\u5ba2\u6237\u672a\u53cd\u9988",
        "\u6d41\u7a0b\u5361\u70b9\u5df2\u534f\u8c03",
    ]


def test_daily_capability_deletes_and_merges_with_item_ids_visible():
    delete_result = run_daily_capability(
        turn_id="turn-delete",
        snapshot=DailySnapshot(
            today_work=["\u5408\u540c\u5ba1\u6838"],
            problems=["\u95ee\u98981", "\u95ee\u98982", "\u95ee\u98983"],
            tomorrow_plan=[],
        ),
        commands=[_command("edit", "\u5220\u96642~3\u6761")],
        execution_policy=_policy("turn-delete", "edit"),
    )

    assert delete_result.after.problems == ["\u95ee\u98983"]
    assert delete_result.actions[0]["edit_action"] == "delete_item"
    assert delete_result.actions[0]["target_field"] == "problems"
    assert delete_result.actions[0]["removed_item_ids"]

    merge_result = run_daily_capability(
        turn_id="turn-merge",
        snapshot=DailySnapshot(
            today_work=["\u5408\u540c\u5ba1\u6838", "\u6750\u6599\u6574\u7406", "\u51fd\u4ef6\u8d77\u8349"],
            problems=[],
            tomorrow_plan=[],
        ),
        commands=[_command("edit", "\u5408\u5e76\u7b2c1\u6761\u548c\u7b2c2\u6761")],
        execution_policy=_policy("turn-merge", "edit"),
    )

    assert merge_result.after.today_work == ["\u5408\u540c\u5ba1\u6838\uff0c\u6750\u6599\u6574\u7406", "\u51fd\u4ef6\u8d77\u8349"]
    assert merge_result.actions[0]["edit_action"] == "merge_items"
    assert merge_result.actions[0]["merged_item_ids"]


def test_daily_capability_supports_revoke_and_copy_previous_without_real_write():
    revoke_result = run_daily_capability(
        turn_id="turn-revoke",
        snapshot=DailySnapshot(
            today_work=["\u5408\u540c\u5ba1\u6838"],
            problems=["\u6682\u65e0"],
            tomorrow_plan=["\u7ee7\u7eed\u8ddf\u8fdb"],
            status="completed",
        ),
        commands=[_command("revoke")],
        execution_policy=_policy("turn-revoke", "revoke"),
    )

    assert revoke_result.after.status == "collecting"
    assert revoke_result.operation_ledger[0].operation == "revoke"
    assert revoke_result.operation_ledger[0].write_policy == "dry_run"

    copy_result = run_daily_capability(
        turn_id="turn-copy",
        snapshot=DailySnapshot(),
        previous_snapshot=DailySnapshot(
            today_work=["\u6628\u65e5\u5de5\u4f5c"],
            problems=["\u6628\u65e5\u95ee\u9898"],
            tomorrow_plan=["\u6628\u65e5\u8ba1\u5212"],
            status="completed",
        ),
        commands=[_command("copy_previous")],
        execution_policy=_policy("turn-copy", "copy_previous"),
    )

    assert copy_result.after.today_work == ["\u6628\u65e5\u5de5\u4f5c"]
    assert copy_result.after.problems == ["\u6628\u65e5\u95ee\u9898"]
    assert copy_result.after.tomorrow_plan == ["\u6628\u65e5\u8ba1\u5212"]
    json.dumps(copy_result.as_dict(), ensure_ascii=False)
