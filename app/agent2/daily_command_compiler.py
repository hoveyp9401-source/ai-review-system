from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
import re
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.agent2.daily_commands import DailyCommand
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    REPORT_FIELDS,
    TypedDailyCommand,
    TypedDailyCommandExecution,
    execute_typed_daily_command,
)


CompilationStatus = Literal["compiled", "blocked", "unsupported"]


@dataclass(frozen=True)
class TypedDailyCompilation:
    status: CompilationStatus
    reason_code: str
    expected_reply_type: str
    command: TypedDailyCommand | None = None


@dataclass(frozen=True)
class TypedDailyBatchResult:
    status: Literal["executed", "blocked", "unsupported"]
    reason_code: str
    before: DailyReportMutationSnapshot
    after: DailyReportMutationSnapshot
    compilations: tuple[TypedDailyCompilation, ...]
    executions: tuple[TypedDailyCommandExecution, ...]
    should_write_db: bool
    execution_command_indices: tuple[int, ...] = ()


def apply_legacy_daily_commands_as_typed(
    legacy_commands: list[DailyCommand],
    *,
    message_id: str,
    snapshot: DailyReportMutationSnapshot,
    actor_user_id: UUID,
    expected_report_version: int,
    executed_idempotency_keys: Collection[str] = (),
    previous_snapshot: DailyReportMutationSnapshot | None = None,
) -> TypedDailyBatchResult:
    working = snapshot
    compilations: list[TypedDailyCompilation] = []
    executions: list[TypedDailyCommandExecution] = []
    execution_command_indices: list[int] = []
    known_keys = set(executed_idempotency_keys)
    next_expected_version = expected_report_version
    for command_index, legacy_command in enumerate(legacy_commands, start=1):
        if legacy_command.operation == "no_write":
            continue
        compilation = compile_typed_daily_command(
            legacy_command,
            message_id=message_id,
            command_index=command_index,
            snapshot=replace(working, version=next_expected_version),
            previous_snapshot=previous_snapshot,
        )
        compilations.append(compilation)
        if compilation.status != "compiled" or compilation.command is None:
            return TypedDailyBatchResult(
                compilation.status,
                compilation.reason_code,
                snapshot,
                snapshot,
                tuple(compilations),
                tuple(executions),
                should_write_db=False,
            )
        execution = execute_typed_daily_command(
            compilation.command,
            snapshot=working,
            actor_user_id=actor_user_id,
            executed_idempotency_keys=known_keys,
        )
        executions.append(execution)
        execution_command_indices.append(command_index - 1)
        if execution.validation.status == "blocked":
            return TypedDailyBatchResult(
                "blocked",
                execution.validation.reason_code,
                snapshot,
                snapshot,
                tuple(compilations),
                tuple(executions),
                should_write_db=False,
                execution_command_indices=tuple(execution_command_indices),
            )
        working = execution.after
        known_keys.add(compilation.command.idempotency_key)
        next_expected_version = working.version
    if not executions:
        return TypedDailyBatchResult(
            "unsupported",
            "forbidden_payload",
            snapshot,
            snapshot,
            tuple(compilations),
            (),
            should_write_db=False,
        )
    return TypedDailyBatchResult(
        "executed",
        "exact_target",
        snapshot,
        working,
        tuple(compilations),
        tuple(executions),
        should_write_db=working != snapshot,
        execution_command_indices=tuple(execution_command_indices),
    )


