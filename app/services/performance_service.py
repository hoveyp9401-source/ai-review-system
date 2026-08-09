from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PerformanceSubmission, PerformanceTask, User
from app.utils.time import now_in_timezone


PERFORMANCE_COLLECTING = "collecting"
PERFORMANCE_PENDING_CONFIRMATION = "pending_confirmation"
PERFORMANCE_COMPLETED = "completed"
PERFORMANCE_CANCELLED = "cancelled"

ACTIVE_PERFORMANCE_STATUSES = {PERFORMANCE_COLLECTING, PERFORMANCE_PENDING_CONFIRMATION}
PERFORMANCE_PLACEHOLDER_COLOR = "#003A8C"
NO_ACTIVE_PERFORMANCE_TASK_MESSAGE = (
    "我识别到这是一段绩效/月报填报格式，但当前没有找到与这段内容匹配的待填绩效任务，所以没有写入日报。\n"
    "如果只是测试格式，可以不用回复；如果要正式填报，请先确认当前对话对应的月报/绩效收集任务。"
)


@dataclass(frozen=True)
class PerformanceSubmitResult:
    submission_id: str
    task_id: str
    status: str
    message: str
    touched_metrics: list[int]
    missing: dict[int, list[str]]
    responses: list[dict[str, Any]]
    confirmed_by_user: bool = False


class PerformanceTaskService:
    def __init__(self, settings: Any):
        self.settings = settings

    async def create_blank_task(
        self,
        session: AsyncSession,
        *,
        title: str,
        period_label: str,
        metrics: list[dict[str, Any]],
        recipients: list[User],
        created_by: str = "",
        now: datetime | None = None,
    ) -> PerformanceTask:
        clean_metrics = normalize_metrics(metrics)
        if not clean_metrics:
            raise ValueError("At least one metric is required.")
        received_at = now or now_in_timezone(getattr(self.settings, "timezone", "Asia/Shanghai"))
        task = PerformanceTask(
            title=title.strip() or f"{period_label}团队绩效填报",
            period_label=period_label.strip(),
            metrics_json=clean_metrics,
            status="active",
            created_by=created_by.strip(),
        )
        session.add(task)
        await session.flush()
        for user in recipients:
            snapshot = {
                "task_id": str(task.id),
                "title": task.title,
                "period_label": task.period_label,
                "metrics": clean_metrics,
                "sent_at": received_at.isoformat(),
            }
            session.add(
                PerformanceSubmission(
                    task_id=task.id,
                    user_id=user.id,
                    team_id=user.team_id,
                    recipient_name=getattr(user, "name", "") or "",
                    sent_snapshot_json=snapshot,
                    responses_json=initial_responses(clean_metrics),
                    input_fragments_json=[],
                    status=PERFORMANCE_COLLECTING,
                    confirmed_by_user=False,
                )
            )
        await session.flush()
        return task

    async def get_active_submission(self, session: AsyncSession, user_id: uuid.UUID) -> PerformanceSubmission | None:
        submissions = await self.get_active_submissions(session, user_id)
        return submissions[0] if submissions else None

    async def get_active_submissions(self, session: AsyncSession, user_id: uuid.UUID) -> list[PerformanceSubmission]:
        result = await session.execute(
            select(PerformanceSubmission)
            .join(PerformanceTask, PerformanceSubmission.task_id == PerformanceTask.id)
            .where(
                PerformanceSubmission.user_id == user_id,
                PerformanceSubmission.status.in_(ACTIVE_PERFORMANCE_STATUSES),
                PerformanceTask.status == "active",
            )
            .order_by(PerformanceSubmission.created_at.desc())
        )
        return list(result.scalars().all())

    async def submit_text(
        self,
        session: AsyncSession,
        *,
        user: User,
        raw_input: str,
        source: str,
        require_performance_signal: bool = False,
        now: datetime | None = None,
    ) -> PerformanceSubmitResult | None:
        active_submissions = await self.get_active_submissions(session, user.id)
        if not active_submissions:
            return None
        submission: PerformanceSubmission | None = None
        if require_performance_signal:
            for candidate_submission in active_submissions:
                candidate_metrics = submission_metrics(candidate_submission)
                candidate_responses = list(candidate_submission.responses_json or [])
                if is_performance_reply_candidate(
                    metrics=candidate_metrics,
                    responses=candidate_responses,
                    raw_input=raw_input,
                    status=candidate_submission.status,
                ):
                    submission = candidate_submission
                    break
            if submission is None:
                return None
        else:
            submission = active_submissions[0]
        metrics = submission_metrics(submission)
        responses = list(submission.responses_json or [])
        received_at = now or now_in_timezone(getattr(user, "timezone", getattr(self.settings, "timezone", "Asia/Shanghai")))
        result = apply_performance_reply(
            metrics=metrics,
            responses=responses,
            raw_input=raw_input,
            status=submission.status,
        )
        submission.responses_json = result.responses
        submission.status = result.status
        submission.confirmed_by_user = result.confirmed_by_user
        if result.confirmed_by_user:
            submission.submitted_at = received_at
        submission.input_fragments_json = [
            *(submission.input_fragments_json or []),
            {
                "received_at": received_at.isoformat(),
                "source": source,
                "raw_input": raw_input,
                "touched_metrics": result.touched_metrics,
                "missing": result.missing,
                "status_after": result.status,
            },
        ]
        await session.flush()
        return PerformanceSubmitResult(
            submission_id=str(submission.id),
            task_id=str(submission.task_id),
            status=result.status,
            message=result.message,
            touched_metrics=result.touched_metrics,
            missing=result.missing,
            responses=result.responses,
            confirmed_by_user=result.confirmed_by_user,
        )


