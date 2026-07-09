from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import re
from typing import Any

from app.agent_core.execution_policy import AuthorizationDecision, ExecutionPolicy, authorize_capability_request
from app.agent_core.types import OperationLedgerEntry
from app.workflows.intake import WORKFLOW_MONTHLY_REPORT


MONTHLY_STATUS_COLLECTING = "collecting"
MONTHLY_STATUS_PENDING_CONFIRMATION = "pending_confirmation"
MONTHLY_STATUS_COMPLETED = "completed"
MONTHLY_FIELDS = ("reason", "next_target", "actions")


@dataclass(frozen=True)
class MonthlyMetricState:
    metric_no: int
    metric_name: str
    unit: str = ""
    reason: str = ""
    next_target: str = ""
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_no": self.metric_no,
            "metric_name": self.metric_name,
            "unit": self.unit,
            "reason": self.reason,
            "next_target": self.next_target,
            "actions": list(self.actions),
        }


@dataclass(frozen=True)
class MonthlySnapshot:
    task_id: str = ""
    department: str = ""
    leader: str = ""
    period_label: str = ""
    status: str = MONTHLY_STATUS_COLLECTING
    metrics: list[MonthlyMetricState] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "department": self.department,
            "leader": self.leader,
            "period_label": self.period_label,
            "status": self.status,
            "metrics": [metric.as_dict() for metric in self.metrics],
        }


@dataclass(frozen=True)
class MonthlyCommand:
    operation: str
    raw_input: str = ""
    task_id: str = ""
    reason: str = ""
    source_effect_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "task_id": self.task_id,
            "raw_input_hash": _hash_text(self.raw_input),
            "raw_input_chars": len(self.raw_input or ""),
            "reason": self.reason,
            "source_effect_type": self.source_effect_type,
        }


@dataclass(frozen=True)
class MonthlyCapabilityResult:
    before: MonthlySnapshot
    after: MonthlySnapshot
    commands: list[MonthlyCommand]
    message: str = ""
    touched_metrics: list[int] = field(default_factory=list)
    missing: dict[int, list[str]] = field(default_factory=dict)
    operation_ledger: list[OperationLedgerEntry] = field(default_factory=list)
    changed: bool = False
    read_only: bool = False
    confirmed_by_user: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "commands": [command.as_dict() for command in self.commands],
            "message": self.message,
            "touched_metrics": list(self.touched_metrics),
            "missing": {str(key): list(value) for key, value in self.missing.items()},
            "operation_ledger": [entry.as_dict() for entry in self.operation_ledger],
            "changed": self.changed,
            "read_only": self.read_only,
            "confirmed_by_user": self.confirmed_by_user,
        }


def run_monthly_capability(
    *,
    turn_id: str,
    snapshot: MonthlySnapshot,
    commands: list[MonthlyCommand],
    execution_policy: ExecutionPolicy | None = None,
) -> MonthlyCapabilityResult:
    """Apply monthly report commands to a snapshot without production side effects."""

    current = _normalized_snapshot(snapshot)
    entries: list[OperationLedgerEntry] = []
    touched: list[int] = []
    message = ""
    confirmed_by_user = False
    read_only = False

    for index, command in enumerate(commands, start=1):
        before_command = current
        decision = _authorize_monthly_command(turn_id=turn_id, command=command, execution_policy=execution_policy)
        if not decision.allowed:
            read_only = True
            entries.append(
                _operation_entry(
                    turn_id=turn_id,
                    index=index,
                    command=command,
                    decision=decision,
                    before=before_command,
                    after=before_command,
                    changed=False,
                    read_only=True,
                    safety_flags=decision.safety_flags,
                    reason=decision.reason,
                )
            )
            continue

        applied = _apply_monthly_command(before_command, command)
        current = applied.after
        touched.extend(applied.touched_metrics)
        message = applied.message or message
        confirmed_by_user = confirmed_by_user or applied.confirmed_by_user
        entries.append(
            _operation_entry(
                turn_id=turn_id,
                index=index,
                command=command,
                decision=decision,
                before=before_command,
                after=current,
                changed=applied.changed,
                read_only=False,
                safety_flags=["no_production_write"],
                reason=command.reason or decision.reason,
            )
        )

    missing = missing_by_metric(current)
    changed = current.as_dict() != _normalized_snapshot(snapshot).as_dict()
    return MonthlyCapabilityResult(
        before=_normalized_snapshot(snapshot),
        after=current,
        commands=list(commands),
        message=message,
        touched_metrics=sorted(set(touched)),
        missing=missing,
        operation_ledger=entries,
        changed=changed,
        read_only=read_only,
        confirmed_by_user=confirmed_by_user,
    )