def compile_typed_daily_command(
    legacy_command: DailyCommand,
    *,
    message_id: str,
    command_index: int,
    snapshot: DailyReportMutationSnapshot,
    previous_snapshot: DailyReportMutationSnapshot | None = None,
) -> TypedDailyCompilation:
    text = "\n".join(str(value).strip() for value in legacy_command.content if str(value).strip())
    if not str(message_id or "").strip():
        return TypedDailyCompilation("blocked", "forbidden_payload", "clarify_intent")
    decision_id, sub_decision_id = _decision_ids(message_id, command_index)
    if legacy_command.operation in {"query_current", "query_history", "begin_edit"}:
        command_id = uuid5(sub_decision_id, "query_report")
        return TypedDailyCompilation(
            "compiled",
            "exact_target",
            "report_preview",
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="query_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={},
                idempotency_key=f"{message_id}:daily:{command_index}",
            ),
        )
    if legacy_command.operation == "copy_previous":
        if previous_snapshot is None:
            return TypedDailyCompilation("blocked", "target_not_found", "write_blocked")
        field_name = legacy_command.target_field
        selected_fields = REPORT_FIELDS if field_name == "all" else (field_name,)
        if any(field not in REPORT_FIELDS for field in selected_fields):
            return TypedDailyCompilation("blocked", "ambiguous_target", "clarify_target")
        sections = {
            field: list(getattr(previous_snapshot, field))
            for field in selected_fields
        }
        command_id = uuid5(sub_decision_id, f"copy_report:{sections}")
        return TypedDailyCompilation(
            "compiled",
            "exact_target",
            "operation_result",
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="copy_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={"sections": sections},
                idempotency_key=f"{message_id}:daily:{command_index}",
            ),
        )
    if legacy_command.operation == "clear":
        field_name = legacy_command.target_field
        if field_name not in {*REPORT_FIELDS, "all"}:
            return TypedDailyCompilation("blocked", "ambiguous_target", "clarify_target")
        command_id = uuid5(sub_decision_id, f"clear_report:{field_name}")
        return TypedDailyCompilation(
            "compiled",
            "exact_target",
            "operation_result",
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="clear_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={"field": field_name},
                idempotency_key=f"{message_id}:daily:{command_index}",
            ),
        )
    if legacy_command.operation == "revoke":
        command_id = uuid5(sub_decision_id, "reopen_report")
        return TypedDailyCompilation(
            "compiled",
            "exact_target",
            "operation_result",
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="reopen_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={},
                idempotency_key=f"{message_id}:daily:{command_index}",
            ),
        )
    if legacy_command.operation == "copy_current_to_tomorrow":
        items = list(snapshot.today_work)
        if not items:
            return TypedDailyCompilation("blocked", "target_not_found", "write_blocked")
        return _append_compilation(
            decision_id=decision_id,
            sub_decision_id=sub_decision_id,
            snapshot=snapshot,
            message_id=message_id,
            command_index=command_index,
            field_name="tomorrow_plan",
            items=items,
            identity="copy_current_to_tomorrow",
        )
    if legacy_command.operation == "complete_previous_plan":
        if previous_snapshot is None:
            return TypedDailyCompilation("blocked", "target_not_found", "write_blocked")
        items = _completed_previous_plan_items(list(previous_snapshot.tomorrow_plan))
        if not items:
            return TypedDailyCompilation("blocked", "target_not_found", "write_blocked")
        return _append_compilation(
            decision_id=decision_id,
            sub_decision_id=sub_decision_id,
            snapshot=snapshot,
            message_id=message_id,
            command_index=command_index,
            field_name="today_work",
            items=items,
            identity="complete_previous_plan",
        )
    if legacy_command.operation == "fill":
        field_name = legacy_command.target_field
        items = [str(value).strip() for value in legacy_command.content if str(value).strip()]
        if field_name not in {"today_work", "problems", "tomorrow_plan"} or not items:
            return TypedDailyCompilation("blocked", "forbidden_payload", "clarify_intent")
        patch = {"field": field_name, "items": items}
        command_id = uuid5(sub_decision_id, f"append_item:{patch}")
        return TypedDailyCompilation(
            "compiled",
            "exact_target",
            "operation_result",
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="append_item",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=f"{message_id}:daily:{command_index}",
            ),
        )
    if legacy_command.operation == "confirm":
        command_id = uuid5(sub_decision_id, "submit_report")
        return TypedDailyCompilation(
            "compiled",
            "exact_target",
            "operation_result",
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="submit_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={},
                idempotency_key=f"{message_id}:daily:{command_index}",
            ),
        )
    if legacy_command.operation != "edit":
        return TypedDailyCompilation("unsupported", "forbidden_payload", "operation_result")
    indices = _explicit_indices(text)
    is_merge = _has_merge_intent(text)
    field_name = legacy_command.target_field if legacy_command.target_field in {"today_work", "problems", "tomorrow_plan"} else ""
    if not field_name:
        return TypedDailyCompilation("blocked", "ambiguous_target", "clarify_target")
    if not indices and _has_delete_intent(text):
        text_target = _delete_text_reference(text)
        if text_target:
            matches = [index for index, value in enumerate(getattr(snapshot, field_name), start=1) if value == text_target]
            if len(matches) == 1:
                indices = matches
            elif not matches:
                return TypedDailyCompilation("blocked", "target_not_found", "clarify_target")
    if (is_merge and len(indices) < 2) or (not is_merge and len(indices) != 1):
        return TypedDailyCompilation("blocked", "ambiguous_target", "clarify_target")
    field_item_ids = tuple(snapshot.item_ids.get(field_name, ()))
    zero_indices = [index - 1 for index in indices]
    if any(item_index < 0 or item_index >= len(field_item_ids) for item_index in zero_indices):
        return TypedDailyCompilation("blocked", "target_not_found", "clarify_target")

    replacement = _replacement_from_text(text)
    if is_merge:
        command_type = "merge_items"
        patch = {"replacement": replacement} if replacement else {}
    elif _has_delete_intent(text):
        command_type = "delete_item"
        patch = {}
    elif replacement:
        command_type = "edit_item"
        patch = {"replacement": replacement}
    else:
        return TypedDailyCompilation("unsupported", "forbidden_payload", "operation_result")

    target_item_ids = tuple(field_item_ids[item_index] for item_index in zero_indices)
    command_id = uuid5(sub_decision_id, f"{command_type}:{target_item_ids}:{patch}")
    return TypedDailyCompilation(
        "compiled",
        "exact_target",
        "operation_result",
        TypedDailyCommand(
            command_id=command_id,
            decision_id=decision_id,
            sub_decision_id=sub_decision_id,
            command_type=command_type,
            report_id=snapshot.report_id,
            report_version=snapshot.version,
            target_item_ids=target_item_ids,
            patch=patch,
            idempotency_key=f"{message_id}:daily:{command_index}",
        ),
    )