@dataclass(frozen=True)
class ParsedPerformanceResult:
    status: str
    message: str
    touched_metrics: list[int]
    missing: dict[int, list[str]]
    responses: list[dict[str, Any]]
    confirmed_by_user: bool = False


def normalize_metrics(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, metric in enumerate(metrics, start=1):
        if not isinstance(metric, dict):
            continue
        name = str(metric.get("name") or metric.get("metric_name") or "").strip()
        if not name:
            continue
        metric_no = _to_int(metric.get("metric_no") or metric.get("no") or index) or index
        unit = str(metric.get("unit") or "").strip()
        result.append(
            {
                "metric_no": metric_no,
                "name": name,
                "unit": unit,
                "display_lines": [str(item).strip() for item in (metric.get("display_lines") or []) if str(item).strip()],
            }
        )
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for metric in result:
        metric_no = int(metric["metric_no"])
        if metric_no in seen:
            continue
        seen.add(metric_no)
        unique.append(metric)
    return sorted(unique, key=lambda item: int(item["metric_no"]))


def initial_responses(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "metric_no": int(metric["metric_no"]),
            "metric_name": str(metric["name"]),
            "unit": str(metric.get("unit") or ""),
            "reason": "",
            "next_target": "",
            "actions": [],
        }
        for metric in metrics
    ]


def submission_metrics(submission: PerformanceSubmission) -> list[dict[str, Any]]:
    snapshot = submission.sent_snapshot_json if isinstance(submission.sent_snapshot_json, dict) else {}
    return normalize_metrics(snapshot.get("metrics") or [])


def build_performance_prompt(task: PerformanceTask, submission: PerformanceSubmission) -> str:
    return build_performance_reply_prompt(submission_metrics(submission))


def build_performance_overview_markdown(metrics: list[dict[str, Any]]) -> str:
    lines = ["【指标完成情况概览】", ""]
    for metric in normalize_metrics(metrics):
        lines.append(f"**{metric['name']}**")
        for display in metric.get("display_lines") or []:
            lines.append(_colorize_performance_placeholders(display))
        lines.append("")
    return "\n".join(lines).rstrip()


def _colorize_performance_placeholders(value: str) -> str:
    return str(value).replace("NA#", f'<font color="{PERFORMANCE_PLACEHOLDER_COLOR}">NA#</font>')


