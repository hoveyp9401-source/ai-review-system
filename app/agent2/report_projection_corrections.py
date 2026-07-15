from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


@dataclass(frozen=True)
class ProjectionSnapshot:
    projection_id: str
    tenant_id: str
    user_id: str
    case_id: str
    case_progress_id: str
    report_id: str
    report_item_id: str
    report_type: str
    projection_type: str
    status: str
    version: int


@dataclass(frozen=True)
class ProjectionCorrectionCommand:
    command_id: str
    tenant_id: str
    user_id: str
    projection_id: str
    expected_version: int
    operation: Literal["remove", "replace_text", "move_section"]
    replacement_text: str
    target_section: str
    idempotency_key: str


@dataclass(frozen=True)
class ProjectionCorrectionPlan:
    allowed: bool
    reason_code: str
    projection_id: str
    report_id: str
    report_item_id: str
    case_progress_id: str
    operation: str
    replacement_text: str
    target_section: str
    projection_status_after: str
    delete_case_fact: bool
    actual_write: bool = False


def parse_projection_correction(raw_text: str) -> tuple[str, str, str]:
    text = str(raw_text or "").strip()
    compact = re.sub(r"[\s，,。.!！?？;；、]+", "", text)
    if compact in {
        "这条别放日报", "别放日报", "只记案件", "案件进展保留日报删掉",
        "案件进展保留日报删除", "日报删掉", "日报删除",
    }:
        return "remove", "", ""
    if compact in {"改成明天计划", "移到明天计划", "这不是今天做的"}:
        return "move_section", "", "tomorrow_plan"
    if compact in {"改成今日工作", "移到今日工作", "这是今天做的"}:
        return "move_section", "", "today_work"
    match = re.match(
        r"^日报里换个说法\s*[：:]\s*(.+?)\s*$", text
    ) or re.match(r"^日报保留[，,]?换成\s*(.+?)\s*$", text)
    if match and match.group(1).strip():
        return "replace_text", match.group(1).strip(), ""
    return "", "", ""


def plan_projection_correction(
    command: ProjectionCorrectionCommand,
    projection: ProjectionSnapshot,
) -> ProjectionCorrectionPlan:
    reason = ""
    if (
        command.tenant_id != projection.tenant_id
        or command.user_id != projection.user_id
        or command.projection_id != projection.projection_id
    ):
        reason = "scope_mismatch"
    elif command.expected_version != projection.version:
        reason = "version_conflict"
    elif projection.status != "active":
        reason = "projection_not_active"
    elif command.operation not in {"remove", "replace_text", "move_section"}:
        reason = "unknown_operation"
    elif command.operation == "replace_text" and not command.replacement_text.strip():
        reason = "replacement_text_required"
    elif command.operation == "move_section" and command.target_section not in {
        "today_work", "tomorrow_plan"
    }:
        reason = "target_section_invalid"

    return ProjectionCorrectionPlan(
        allowed=not reason,
        reason_code=reason,
        projection_id=projection.projection_id,
        report_id=projection.report_id,
        report_item_id=projection.report_item_id,
        case_progress_id=projection.case_progress_id,
        operation=command.operation,
        replacement_text=command.replacement_text,
        target_section=command.target_section,
        projection_status_after=(
            "removed" if not reason and command.operation == "remove" else projection.status
        ),
        delete_case_fact=False,
        actual_write=False,
    )
