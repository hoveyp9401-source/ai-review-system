from uuid import uuid4

from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.daily_command_compiler import apply_legacy_daily_commands_as_typed, compile_typed_daily_command
from app.agent2.daily_commands import DailyCommand


def test_exact_delete_uses_item_id_and_executes_without_raw_text():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="delete_item",
        report_id=report_id,
        report_version=3,
        target_item_ids=("tw-2",),
        patch={},
        idempotency_key="msg-100:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=3,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2"), "problems": (), "tomorrow_plan": ()},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.validation.reason_code == "exact_target"
    assert result.changed is True
    assert result.should_write_db is True
    assert result.after.today_work == ("完成合同审核",)
    assert result.after.item_ids["today_work"] == ("tw-1",)
    assert result.after.version == 4
    assert "raw_input" not in command.as_dict()


def test_delete_without_exact_item_id_is_blocked_as_ambiguous():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="delete_item",
        report_id=report_id,
        report_version=3,
        target_item_ids=(),
        patch={},
        idempotency_key="msg-101:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=3,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "blocked"
    assert result.validation.reason_code == "ambiguous_target"
    assert result.changed is False
    assert result.should_write_db is False
    assert result.after == snapshot


def test_single_item_mutation_with_multiple_targets_is_blocked_as_ambiguous():
    report_id = uuid4()
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=3,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    for command_type, patch in (("delete_item", {}), ("edit_item", {"replacement": "新内容"})):
        command = TypedDailyCommand(
            command_id=uuid4(),
            decision_id=uuid4(),
            sub_decision_id=uuid4(),
            command_type=command_type,
            report_id=report_id,
            report_version=3,
            target_item_ids=("tw-1", "tw-2"),
            patch=patch,
            idempotency_key=f"msg-multi:{command_type}",
        )

        result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

        assert result.validation.status == "blocked"
        assert result.validation.reason_code == "ambiguous_target"
        assert result.should_write_db is False
        assert result.after == snapshot


def test_exact_edit_replaces_one_item_and_preserves_its_id():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="edit_item",
        report_id=report_id,
        report_version=7,
        target_item_ids=("pb-1",),
        patch={"replacement": "客户材料已补齐"},
        idempotency_key="msg-102:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=7,
        status="collecting",
        problems=("客户材料未反馈",),
        item_ids={"today_work": (), "problems": ("pb-1",), "tomorrow_plan": ()},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.after.problems == ("客户材料已补齐",)
    assert result.after.item_ids["problems"] == ("pb-1",)
    assert result.after.version == 8


def test_exact_merge_uses_two_item_ids_and_preserves_the_first_id():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="merge_items",
        report_id=report_id,
        report_version=2,
        target_item_ids=("tw-1", "tw-2"),
        patch={},
        idempotency_key="msg-103:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=2,
        status="collecting",
        today_work=("完成合同审核", "整理案件材料", "起草函件"),
        item_ids={"today_work": ("tw-1", "tw-2", "tw-3")},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.after.today_work == ("完成合同审核，整理案件材料", "起草函件")
    assert result.after.item_ids["today_work"] == ("tw-1", "tw-3")
    assert result.after.version == 3


def test_append_adds_items_without_creating_a_confirmation_pending():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="append_item",
        report_id=report_id,
        report_version=0,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="msg-104:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=0,
        status="collecting",
        item_ids={},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.after.today_work == ("完成合同审核",)
    assert len(result.after.item_ids["today_work"]) == 1
    assert result.after.version == 1
    assert "pending" not in result.after.__dict__


def test_submit_complete_report_executes_directly():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="submit_report",
        report_id=report_id,
        report_version=5,
        target_item_ids=(),
        patch={},
        idempotency_key="msg-105:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=5,
        status="collecting",
        today_work=("完成合同审核",),
        problems=("暂无",),
        tomorrow_plan=("继续跟进案件",),
        item_ids={"today_work": ("tw-1",), "problems": ("pb-1",), "tomorrow_plan": ("tp-1",)},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.after.status == "completed"
    assert result.after.version == 6
    assert result.should_write_db is True


def test_stale_report_version_is_blocked_without_mutation():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="delete_item",
        report_id=report_id,
        report_version=8,
        target_item_ids=("tw-1",),
        patch={},
        idempotency_key="msg-106:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=9,
        status="collecting",
        today_work=("完成合同审核",),
        item_ids={"today_work": ("tw-1",)},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "blocked"
    assert result.validation.reason_code == "version_conflict"
    assert result.after == snapshot
    assert result.should_write_db is False