def build_performance_reply_prompt(metrics: list[dict[str, Any]]) -> str:
    lines = [
        "【请回复】本次需要填写的指标：",
        "可以一次性回复全部指标，也可以先回其中几项，后续再补。",
        "",
    ]
    for metric in normalize_metrics(metrics):
        unit = str(metric.get("unit") or "")
        target_label = f"下月目标（{unit}）：" if unit else "下月目标："
        lines.extend(
            [
                f"{metric['metric_no']}. 【{metric['name']}】",
                "未完成原因/存在问题：",
                target_label,
                "行动方案：",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def build_complete_performance_report(metrics: list[dict[str, Any]], responses: list[dict[str, Any]]) -> str:
    clean_metrics = normalize_metrics(metrics)
    response_map = {int(item.get("metric_no") or 0): item for item in responses if isinstance(item, dict)}
    lines = ["【完整绩效汇报预览】", ""]
    for metric in clean_metrics:
        metric_no = int(metric["metric_no"])
        response = response_map.get(metric_no) or {}
        lines.append(f"{metric_no}. {metric['name']}")
        lines.append("（1）本月绩效完成情况")
        for display in metric.get("display_lines") or []:
            lines.append(display)
        lines.append("（2）未完成原因/存在问题：")
        lines.append(str(response.get("reason") or "未填写").strip())
        lines.append("下月绩效目标及行动方案")
        lines.append(f"下月目标：{str(response.get('next_target') or '未填写').strip()}")
        lines.append("行动方案：")
        actions = [str(item).strip() for item in (response.get("actions") or []) if str(item).strip()]
        if actions:
            for index, action in enumerate(actions[:4], start=1):
                lines.append(f"{index}. {action}")
        else:
            lines.append("1. 未填写")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_performance_message_pair(task: PerformanceTask, submission: PerformanceSubmission) -> tuple[str, str]:
    metrics = submission_metrics(submission)
    overview = build_performance_overview_markdown(metrics)
    reply_prompt = build_performance_reply_prompt(metrics)
    return overview, reply_prompt


def _build_legacy_performance_prompt(task: PerformanceTask, submission: PerformanceSubmission) -> str:
    metrics = submission_metrics(submission)
    lines = [
        f"{task.title}",
        "",
        "请补充每个指标的：未完成原因/存在问题、下月目标、行动方案。",
        "可以一次性回复所有指标，也可以分批按编号回复。",
        "",
        "本次需要填写的指标：",
    ]
    for metric in metrics:
        lines.append(f"{metric['metric_no']}. {metric['name']}")
        for display in metric.get("display_lines") or []:
            lines.append(f"   {display}")
        unit = str(metric.get("unit") or "")
        target_label = f"下月目标（{unit}）" if unit else "下月目标"
        lines.append(f"   需回复：未完成原因/存在问题；{target_label}；行动方案")
    return "\n".join(lines)


def apply_performance_reply(
    *,
    metrics: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    raw_input: str,
    status: str = PERFORMANCE_COLLECTING,
) -> ParsedPerformanceResult:
    clean_metrics = normalize_metrics(metrics)
    merged = _responses_by_no(clean_metrics, responses)
    if _is_confirm_reply(raw_input):
        missing = missing_by_metric(clean_metrics, list(merged.values()))
        if missing:
            return ParsedPerformanceResult(
                status=PERFORMANCE_COLLECTING,
                message=_build_progress_message(clean_metrics, missing, prefix="还不能提交。"),
                touched_metrics=[],
                missing=missing,
                responses=_ordered_responses(clean_metrics, merged),
                confirmed_by_user=False,
            )
        ordered = _ordered_responses(clean_metrics, merged)
        preview = build_complete_performance_report(clean_metrics, ordered)
        return ParsedPerformanceResult(
            status=PERFORMANCE_COMPLETED,
            message=f"已确认提交本次团队绩效填报。\n\n{preview}",
            touched_metrics=[],
            missing={},
            responses=ordered,
            confirmed_by_user=True,
        )

    if _looks_like_complete_performance_preview(raw_input):
        ordered = _ordered_responses(clean_metrics, merged)
        missing = missing_by_metric(clean_metrics, ordered)
        next_status = PERFORMANCE_PENDING_CONFIRMATION if not missing else PERFORMANCE_COLLECTING
        return ParsedPerformanceResult(
            status=next_status,
            message=_build_preview_replay_message(clean_metrics, missing, ordered),
            touched_metrics=[],
            missing=missing,
            responses=ordered,
            confirmed_by_user=False,
        )

    clear_metric_numbers = _parse_metric_clear_instruction(raw_input, clean_metrics)
    if clear_metric_numbers:
        initial_by_no = {int(item["metric_no"]): item for item in initial_responses(clean_metrics)}
        for metric_no in clear_metric_numbers:
            merged[metric_no] = initial_by_no[metric_no]
        ordered = _ordered_responses(clean_metrics, merged)
        missing = missing_by_metric(clean_metrics, ordered)
        touched_text = "、".join(f"第{metric_no}项" for metric_no in clear_metric_numbers)
        return ParsedPerformanceResult(
            status=PERFORMANCE_COLLECTING,
            message=_build_progress_message(clean_metrics, missing, prefix=f"已清空{touched_text}。"),
            touched_metrics=clear_metric_numbers,
            missing=missing,
            responses=ordered,
            confirmed_by_user=False,
        )

    replacement = _parse_replacement_instruction(raw_input)
    if replacement is not None:
        old_value, new_value = replacement
        touched = _apply_response_replacement(merged, old_value, new_value)
        if touched:
            ordered = _ordered_responses(clean_metrics, merged)
            missing = missing_by_metric(clean_metrics, ordered)
            next_status = PERFORMANCE_PENDING_CONFIRMATION if not missing else PERFORMANCE_COLLECTING
            return ParsedPerformanceResult(
                status=next_status,
                message=_build_replacement_message(clean_metrics, touched, missing, ordered, old_value, new_value),
                touched_metrics=sorted(set(touched)),
                missing=missing,
                responses=ordered,
                confirmed_by_user=False,
            )

    blocks = parse_performance_reply_blocks(raw_input, clean_metrics, merged)
    if not blocks:
        missing = missing_by_metric(clean_metrics, list(merged.values()))
        hint = "我还没识别到要写入哪个指标。请按“编号 + 未完成原因/存在问题 + 下月目标 + 行动方案”回复，例如“1 未完成原因/存在问题：... 下月目标：... 行动方案：...”。"
        if len(missing) == 1:
            metric_no = next(iter(missing))
            hint += f"\n当前只剩第{metric_no}项有缺失，也可以直接说“第{metric_no}项未完成原因/存在问题：...”。"
        return ParsedPerformanceResult(
            status=status,
            message=hint,
            touched_metrics=[],
            missing=missing,
            responses=_ordered_responses(clean_metrics, merged),
            confirmed_by_user=False,
        )

    touched: list[int] = []
    for block in blocks:
        metric_no = block["metric_no"]
        response = dict(merged.get(metric_no) or {})
        if block.get("reason"):
            response["reason"] = block["reason"]
        if block.get("next_target"):
            response["next_target"] = block["next_target"]
        if block.get("actions"):
            response["actions"] = block["actions"][:4]
        merged[metric_no] = response
        touched.append(metric_no)

    ordered = _ordered_responses(clean_metrics, merged)
    missing = missing_by_metric(clean_metrics, ordered)
    next_status = PERFORMANCE_PENDING_CONFIRMATION if not missing else PERFORMANCE_COLLECTING
    message = _build_update_message(clean_metrics, touched, missing, ordered)
    return ParsedPerformanceResult(
        status=next_status,
        message=message,
        touched_metrics=sorted(set(touched)),
        missing=missing,
        responses=ordered,
        confirmed_by_user=False,
    )


def is_performance_reply_candidate(
    *,
    metrics: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    raw_input: str,
    status: str = PERFORMANCE_COLLECTING,
) -> bool:
    clean_metrics = normalize_metrics(metrics)
    if not clean_metrics:
        return False
    text = str(raw_input or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _metric_header_name_mismatch(text, clean_metrics):
        return False
    if any(
        marker in compact
        for marker in (
            "请回复本次需要填写的指标",
            "指标完成情况概览",
            "完整绩效汇报预览",
            "绩效目标及行动方案",
            "未完成原因存在问题",
        )
    ):
        return True
    merged = _responses_by_no(clean_metrics, responses)
    missing = missing_by_metric(clean_metrics, list(merged.values()))
    if _is_confirm_reply(text):
        return status == PERFORMANCE_PENDING_CONFIRMATION and not missing
    if _parse_metric_clear_instruction(text, clean_metrics):
        return True
    replacement = _parse_replacement_instruction(text)
    if replacement is not None:
        old_value, _new_value = replacement
        if _replacement_touched_metric_numbers(merged, old_value):
            return True
    return bool(parse_performance_reply_blocks(text, clean_metrics, merged))


def looks_like_performance_reply_template(raw_input: str) -> bool:
    text = str(raw_input or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if any(
        marker in compact
        for marker in (
            "请回复本次需要填写的指标",
            "指标完成情况概览",
            "完整绩效汇报预览",
            "绩效目标及行动方案",
        )
    ):
        return True
    field_hits = sum(
        1
        for marker in ("未完成原因存在问题", "未完成原因", "存在问题", "下月目标", "行动方案", "措施")
        if marker in compact
    )
    if field_hits < 2:
        return False
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    header_pattern = re.compile(r"^\s*(?:第)?[一二两三四五六七八九十\d]{1,3}(?:个|项|条)?[、.．）):：\s-]+")
    has_metric_header = any(header_pattern.match(_strip_markdown_metric_prefix(line)) for line in lines)
    has_bracketed_metric = any("【" in line and "】" in line for line in lines)
    return has_metric_header or has_bracketed_metric


def parse_performance_reply_blocks(
    raw_input: str,
    metrics: list[dict[str, Any]],
    current_responses: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    segments = _segment_metric_blocks(raw_input, metrics, current_responses or {})
    result: list[dict[str, Any]] = []
    for metric_no, text in segments:
        parsed = _parse_metric_fields(text)
        if not any(parsed.values()):
            continue
        parsed["metric_no"] = metric_no
        result.append(parsed)
    return result


def missing_by_metric(metrics: list[dict[str, Any]], responses: list[dict[str, Any]]) -> dict[int, list[str]]:
    response_map = {int(item.get("metric_no") or 0): item for item in responses if isinstance(item, dict)}
    result: dict[int, list[str]] = {}
    for metric in metrics:
        metric_no = int(metric["metric_no"])
        response = response_map.get(metric_no) or {}
        missing: list[str] = []
        if not str(response.get("reason") or "").strip():
            missing.append("未完成原因/存在问题")
        if not str(response.get("next_target") or "").strip():
            missing.append("下月目标")
        actions = [str(item).strip() for item in (response.get("actions") or []) if str(item).strip()]
        if not actions:
            missing.append("行动方案")
        if missing:
            result[metric_no] = missing
    return result


def _segment_metric_blocks(
    raw_input: str,
    metrics: list[dict[str, Any]],
    current_responses: dict[int, dict[str, Any]],
) -> list[tuple[int, str]]:
    prepared_text = _prepare_metric_reply_text(raw_input, metrics)
    lines = [line.strip() for line in prepared_text.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    metric_numbers = {int(metric["metric_no"]) for metric in metrics}
    metric_names = {int(metric["metric_no"]): _compact(str(metric["name"])) for metric in metrics}
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
        fallback_no = _single_metric_fallback(metrics, current_responses)
        if fallback_no is not None:
            return [(fallback_no, str(raw_input or ""))]
    return [(metric_no, "\n".join(parts).strip()) for metric_no, parts in blocks if "\n".join(parts).strip()]


def _prepare_metric_reply_text(raw_input: str, metrics: list[dict[str, Any]]) -> str:
    text = str(raw_input or "").replace("\r\n", "\n").strip()
    if not text:
        return text
    ordinal_header_pattern = re.compile(
        r"(?<!^)(?<!\n)(?=(?:"
        r"第[一二两三四五六七八九十\d]{1,3}(?:个|项|条)?"
        r"|\d{1,3}[、.．）):：\s-]+"
        r"|[一二两三四五六七八九十]{1,3}[、.．）):：\s-]+"
        r")[^\n。；;]{0,30}(?:未完成|存在的问题|存在问题|原因|下月|下一|目标|行动方案|措施|指标))"
    )
    text = ordinal_header_pattern.sub("\n", text)
    aliases = sorted(
        {
            alias
            for metric in normalize_metrics(metrics)
            for alias in _metric_raw_aliases(str(metric.get("name") or ""))
            if len(_compact(alias)) >= 4
        },
        key=len,
        reverse=True,
    )
    for alias in aliases:
        pattern = re.compile(
            rf"(?<!^)(?<!\n)(?={re.escape(alias)}[^\n。；;]{{0,24}}(?:未完成|存在的问题|存在问题|原因|下月|下一|目标|行动方案|措施))"
        )
        text = pattern.sub("\n", text)
    return text


def _metric_raw_aliases(metric_name: str) -> set[str]:
    name = str(metric_name or "").strip()
    aliases = {name}
    aliases.add(re.sub(r"[（(][^）)]*[）)]", "", name).strip())
    aliases.add(name.replace("/", ""))
    aliases.add(name.replace("／", ""))
    aliases.add(name.replace("（现金）", "").replace("(现金)", "").strip())
    return {alias for alias in aliases if alias}


def _metric_header_from_line(
    line: str,
    metric_numbers: set[int],
    metric_names: dict[int, str],
    *,
    in_actions: bool,
) -> tuple[int, str] | None:
    line = _strip_markdown_metric_prefix(line)
    match = re.match(
        r"^\s*(?:把|将)?(?:"
        r"第(?P<prefixed>[一二两三四五六七八九十\d]{1,3})(?:个|项|条)?"
        r"|(?P<cn_suffix>[一二两三四五六七八九十]{1,3})(?:个|项|条)"
        r"|(?P<digit>\d{1,3})[、.．）):：\s-]+"
        r"|(?P<cn>[一二两三四五六七八九十]{1,3})[、.．）):：\s-]+"
        r")(.*)$",
        line,
    )
    if not match:
        return _metric_name_header_from_line(line, metric_names, in_actions=in_actions)
    metric_no = _parse_cn_number(
        match.group("prefixed")
        or match.group("cn_suffix")
        or match.group("digit")
        or match.group("cn")
    )
    if metric_no is None or metric_no not in metric_numbers:
        return None
    rest = match.group(5).strip()
    compact_rest = _compact(rest)
    has_field_label = any(token in compact_rest for token in ("未完成原因", "存在问题", "原因", "下月目标", "目标", "行动方案", "措施"))
    has_metric_name = bool(compact_rest and any(name and (name in compact_rest or compact_rest in name) for name in metric_names.values()))
    if in_actions and not has_field_label and not has_metric_name:
        return None
    if not rest or has_field_label or has_metric_name:
        return metric_no, rest
    return metric_no, rest


def _metric_name_header_from_line(
    line: str,
    metric_names: dict[int, str],
    *,
    in_actions: bool,
) -> tuple[int, str] | None:
    compact_line = _compact(line)
    if not compact_line:
        return None
    aliases: list[tuple[int, str]] = []
    for metric_no, name in metric_names.items():
        values = {name, name.replace("/", ""), name.replace("／", ""), name.replace("现金", "")}
        for alias in values:
            if len(alias) >= 4:
                aliases.append((metric_no, alias))
    aliases.sort(key=lambda item: len(item[1]), reverse=True)
    for metric_no, alias in aliases:
        if not compact_line.startswith(alias):
            continue
        if in_actions and not any(token in compact_line for token in ("未完成原因", "存在的问题", "存在问题", "原因", "下月目标", "下一目标", "目标", "行动方案", "措施")):
            return None
        return metric_no, line
    return None


def _metric_header_name_mismatch(raw_input: str, metrics: list[dict[str, Any]]) -> bool:
    metric_names = {int(metric["metric_no"]): _compact(str(metric["name"])) for metric in metrics}
    if not metric_names:
        return False
    in_actions = False
    for line in str(raw_input or "").replace("\r\n", "\n").split("\n"):
        value = _strip_markdown_metric_prefix(line)
        match = re.match(r"^\s*(?:把|将)?(?:第)?([一二两三四五六七八九十\d]{1,3})(?:个|项|条)?[、.．）):：\s-]*(.*)$", value)
        if not match:
            if _has_action_label(value):
                in_actions = True
            continue
        metric_no = _parse_cn_number(match.group(1))
        rest = match.group(2).strip()
        compact_rest = _compact(rest)
        if not compact_rest or _has_metric_field_label(compact_rest):
            if _has_action_label(rest):
                in_actions = True
            continue
        expected_name = metric_names.get(metric_no)
        expected_match = bool(expected_name and (expected_name in compact_rest or compact_rest in expected_name))
        if in_actions and not expected_match and not _has_bracketed_metric_title(rest):
            continue
        if metric_no not in metric_names:
            return _looks_like_metric_title(rest)
        if expected_match:
            in_actions = _has_action_label(rest)
            continue
        if _looks_like_metric_title(rest):
            return True
    return False


def _has_metric_field_label(compact_value: str) -> bool:
    return any(token in compact_value for token in ("未完成原因", "存在的问题", "存在问题", "原因", "下月目标", "下一目标", "目标", "行动方案", "措施"))


def _looks_like_metric_title(value: str) -> bool:
    text = str(value or "").strip()
    compact = _compact(text)
    if not compact or _has_metric_field_label(compact):
        return False
    if _has_bracketed_metric_title(text):
        return True
    return len(compact) >= 4 and not re.search(r"[:：；;，,。]", text)


def _has_bracketed_metric_title(value: str) -> bool:
    return bool(re.match(r"^[【\[][^】\]]+[】\]]$", str(value or "").strip()))


def _strip_markdown_metric_prefix(line: str) -> str:
    value = str(line or "").strip()
    value = re.sub(r"^(?:#{1,6}|>|[-*+])\s+", "", value).strip()
    return value


def _parse_metric_fields(text: str) -> dict[str, Any]:
    normalized = _trim_metric_reply_context(str(text or "").strip())
    next_target = _extract_field(normalized, ("下月绩效目标", "下月目标", "下一目标", "下一个月目标"), ("行动方案", "措施"))
    if not next_target:
        next_target = _extract_field(normalized, ("目标",), ("行动方案", "措施"), require_separator=True)
    next_target = _strip_target_unit_prefix(next_target)
    return {
        "reason": _extract_field(
            normalized,
            (
                "未完成原因/存在问题",
                "未完成原因分析",
                "未完成原因及存在问题",
                "未完成的原因及存在的问题",
                "未完成的原因",
                "未完成原因",
                "存在的问题",
                "存在问题",
                "原因",
            ),
            ("下月绩效目标及行动方案", "下月目标", "下一目标", "目标", "行动方案", "措施"),
        ),
        "next_target": next_target,
        "actions": _extract_actions(normalized),
    }


def _trim_metric_reply_context(text: str) -> str:
    value = str(text or "").strip()
    marker = re.search(r"(?:（|\()2(?:）|\))\s*未完成原因/存在问题\s*[:：；;]?", value)
    if marker:
        value = value[marker.start() :]
    lines: list[str] = []
    for line in value.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        compact = _compact(stripped)
        if compact in {"完整绩效汇报预览", "1本月绩效完成情况", "下月绩效目标及行动方案"}:
            continue
        if compact in {"（1）本月绩效完成情况", "(1)本月绩效完成情况"}:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _extract_field(
    text: str,
    labels: tuple[str, ...],
    stop_labels: tuple[str, ...],
    *,
    require_separator: bool = False,
) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    edit_pattern = "改成|改为|替换成|换成|调整为|变更为"
    label_separator = rf"\s*(?:[:：；;]|是|为|{edit_pattern})\s*" if require_separator else rf"\s*(?:[:：；;]|是|为|{edit_pattern})?\s*"
    stop_separator = rf"\s*(?:[:：；;]|是|为|{edit_pattern})?\s*"
    unit_suffix = r"(?:\s*[（(][^）)]*[）)])?"
    match = re.search(rf"(?:{label_pattern}){unit_suffix}{label_separator}(.*?)(?=(?:{stop_pattern}){unit_suffix}{stop_separator}|$)", text, flags=re.S)
    if not match:
        return ""
    value = match.group(1).strip()
    value = re.sub(r"^[：:，,。；;\s]+", "", value)
    value = re.sub(r"\s+", " ", value)
    value = _strip_edit_prefix(value)
    return value.strip(" _-—；;。，,、")


def _strip_target_unit_prefix(value: str) -> str:
    return re.sub(r"^（[^）]+）\s*[:：；;]?\s*", "", str(value or "")).strip(" _-—；;。，,、")


def _extract_actions(text: str) -> list[str]:
    match = re.search(r"(?:行动方案|措施)\s*[:：]?\s*(.+)$", text, flags=re.S)
    if not match:
        return []
    value = _strip_edit_prefix(match.group(1).strip())
    value = re.split(r"\n\s*(?:第)?[一二两三四五六七八九十\d]{1,3}(?:个|项|条)\s+", value, maxsplit=1)[0].strip()
    parts = [part.strip() for part in re.split(r"(?:^|\n|\s)(?:\d+|[一二两三四])[\.\、．)]\s*", value) if part.strip()]
    if len(parts) == 1 and parts[0] == value:
        parts = [part.strip() for part in re.split(r"[；;\n]+", value) if part.strip()]
    cleaned: list[str] = []
    for part in parts:
        item = re.sub(r"\s+", " ", part).strip(" _-—；;。")
        item = _strip_edit_prefix(item)
        item = re.sub(r"^(?:\d+|[一二两三四])[\.\、．)]\s*", "", item).strip()
        item = _strip_edit_prefix(item)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned[:4]


def _responses_by_no(metrics: list[dict[str, Any]], responses: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    initial = {int(item["metric_no"]): item for item in initial_responses(metrics)}
    for response in responses:
        if not isinstance(response, dict):
            continue
        metric_no = _to_int(response.get("metric_no"))
        if metric_no is None or metric_no not in initial:
            continue
        current = dict(initial[metric_no])
        current.update(
            {
                "reason": str(response.get("reason") or "").strip(),
                "next_target": str(response.get("next_target") or "").strip(),
                "actions": [str(item).strip() for item in (response.get("actions") or []) if str(item).strip()][:4],
            }
        )
        initial[metric_no] = current
    return initial


def _ordered_responses(metrics: list[dict[str, Any]], responses_by_no: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [responses_by_no[int(metric["metric_no"])] for metric in metrics if int(metric["metric_no"]) in responses_by_no]


def _build_update_message(
    metrics: list[dict[str, Any]],
    touched: list[int],
    missing: dict[int, list[str]],
    responses: list[dict[str, Any]],
) -> str:
    touched_text = "、".join(f"第{metric_no}项" for metric_no in sorted(set(touched)))
    if missing:
        return _build_progress_message(metrics, missing, prefix=f"已记录{touched_text}。")
    preview = build_complete_performance_report(metrics, responses)
    return f"已记录{touched_text}，所有指标都已补齐。\n\n{preview}\n\n确认无误请回复“确认提交”；需要修改可以说“把XX改成XX”，也可以按指标编号整段重发。"


def _looks_like_complete_performance_preview(raw_input: str) -> bool:
    return "完整绩效汇报预览" in _compact(raw_input)


def _build_preview_replay_message(
    metrics: list[dict[str, Any]], missing: dict[int, list[str]], responses: list[dict[str, Any]]
) -> str:
    if missing:
        return _build_progress_message(metrics, missing, prefix="已收到预览内容，未重复写入。")
    preview = build_complete_performance_report(metrics, responses)
    return f"已收到当前预览，未重复写入。\n\n{preview}\n\n确认无误请回复“确认提交”；需要修改可以说“把XX改成XX”，也可以按指标编号整段重发。"


def _build_missing_message(metrics: list[dict[str, Any]], missing: dict[int, list[str]], *, prefix: str) -> str:
    metric_names = {int(metric["metric_no"]): str(metric["name"]) for metric in metrics}
    lines = [prefix]
    for metric_no in sorted(missing):
        lines.append(f"{metric_no}. {metric_names.get(metric_no, '')}：{'、'.join(missing[metric_no])}")
    return "\n".join(lines)


def _build_progress_message(metrics: list[dict[str, Any]], missing: dict[int, list[str]], *, prefix: str = "") -> str:
    completed_text = "".join(f"【{name}】" for name in _completed_metric_names(metrics, missing))
    remaining_text = "".join(f"【{name}】" for name in _remaining_metric_names(metrics, missing))
    if completed_text:
        message = f"当前已完成了{completed_text}的填写，请继续填写{remaining_text}。"
    else:
        message = f"当前还没有完整填完的指标，请继续填写{remaining_text}。"
    return f"{prefix}{message}" if prefix else message


def _build_replacement_message(
    metrics: list[dict[str, Any]],
    touched: list[int],
    missing: dict[int, list[str]],
    responses: list[dict[str, Any]],
    old_value: str,
    new_value: str,
) -> str:
    touched_text = "、".join(f"第{metric_no}项" for metric_no in sorted(set(touched)))
    prefix = f"已修改{touched_text}，将“{old_value}”改为“{new_value}”。"
    if missing:
        return _build_progress_message(metrics, missing, prefix=prefix)
    preview = build_complete_performance_report(metrics, responses)
    return f"{prefix}\n\n{preview}\n\n确认无误请回复“确认提交”；需要修改可以继续说。"


def _completed_metric_names(metrics: list[dict[str, Any]], missing: dict[int, list[str]]) -> list[str]:
    return [str(metric["name"]) for metric in metrics if int(metric["metric_no"]) not in missing]


def _remaining_metric_names(metrics: list[dict[str, Any]], missing: dict[int, list[str]]) -> list[str]:
    return [str(metric["name"]) for metric in metrics if int(metric["metric_no"]) in missing]


def _parse_replacement_instruction(raw_input: str) -> tuple[str, str] | None:
    text = str(raw_input or "").strip()
    if not text:
        return None
    patterns = (
        r"^把\s*(?P<old>.+?)\s*(?:改成|改为|替换成|换成|调整为|变更为)\s*(?P<new>.+)$",
        r"^(?P<old>.+?)\s*(?:改成|改为|替换成|换成|调整为|变更为)\s*(?P<new>.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.S)
        if not match:
            continue
        old_value = _clean_replacement_value(match.group("old"))
        new_value = _clean_replacement_value(match.group("new"))
        if old_value and new_value and old_value != new_value:
            return old_value, new_value
    return None


def _parse_metric_clear_instruction(raw_input: str, metrics: list[dict[str, Any]]) -> list[int]:
    clean_metrics = normalize_metrics(metrics)
    if not clean_metrics:
        return []
    segments = _segment_metric_blocks(raw_input, clean_metrics, {})
    cleared: list[int] = []
    for metric_no, text in segments:
        compact = _compact(text)
        if any(token in compact for token in ("清空", "清掉", "清除", "重新清空", "重新填写", "重填")):
            cleared.append(metric_no)
    return sorted(set(cleared))


def _clean_replacement_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n\"'“”‘’：:，,。；;")


def _strip_edit_prefix(value: str) -> str:
    text = re.sub(r"^[\s：:，,。；;、]+", "", str(value or ""))
    return re.sub(r"^\s*(?:改成|改为|替换成|换成|调整为|变更为)\s*[:：；;，,、]?\s*", "", text).strip()


def _replacement_touched_metric_numbers(responses_by_no: dict[int, dict[str, Any]], old_value: str) -> list[int]:
    touched: list[int] = []
    for metric_no, response in responses_by_no.items():
        if old_value in str(response.get("reason") or "") or old_value in str(response.get("next_target") or ""):
            touched.append(metric_no)
            continue
        if any(old_value in str(action or "") for action in (response.get("actions") or [])):
            touched.append(metric_no)
    return touched


def _apply_response_replacement(responses_by_no: dict[int, dict[str, Any]], old_value: str, new_value: str) -> list[int]:
    touched: list[int] = []
    for metric_no, response in responses_by_no.items():
        changed = False
        for field in ("reason", "next_target"):
            current = str(response.get(field) or "")
            if old_value in current:
                response[field] = current.replace(old_value, new_value)
                changed = True
        actions: list[str] = []
        for action in response.get("actions") or []:
            current_action = str(action or "")
            replaced_action = current_action.replace(old_value, new_value)
            if replaced_action != current_action:
                changed = True
            actions.append(replaced_action)
        if changed:
            response["actions"] = [action for action in actions if action.strip()][:4]
            touched.append(metric_no)
    return touched


def _build_preview(metric_names: dict[int, str], responses: list[dict[str, Any]]) -> str:
    lines = ["当前绩效填报草稿："]
    for response in responses:
        metric_no = int(response.get("metric_no") or 0)
        lines.append(f"{metric_no}. {metric_names.get(metric_no, response.get('metric_name', ''))}")
        lines.append(f"未完成原因/存在问题：{response.get('reason') or '未填写'}")
        lines.append(f"下月目标：{response.get('next_target') or '未填写'}")
        actions = [str(item).strip() for item in (response.get("actions") or []) if str(item).strip()]
        if actions:
            lines.append("行动方案：" + "；".join(f"{index}. {item}" for index, item in enumerate(actions, start=1)))
        else:
            lines.append("行动方案：未填写")
    return "\n".join(lines)


def _single_metric_fallback(metrics: list[dict[str, Any]], current_responses: dict[int, dict[str, Any]]) -> int | None:
    missing = missing_by_metric(metrics, list(current_responses.values()))
    if len(metrics) == 1:
        return int(metrics[0]["metric_no"])
    if len(missing) == 1:
        return next(iter(missing))
    return None


def _has_action_label(value: str) -> bool:
    return any(token in _compact(value) for token in ("行动方案", "措施"))


def _is_confirm_reply(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if "完整绩效汇报预览" in compact and compact.endswith("提交"):
        return True
    return compact in {"确认", "确认提交", "提交", "可以提交", "没问题提交", "没问题", "就这样", "对", "对的"}


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000，,。.;；:：!！?？()（）【】\[\]\"'“”‘’]+", "", str(value or "")).lower()


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return _parse_cn_number(text)


def _parse_cn_number(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if text in numerals:
        return numerals[text]
    if text.startswith("十") and len(text) == 2:
        return 10 + numerals.get(text[1], 0)
    if text.endswith("十") and len(text) == 2:
        return numerals.get(text[0], 0) * 10
    if "十" in text:
        left, right = text.split("十", 1)
        return numerals.get(left, 1 if not left else 0) * 10 + numerals.get(right, 0)
    return None