@dataclass(frozen=True)
class _AppliedMonthlyCommand:
    after: MonthlySnapshot
    message: str
    touched_metrics: list[int]
    changed: bool = False
    confirmed_by_user: bool = False


def _apply_monthly_command(snapshot: MonthlySnapshot, command: MonthlyCommand) -> _AppliedMonthlyCommand:
    if command.operation == "confirm_submission":
        missing = missing_by_metric(snapshot)
        if missing:
            return _AppliedMonthlyCommand(
                after=replace(snapshot, status=MONTHLY_STATUS_COLLECTING),
                message=_progress_message(snapshot, missing, prefix="还有指标未填写完整，暂不能提交。"),
                touched_metrics=[],
                changed=snapshot.status != MONTHLY_STATUS_COLLECTING,
            )
        return _AppliedMonthlyCommand(
            after=replace(snapshot, status=MONTHLY_STATUS_COMPLETED),
            message=f"已确认提交本次团队月报。\n\n{render_monthly_preview(snapshot)}",
            touched_metrics=[],
            changed=snapshot.status != MONTHLY_STATUS_COMPLETED,
            confirmed_by_user=True,
        )

    if command.operation != "capture_reply":
        return _AppliedMonthlyCommand(after=snapshot, message=f"暂不支持月报操作：{command.operation}", touched_metrics=[])

    edit = _parse_metric_edit(command.raw_input, snapshot)
    if edit is not None:
        metric_no, field_name, value = edit
        after = _replace_metric(snapshot, metric_no, _updated_metric(snapshot, metric_no, field_name, value))
        missing = missing_by_metric(after)
        after = replace(after, status=MONTHLY_STATUS_PENDING_CONFIRMATION if not missing else MONTHLY_STATUS_COLLECTING)
        return _AppliedMonthlyCommand(
            after=after,
            message=_update_message(after, [metric_no], missing, prefix=f"已修改第{metric_no}项。"),
            touched_metrics=[metric_no],
            changed=after.as_dict() != snapshot.as_dict(),
        )

    blocks = _parse_monthly_reply_blocks(command.raw_input, snapshot)
    if not blocks:
        missing = missing_by_metric(snapshot)
        return _AppliedMonthlyCommand(
            after=snapshot,
            message=_progress_message(snapshot, missing, prefix="我还没识别到要写入哪项指标。"),
            touched_metrics=[],
            changed=False,
        )

    current = snapshot
    touched: list[int] = []
    for metric_no, fields in blocks:
        metric = _metric_by_no(current, metric_no)
        if metric is None:
            continue
        updated = metric
        if fields.get("reason"):
            updated = replace(updated, reason=str(fields["reason"]).strip())
        if fields.get("next_target"):
            updated = replace(updated, next_target=str(fields["next_target"]).strip())
        if fields.get("actions"):
            updated = replace(updated, actions=list(fields["actions"])[:4])
        current = _replace_metric(current, metric_no, updated)
        touched.append(metric_no)

    missing = missing_by_metric(current)
    current = replace(current, status=MONTHLY_STATUS_PENDING_CONFIRMATION if not missing else MONTHLY_STATUS_COLLECTING)
    return _AppliedMonthlyCommand(
        after=current,
        message=_update_message(current, touched, missing),
        touched_metrics=sorted(set(touched)),
        changed=current.as_dict() != snapshot.as_dict(),
    )