def test_replayed_idempotency_key_is_blocked_without_duplicate_write():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="append_item",
        report_id=report_id,
        report_version=1,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="stable-message-id:daily:1",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=1,
        status="collecting",
    )

    result = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=owner_id,
        executed_idempotency_keys={"stable-message-id:daily:1"},
    )

    assert result.validation.status == "duplicate"
    assert result.validation.reason_code == "duplicate_message"
    assert result.after == snapshot
    assert result.should_write_db is False
    assert result.audit.execution_result == "duplicate"


def test_execution_emits_minimal_replayable_audit_closure():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="delete_item",
        report_id=report_id,
        report_version=4,
        target_item_ids=("tw-1",),
        patch={},
        idempotency_key="stable-message-id:daily:2",
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=4,
        status="collecting",
        today_work=("完成合同审核",),
        item_ids={"today_work": ("tw-1",)},
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.audit.decision_id == command.decision_id
    assert result.audit.sub_decision_id == command.sub_decision_id
    assert result.audit.command_id == command.command_id
    assert result.audit.interaction_id
    assert result.audit.before_version == 4
    assert result.audit.after_version == 5
    assert result.audit.result == "executed"
    assert result.audit.reason == "exact_target"
    assert result.audit.idempotency_key == command.idempotency_key


def test_compiler_resolves_explicit_delete_to_stable_item_id():
    report_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=uuid4(),
        version=6,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="edit", target_field="today_work", content=["删除今日工作第2条"], should_write=True),
        message_id="ding-message-200",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.reason_code == "exact_target"
    assert compiled.expected_reply_type == "operation_result"
    assert compiled.command is not None
    assert compiled.command.command_type == "delete_item"
    assert compiled.command.target_item_ids == ("tw-2",)
    assert compiled.command.report_version == 6
    assert compiled.command.idempotency_key == "ding-message-200:daily:1"


def test_compiler_emits_no_command_for_ambiguous_deictic_delete():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=2,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="edit", target_field="today_work", content=["那条删掉"], should_write=True),
        message_id="ding-message-201",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "blocked"
    assert compiled.reason_code == "ambiguous_target"
    assert compiled.expected_reply_type == "clarify_target"
    assert compiled.command is None


def test_compiler_resolves_explicit_edit_to_item_id_and_replacement_patch():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=3,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="edit", target_field="today_work", content=["把今日工作第2条改成跟进恒大案件"], should_write=True),
        message_id="ding-message-202",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.command is not None
    assert compiled.command.command_type == "edit_item"
    assert compiled.command.target_item_ids == ("tw-2",)
    assert compiled.command.patch == {"replacement": "跟进恒大案件"}


def test_compiler_resolves_explicit_merge_to_two_item_ids():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=3,
        status="collecting",
        today_work=("完成合同审核", "整理案件材料", "起草函件"),
        item_ids={"today_work": ("tw-1", "tw-2", "tw-3")},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="edit", target_field="today_work", content=["合并今天工作第 1、2 条"], should_write=True),
        message_id="ding-message-203",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.command is not None
    assert compiled.command.command_type == "merge_items"
    assert compiled.command.target_item_ids == ("tw-1", "tw-2")
    assert compiled.command.patch == {}


def test_compiler_maps_fill_to_typed_append_without_raw_text():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=1,
        status="collecting",
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="fill", target_field="today_work", content=["完成合同审核"], should_write=True),
        message_id="ding-message-204",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.command is not None
    assert compiled.command.command_type == "append_item"
    assert compiled.command.target_item_ids == ()
    assert compiled.command.patch == {"field": "today_work", "items": ["完成合同审核"]}
    assert set(compiled.command.as_dict()) == {
        "command_id",
        "decision_id",
        "sub_decision_id",
        "command_type",
        "report_id",
        "report_version",
        "target_item_ids",
        "patch",
        "idempotency_key",
    }


def test_compiler_maps_explicit_submit_to_typed_submit():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=4,
        status="collecting",
        today_work=("完成合同审核",),
        problems=("暂无",),
        tomorrow_plan=("继续跟进案件",),
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="confirm", target_field="all", content=[], should_write=True),
        message_id="ding-message-205",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.command is not None
    assert compiled.command.command_type == "submit_report"
    assert compiled.command.target_item_ids == ()
    assert compiled.command.patch == {}