def _decision_ids(message_id: str, command_index: int) -> tuple[UUID, UUID]:
    decision_id = uuid5(NAMESPACE_URL, f"agent2-decision:{message_id}")
    return decision_id, uuid5(decision_id, f"sub-decision:{command_index}")


def _append_compilation(
    *,
    decision_id: UUID,
    sub_decision_id: UUID,
    snapshot: DailyReportMutationSnapshot,
    message_id: str,
    command_index: int,
    field_name: str,
    items: list[str],
    identity: str,
) -> TypedDailyCompilation:
    patch = {"field": field_name, "items": items}
    return TypedDailyCompilation(
        "compiled",
        "exact_target",
        "operation_result",
        TypedDailyCommand(
            command_id=uuid5(sub_decision_id, f"{identity}:{patch}"),
            decision_id=decision_id,
            sub_decision_id=sub_decision_id,
            command_type="append_item",
            report_id=snapshot.report_id,
            report_version=snapshot.version,
            target_item_ids=(),
            patch=patch,
            idempotency_key=f"{message_id}:daily:{command_index}",
        ),
    )


def _completed_previous_plan_items(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip(" \t\r\n，。；;：:")
        text = re.sub(r"^(?:明天|明日|明儿|明个|次日|后续|今天|今日)\s*", "", text)
        text = re.sub(r"^(?:计划|安排|待办|事项)\s*", "", text)
        text = re.sub(r"^(?:继续|持续|开始做|开始|做)\s*", "", text).strip()
        if text and not text.startswith(("完成", "已完成")):
            text = f"完成{text}"
        if text and text not in result:
            result.append(text)
    return result


def _explicit_indices(text: str) -> list[int]:
    value = str(text or "")
    result: list[int] = []
    for group in re.findall(r"第?\s*(\d+(?:\s*[、,，和与]\s*(?:第?\s*)?\d+)+)\s*(?:条|项|个)", value):
        result.extend(int(item) for item in re.findall(r"\d+", group))
    result.extend(int(item) for item in re.findall(r"第?\s*(\d+)\s*(?:条|项|个)", value))
    return list(dict.fromkeys(result))


def _has_delete_intent(text: str) -> bool:
    return any(token in str(text or "") for token in ("删除", "删掉", "删了", "去掉", "移除"))


def _has_merge_intent(text: str) -> bool:
    return any(token in str(text or "") for token in ("合并", "合成", "并成"))


def _replacement_from_text(text: str) -> str:
    match = re.search(r"(?:改成|改为|修改为|替换为)\s*(.+)$", str(text or ""))
    return str(match.group(1) if match else "").strip(" 。；;，,")


def _delete_text_reference(text: str) -> str:
    value = str(text or "")
    match = re.search(r"(?:删除|删掉|去掉|移除)\s*[“\"](.+?)[”\"]", value)
    if match is None:
        match = re.search(r"把\s*[“\"](.+?)[”\"]\s*(?:删除|删掉|去掉|移除)", value)
    return str(match.group(1) if match else "").strip()