def missing_by_metric(snapshot: MonthlySnapshot) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for metric in snapshot.metrics:
        missing: list[str] = []
        if not metric.reason.strip():
            missing.append("未完成原因/存在问题")
        if not metric.next_target.strip():
            missing.append("下月目标")
        if not [item for item in metric.actions if item.strip()]:
            missing.append("行动方案")
        if missing:
            result[metric.metric_no] = missing
    return result


def render_monthly_preview(snapshot: MonthlySnapshot) -> str:
    lines = ["【团队月报预览】"]
    if snapshot.department or snapshot.period_label:
        lines.append(f"{snapshot.period_label} {snapshot.department}".strip())
    lines.append("")
    for metric in snapshot.metrics:
        lines.extend(
            [
                f"{metric.metric_no}. {metric.metric_name}",
                f"未完成原因/存在问题：{metric.reason or '未填写'}",
                f"下月目标：{metric.next_target or '未填写'}",
                "行动方案：",
            ]
        )
        actions = [item for item in metric.actions if item.strip()]
        if actions:
            lines.extend(f"{index}. {action}" for index, action in enumerate(actions[:4], start=1))
        else:
            lines.append("1. 未填写")
        lines.append("")
    return "\n".join(lines).rstrip()


def _parse_monthly_reply_blocks(raw_input: str, snapshot: MonthlySnapshot) -> list[tuple[int, dict[str, Any]]]:
    segments = _segment_metric_blocks(raw_input, snapshot)
    result: list[tuple[int, dict[str, Any]]] = []
    for metric_no, text in segments:
        fields = _parse_metric_fields(text)
        if any(fields.values()):
            result.append((metric_no, fields))
    return result


def _segment_metric_blocks(raw_input: str, snapshot: MonthlySnapshot) -> list[tuple[int, str]]:
    lines = [line.strip() for line in str(raw_input or "").replace("\r\n", "\n").split("\n") if line.strip()]
    metric_numbers = {metric.metric_no for metric in snapshot.metrics}
    metric_names = {metric.metric_no: _compact(metric.metric_name) for metric in snapshot.metrics}
    blocks: list[tuple[int, list[str]]] = []
    current_no: int | None = None
    current_lines: list[str] = []
    in_actions = False
    for line in lines:
        header = _metric_header_from_line(line, metric_numbers, metric_names, in_actions=in_actions)
        if header is not None:
            if current_no is not None:
                blocks.append((current_no, current_lines))
            current_no, rest = header
            current_lines = [rest] if rest else []
            in_actions = _has_action_label(rest)
            continue
        if current_no is not None:
            current_lines.append(line)
            if _has_action_label(line):
                in_actions = True
    if current_no is not None:
        blocks.append((current_no, current_lines))
    if not blocks:
        fallback_no = _single_missing_metric_no(snapshot)
        if fallback_no is not None:
            return [(fallback_no, str(raw_input or ""))]
    return [(metric_no, "\n".join(parts).strip()) for metric_no, parts in blocks if "\n".join(parts).strip()]


def _metric_header_from_line(
    line: str,
    metric_numbers: set[int],
    metric_names: dict[int, str],
    *,
    in_actions: bool,
) -> tuple[int, str] | None:
    value = _strip_markdown_prefix(line)
    match = re.match(r"^\s*(?:第)?(\d{1,3})(?:项|条)?\s*[\.\)、）:：、]?\s*(.*)$", value)
    if not match:
        return None
    metric_no = int(match.group(1))
    if metric_no not in metric_numbers:
        return None
    rest = match.group(2).strip()
    compact_rest = _compact(rest)
    has_field_label = _has_field_label(compact_rest)
    has_metric_name = bool(compact_rest and any(name and (name in compact_rest or compact_rest in name) for name in metric_names.values()))
    if in_actions and not has_field_label and not has_metric_name:
        return None
    if not rest or has_field_label or has_metric_name:
        return metric_no, rest
    return metric_no, rest