def test_legacy_adapter_compiles_then_executes_through_typed_seam():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=10,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    result = apply_legacy_daily_commands_as_typed(
        [DailyCommand(operation="edit", target_field="today_work", content=["删除今日工作第2条"], should_write=True)],
        message_id="ding-message-206",
        snapshot=snapshot,
        actor_user_id=owner_id,
        expected_report_version=10,
    )

    assert result.status == "executed"
    assert result.should_write_db is True
    assert result.after.today_work == ("完成合同审核",)
    assert result.after.version == 11
    assert result.executions[0].command.command_type == "delete_item"
    assert result.executions[0].audit.result == "executed"


def test_compiler_emits_no_command_for_ambiguous_merge():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=2,
        status="collecting",
        today_work=("alpha", "beta", "gamma"),
        item_ids={"today_work": ("item-1", "item-2", "item-3")},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="edit", target_field="today_work", content=["这几条合并一下"], should_write=True),
        message_id="ding-message-207",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "blocked"
    assert compiled.reason_code == "ambiguous_target"
    assert compiled.expected_reply_type == "clarify_target"
    assert compiled.command is None


def test_same_message_replayed_three_times_has_one_business_write():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=0,
        status="collecting",
    )
    command = DailyCommand(operation="fill", target_field="today_work", content=["完成合同审核"], should_write=True)
    executed_keys: set[str] = set()
    working = snapshot
    actual_writes = 0

    for _ in range(3):
        result = apply_legacy_daily_commands_as_typed(
            [command],
            message_id="same-ding-message",
            snapshot=working,
            actor_user_id=owner_id,
            expected_report_version=working.version,
            executed_idempotency_keys=executed_keys,
        )
        if result.should_write_db:
            actual_writes += 1
            working = result.after
            executed_keys.update(execution.command.idempotency_key for execution in result.executions)

    assert actual_writes == 1
    assert working.today_work == ("完成合同审核",)
    assert working.version == 1


def test_validator_does_not_interpret_natural_language_payload_semantics():
    report_id = uuid4()
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=1,
        status="collecting",
    )

    for index, payload in enumerate(("删除第2条", "印章流程是什么？"), start=1):
        command = TypedDailyCommand(
            command_id=uuid4(),
            decision_id=uuid4(),
            sub_decision_id=uuid4(),
            command_type="append_item",
            report_id=report_id,
            report_version=1,
            target_item_ids=(),
            patch={"field": "today_work", "items": [payload]},
            idempotency_key=f"forbidden-payload-{index}:daily:1",
        )

        result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

        assert result.validation.status == "authorized"
        assert result.should_write_db is True
        assert result.after.today_work == (payload,)

    # Whether either string is a real daily-report fact, a question, or an
    # operation request belongs to Agent2's model review before this
    # deterministic executor is called. Reintroducing text-pattern checks here
    # would create an Agent1-style semantic side door.


def test_compiler_resolves_unique_exact_text_delete_to_item_id():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=3,
        status="collecting",
        today_work=("完成合同审核", "跟进保利案件"),
        item_ids={"today_work": ("tw-1", "tw-2")},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="edit", target_field="today_work", content=["删除“跟进保利案件”"], should_write=True),
        message_id="ding-message-208",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.command is not None
    assert compiled.command.command_type == "delete_item"
    assert compiled.command.target_item_ids == ("tw-2",)


def test_high_impact_clear_all_does_not_fall_back_to_raw_legacy_executor():
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=uuid4(),
        version=3,
        status="collecting",
        today_work=("完成合同审核",),
        item_ids={"today_work": ("tw-1",)},
    )

    compiled = compile_typed_daily_command(
        DailyCommand(operation="clear", target_field="all", content=["清空今日日报"], should_write=True),
        message_id="ding-message-209",
        command_index=1,
        snapshot=snapshot,
    )

    assert compiled.status == "compiled"
    assert compiled.reason_code == "exact_target"
    assert compiled.expected_reply_type == "operation_result"
    assert compiled.command is not None
    assert compiled.command.command_type == "clear_report"
    assert compiled.command.patch == {"field": "all"}
    assert "raw_input" not in compiled.command.as_dict()


def test_typed_batch_ignores_no_write_segments_between_valid_daily_writes():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=0,
        status="collecting",
    )

    result = apply_legacy_daily_commands_as_typed(
        [
            DailyCommand(operation="fill", target_field="today_work", content=["完成合同审核"], should_write=True),
            DailyCommand(operation="no_write", target_field="none", content=[], should_write=False, reason="read-only side segment"),
            DailyCommand(operation="fill", target_field="tomorrow_plan", content=["继续跟进案件"], should_write=True),
        ],
        message_id="multi-segment-message",
        snapshot=snapshot,
        actor_user_id=owner_id,
        expected_report_version=0,
    )

    assert result.status == "executed"
    assert result.after.today_work == ("完成合同审核",)
    assert result.after.tomorrow_plan == ("继续跟进案件",)
    assert result.after.version == 2
    assert [execution.command.command_type for execution in result.executions] == ["append_item", "append_item"]
    assert result.execution_command_indices == (0, 2)


def test_validator_blocks_wrong_owner_locked_report_and_missing_item():
    report_id = uuid4()
    owner_id = uuid4()
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="delete_item",
        report_id=report_id,
        report_version=1,
        target_item_ids=("tw-1",),
        patch={},
        idempotency_key="validator-coverage:daily:1",
    )
    collecting = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=1,
        status="collecting",
        today_work=("alpha",),
        item_ids={"today_work": ("tw-1",)},
    )

    wrong_owner = execute_typed_daily_command(command, snapshot=collecting, actor_user_id=uuid4())
    locked = execute_typed_daily_command(
        command,
        snapshot=DailyReportMutationSnapshot(**{**collecting.__dict__, "status": "completed"}),
        actor_user_id=owner_id,
    )
    missing = execute_typed_daily_command(
        command,
        snapshot=DailyReportMutationSnapshot(**{**collecting.__dict__, "today_work": (), "item_ids": {}}),
        actor_user_id=owner_id,
    )

    assert wrong_owner.validation.reason_code == "forbidden_payload"
    assert locked.validation.reason_code == "invalid_report_state"
    assert missing.validation.reason_code == "target_not_found"
    assert not any(result.should_write_db for result in (wrong_owner, locked, missing))


def test_typed_query_report_is_read_only_and_allowed_for_completed_report():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=4,
        status="completed",
        today_work=("完成审核",),
        item_ids={"today_work": ("tw-1",)},
    )
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="query_report",
        report_id=snapshot.report_id,
        report_version=4,
        target_item_ids=(),
        patch={},
        idempotency_key="query-message:daily:1",
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.after == snapshot
    assert result.changed is False
    assert result.should_write_db is False
    assert result.audit.actual_write is False


def test_typed_copy_report_merges_previous_sections_without_duplicates():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=1,
        status="collecting",
        today_work=("已有事项",),
        item_ids={"today_work": ("tw-1",), "problems": (), "tomorrow_plan": ()},
    )
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="copy_report",
        report_id=snapshot.report_id,
        report_version=1,
        target_item_ids=(),
        patch={
            "sections": {
                "today_work": ["已有事项", "昨日新增"],
                "problems": ["暂无"],
                "tomorrow_plan": ["继续跟进"],
            }
        },
        idempotency_key="copy-message:daily:1",
    )

    result = execute_typed_daily_command(command, snapshot=snapshot, actor_user_id=owner_id)

    assert result.validation.status == "authorized"
    assert result.should_write_db is True
    assert result.after.version == 2
    assert result.after.today_work == ("已有事项", "昨日新增")
    assert result.after.problems == ("暂无",)
    assert result.after.tomorrow_plan == ("继续跟进",)
    assert len(result.after.item_ids["today_work"]) == 2


def test_legacy_daily_query_and_copy_are_compiled_to_typed_commands():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=0,
        status="collecting",
    )
    previous = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=3,
        status="completed",
        today_work=("昨日工作",),
        problems=("暂无",),
        tomorrow_plan=("今日计划",),
    )

    query = compile_typed_daily_command(
        DailyCommand(operation="query_history", target_field="all", content=[], should_write=False),
        message_id="query-history",
        command_index=1,
        snapshot=snapshot,
        previous_snapshot=previous,
    )
    copy = compile_typed_daily_command(
        DailyCommand(operation="copy_previous", target_field="all", content=[], should_write=True),
        message_id="copy-history",
        command_index=1,
        snapshot=snapshot,
        previous_snapshot=previous,
    )

    assert query.status == "compiled"
    assert query.command is not None and query.command.command_type == "query_report"
    assert copy.status == "compiled"
    assert copy.command is not None and copy.command.command_type == "copy_report"
    assert copy.command.patch["sections"]["today_work"] == ["昨日工作"]