def _parse_metric_fields(text: str) -> dict[str, Any]:
    normalized = _strip_template_noise(str(text or "").strip())
    next_target = _extract_field(normalized, ("下月目标",), ("行动方案", "措施"))
    next_target = re.sub(r"^（[^）]+）\s*[:：]?\s*", "", next_target).strip()
    return {
        "reason": _extract_field(
            normalized,
            ("未完成原因/存在问题", "未完成原因", "存在问题", "原因"),
            ("下月目标", "目标", "行动方案", "措施"),
        ),
        "next_target": next_target,
        "actions": _extract_actions(normalized),
    }


def _extract_field(text: str, labels: tuple[str, ...], stop_labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    match = re.search(rf"(?:{label_pattern})\s*(?:[:：；;])?\s*(.*?)(?=(?:{stop_pattern})\s*(?:[:：；;])?|$)", text, flags=re.S)
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip()
    return _strip_edit_prefix(value).strip(" _-。；;")


def _extract_actions(text: str) -> list[str]:
    match = re.search(r"(?:行动方案|措施)\s*(?:[:：；;])?\s*(.+)$", text, flags=re.S)
    if not match:
        return []
    value = _strip_edit_prefix(match.group(1).strip())
    value = re.split(r"\n\s*(?:第)?\d{1,3}(?:项|条)?\s*[\.\)、）:：、]", value, maxsplit=1)[0].strip()
    parts = [part.strip() for part in re.split(r"(?:^|\n|\s)(?:\d+|[一二三四])[\.\、\)]\s*", value) if part.strip()]
    if len(parts) == 1 and parts[0] == value:
        parts = [part.strip() for part in re.split(r"[；;\n，,]+", value) if part.strip()]
    cleaned: list[str] = []
    for part in parts:
        item = _strip_edit_prefix(re.sub(r"\s+", " ", part).strip(" _-。；;"))
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned[:4]


def _parse_metric_edit(raw_input: str, snapshot: MonthlySnapshot) -> tuple[int, str, str] | None:
    text = str(raw_input or "").strip()
    match = re.search(
        r"(?:把)?第?(\d{1,3})(?:项|条)?[^。；;\n]{0,40}?"
        r"(未完成原因/存在问题|未完成原因|存在问题|下月目标|行动方案|措施)"
        r"[^。；;\n]{0,12}?(?:改成|改为|换成|调整为|变成)\s*[:：]?\s*(.+)$",
        text,
        flags=re.S,
    )
    if not match:
        return None
    metric_no = int(match.group(1))
    if _metric_by_no(snapshot, metric_no) is None:
        return None
    label = match.group(2)
    field_name = "actions" if label in {"行动方案", "措施"} else "next_target" if label == "下月目标" else "reason"
    return metric_no, field_name, match.group(3).strip()


def _updated_metric(snapshot: MonthlySnapshot, metric_no: int, field_name: str, value: str) -> MonthlyMetricState:
    metric = _metric_by_no(snapshot, metric_no)
    if metric is None:
        raise ValueError(f"missing metric: {metric_no}")
    if field_name == "actions":
        return replace(metric, actions=_split_edit_actions(value))
    if field_name == "next_target":
        return replace(metric, next_target=_strip_edit_prefix(value).strip())
    return replace(metric, reason=_strip_edit_prefix(value).strip())


def _split_edit_actions(value: str) -> list[str]:
    parsed = _extract_actions(f"行动方案：{value}")
    return parsed or [_strip_edit_prefix(value).strip()]


def _replace_metric(snapshot: MonthlySnapshot, metric_no: int, metric: MonthlyMetricState) -> MonthlySnapshot:
    return replace(snapshot, metrics=[metric if item.metric_no == metric_no else item for item in snapshot.metrics])


def _metric_by_no(snapshot: MonthlySnapshot, metric_no: int) -> MonthlyMetricState | None:
    for metric in snapshot.metrics:
        if metric.metric_no == metric_no:
            return metric
    return None


def _normalized_snapshot(snapshot: MonthlySnapshot) -> MonthlySnapshot:
    metrics = sorted(
        [
            MonthlyMetricState(
                metric_no=int(metric.metric_no),
                metric_name=str(metric.metric_name).strip(),
                unit=str(metric.unit or "").strip(),
                reason=str(metric.reason or "").strip(),
                next_target=str(metric.next_target or "").strip(),
                actions=[str(item).strip() for item in (metric.actions or []) if str(item).strip()][:4],
            )
            for metric in snapshot.metrics
            if str(metric.metric_name).strip()
        ],
        key=lambda metric: metric.metric_no,
    )
    return replace(snapshot, metrics=metrics)


def _single_missing_metric_no(snapshot: MonthlySnapshot) -> int | None:
    missing = missing_by_metric(snapshot)
    if len(missing) == 1:
        return next(iter(missing))
    if len(snapshot.metrics) == 1:
        return snapshot.metrics[0].metric_no
    return None


def _progress_message(snapshot: MonthlySnapshot, missing: dict[int, list[str]], *, prefix: str = "") -> str:
    completed = [metric.metric_name for metric in snapshot.metrics if metric.metric_no not in missing]
    remaining = [metric.metric_name for metric in snapshot.metrics if metric.metric_no in missing]
    if completed:
        return f"{prefix}当前已完成了{_bracket_join(completed)}的填写，请继续填写{_bracket_join(remaining)}。"
    return f"{prefix}请继续填写{_bracket_join(remaining)}。"


def _update_message(snapshot: MonthlySnapshot, touched: list[int], missing: dict[int, list[str]], *, prefix: str = "") -> str:
    if not missing:
        return f"{prefix}已收齐本次月报填报内容，请核对：\n\n{render_monthly_preview(snapshot)}\n\n需要调整可以继续说；确认无误请回复“确认提交”。".strip()
    return _progress_message(snapshot, missing, prefix=prefix)


def _bracket_join(values: list[str]) -> str:
    return "".join(f"【{value}】" for value in values if value)


def _authorize_monthly_command(
    *,
    turn_id: str,
    command: MonthlyCommand,
    execution_policy: ExecutionPolicy | None,
) -> AuthorizationDecision:
    return authorize_capability_request(
        execution_policy,
        turn_id=turn_id,
        capability=WORKFLOW_MONTHLY_REPORT,
        operation=command.operation,
        task_id=command.task_id,
        requested_write_policy="dry_run",
    )


def _operation_entry(
    *,
    turn_id: str,
    index: int,
    command: MonthlyCommand,
    decision: AuthorizationDecision,
    before: MonthlySnapshot,
    after: MonthlySnapshot,
    changed: bool,
    read_only: bool,
    safety_flags: list[str],
    reason: str,
) -> OperationLedgerEntry:
    return OperationLedgerEntry(
        operation_id=_operation_id(turn_id, command.operation, index),
        workflow=WORKFLOW_MONTHLY_REPORT,
        capability=WORKFLOW_MONTHLY_REPORT,
        operation=command.operation,
        write_policy=decision.write_policy if decision.allowed else "blocked",
        plan_id=decision.plan_id,
        authorization_id=decision.authorization_id,
        authorization_status="allowed" if decision.allowed else "denied",
        before_state=before.as_dict(),
        after_state=after.as_dict(),
        changed=changed,
        read_only=read_only,
        safety_flags=list(safety_flags),
        reason=reason,
    )


def _operation_id(turn_id: str, operation: str, index: int) -> str:
    raw = f"{turn_id}:{WORKFLOW_MONTHLY_REPORT}:{operation}:{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _strip_template_noise(text: str) -> str:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped in {"【请回复】本次需要填写的指标：", "本次需要填写的指标："}:
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def _strip_markdown_prefix(line: str) -> str:
    return re.sub(r"^(?:#{1,6}|>|[-*+])\s+", "", str(line or "").strip()).strip()


def _strip_edit_prefix(value: str) -> str:
    return re.sub(r"^(?:改成|改为|换成|调整为|变成)\s*[:：]?\s*", "", str(value or "").strip()).strip()


def _has_action_label(value: str) -> bool:
    return "行动方案" in str(value or "") or "措施" in str(value or "")


def _has_field_label(compact_value: str) -> bool:
    return any(
        token in compact_value
        for token in ("未完成原因", "存在问题", "原因", "下月目标", "目标", "行动方案", "措施")
    )


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