def test_typed_clear_report_can_clear_one_section_or_all_without_raw_text():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=2,
        status="collecting",
        today_work=("工作一",),
        problems=("风险一",),
        tomorrow_plan=("计划一",),
        item_ids={
            "today_work": ("tw-1",),
            "problems": ("pr-1",),
            "tomorrow_plan": ("tp-1",),
        },
    )
    section_command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="clear_report",
        report_id=snapshot.report_id,
        report_version=2,
        target_item_ids=(),
        patch={"field": "problems"},
        idempotency_key="clear-section:daily:1",
    )

    section = execute_typed_daily_command(section_command, snapshot=snapshot, actor_user_id=owner_id)
    all_command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="clear_report",
        report_id=snapshot.report_id,
        report_version=section.after.version,
        target_item_ids=(),
        patch={"field": "all"},
        idempotency_key="clear-all:daily:2",
    )
    all_result = execute_typed_daily_command(all_command, snapshot=section.after, actor_user_id=owner_id)

    assert section.validation.status == "authorized"
    assert section.after.problems == ()
    assert section.after.today_work == ("工作一",)
    assert section.after.version == 3
    assert all_result.validation.status == "authorized"
    assert all_result.after.today_work == all_result.after.problems == all_result.after.tomorrow_plan == ()
    assert all_result.after.version == 4
    assert "raw_input" not in all_command.as_dict()


def test_typed_reopen_report_only_allows_completed_report():
    owner_id = uuid4()
    completed = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=5,
        status="completed",
    )
    command = TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="reopen_report",
        report_id=completed.report_id,
        report_version=5,
        target_item_ids=(),
        patch={},
        idempotency_key="reopen:daily:1",
    )

    result = execute_typed_daily_command(command, snapshot=completed, actor_user_id=owner_id)
    invalid = execute_typed_daily_command(
        command,
        snapshot=DailyReportMutationSnapshot(**{**completed.__dict__, "status": "collecting"}),
        actor_user_id=owner_id,
    )

    assert result.validation.status == "authorized"
    assert result.after.status == "collecting"
    assert result.after.version == 6
    assert result.should_write_db is True
    assert invalid.validation.reason_code == "invalid_report_state"


def test_remaining_legacy_daily_operations_compile_to_closed_typed_vocabulary():
    owner_id = uuid4()
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=1,
        status="collecting",
        today_work=("完成合同审核",),
        problems=("暂无",),
        tomorrow_plan=(),
        item_ids={"today_work": ("tw-1",), "problems": ("pr-1",), "tomorrow_plan": ()},
    )
    previous = DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=4,
        status="completed",
        tomorrow_plan=("明天继续跟进执行案件",),
    )
    operations = (
        (DailyCommand(operation="clear", target_field="all", should_write=True), "clear_report"),
        (DailyCommand(operation="revoke", target_field="all", should_write=True), "reopen_report"),
        (
            DailyCommand(operation="copy_current_to_tomorrow", target_field="tomorrow_plan", should_write=True),
            "append_item",
        ),
        (
            DailyCommand(operation="complete_previous_plan", target_field="today_work", should_write=True),
            "append_item",
        ),
    )

    compiled = [
        compile_typed_daily_command(
            legacy,
            message_id=f"remaining-{index}",
            command_index=1,
            snapshot=(
                DailyReportMutationSnapshot(**{**snapshot.__dict__, "status": "completed"})
                if legacy.operation == "revoke"
                else snapshot
            ),
            previous_snapshot=previous,
        )
        for index, (legacy, _) in enumerate(operations, start=1)
    ]

    assert [item.status for item in compiled] == ["compiled"] * 4
    assert [item.command.command_type for item in compiled if item.command] == [
        expected for _, expected in operations
    ]
    complete = compiled[-1].command
    assert complete is not None
    assert complete.patch == {"field": "today_work", "items": ["完成跟进执行案件"]}
