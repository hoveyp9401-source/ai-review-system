from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any

from app.workflows.relative_dates import date_hint_from_text


ACTION_CONTEXT_VERSION = "action_context.v1"

WORK_OBJECT_DAILY_REPORT = "daily_report"
WORK_OBJECT_MONTHLY_REPORT = "monthly_report"
WORK_OBJECT_WEEKLY_REPORT = "weekly_report"
WORK_OBJECT_CASE_DATA = "case_data"
WORK_OBJECT_CASE_PROGRESS = "case_progress"
WORK_OBJECT_TRAVEL_PLAN = "travel_plan"
WORK_OBJECT_LEGAL_RESEARCH = "legal_research"
WORK_OBJECT_INTERNAL_QA = "internal_qa"
WORK_OBJECT_CHAT = "chat"
WORK_OBJECT_UNKNOWN = "unknown"

OP_BEGIN_EDIT = "begin_edit"
OP_EDIT = "edit"
OP_WRITE = "write"
OP_VIEW = "view"
OP_STATUS_QUERY = "status_query"
OP_ASK = "ask"
OP_CONFIRM = "confirm"
OP_COPY = "copy"
OP_CLEAR = "clear"
OP_REVOKE = "revoke"
OP_CHAT = "chat"
OP_UNKNOWN = "unknown"

WRITE_INTENT_WRITE = "write"
WRITE_INTENT_READ_ONLY = "read_only"
WRITE_INTENT_NO_WRITE = "no_write"
WRITE_INTENT_PENDING = "pending"
WRITE_INTENT_SANDBOX = "sandbox"


@dataclass(frozen=True)
class ActionContext:
    """One visible interpretation of a user turn before workflow execution.

    This contract is intentionally lightweight. It does not route or mutate
    state; it records what object, time, and operation the first cognitive pass
    believes the user is addressing.
    """

    work_object: str = WORK_OBJECT_UNKNOWN
    operation: str = OP_UNKNOWN
    temporal_hint: str = "unknown"
    report_date_policy: str = "none"
    write_intent: str = WRITE_INTENT_PENDING
    confidence: float = 0.0
    reason: str = ""
    secondary_work_objects: tuple[str, ...] = ()
    active_workflows: tuple[str, ...] = ()
    contract_version: str = ACTION_CONTEXT_VERSION
    source: str = "rule_context_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "work_object": self.work_object,
            "secondary_work_objects": list(self.secondary_work_objects),
            "operation": self.operation,
            "temporal_hint": self.temporal_hint,
            "report_date_policy": self.report_date_policy,
            "write_intent": self.write_intent,
            "confidence": self.confidence,
            "reason": self.reason,
            "active_workflows": list(self.active_workflows),
            "source": self.source,
        }


def resolve_action_context(
    raw_text: str,
    *,
    received_at: datetime | None = None,
    active_tasks: tuple[Any, ...] | list[Any] = (),
) -> ActionContext:
    """Resolve object/time/action context for a turn without side effects."""

    text = str(raw_text or "").strip()
    compact = _compact(text)
    active_workflows = _active_workflows(active_tasks)
    if not compact:
        return ActionContext(
            work_object=WORK_OBJECT_CHAT,
            operation=OP_CHAT,
            write_intent=WRITE_INTENT_NO_WRITE,
            confidence=0.75,
            active_workflows=active_workflows,
            reason="empty or whitespace-only user turn",
        )

    work_object, secondary, object_reason = _resolve_work_object(compact, active_workflows=active_workflows)
    operation, operation_reason = _resolve_operation(compact, work_object=work_object)
    temporal_hint = _resolve_temporal_hint(compact, received_at=received_at)
    report_date_policy = _resolve_report_date_policy(
        compact,
        work_object=work_object,
        operation=operation,
        temporal_hint=temporal_hint,
        received_at=received_at,
        active_workflows=active_workflows,
    )
    write_intent = _resolve_write_intent(work_object=work_object, operation=operation)
    confidence = _confidence(work_object=work_object, operation=operation, temporal_hint=temporal_hint)

    return ActionContext(
        work_object=work_object,
        secondary_work_objects=tuple(secondary),
        operation=operation,
        temporal_hint=temporal_hint,
        report_date_policy=report_date_policy,
        write_intent=write_intent,
        confidence=confidence,
        reason=_join_reason(object_reason, operation_reason),
        active_workflows=active_workflows,
    )


def _resolve_work_object(text: str, *, active_workflows: tuple[str, ...]) -> tuple[str, list[str], str]:
    secondary: list[str] = []
    if _looks_like_chat(text):
        return WORK_OBJECT_CHAT, secondary, "message is interaction/meta chatter rather than a business object"
    if _looks_like_monthly_report(text):
        return WORK_OBJECT_MONTHLY_REPORT, secondary, "monthly report or performance metric object is explicit"
    if _looks_like_weekly_report(text):
        return WORK_OBJECT_WEEKLY_REPORT, secondary, "weekly report object is explicit"
    if _looks_like_daily_report(text):
        if _looks_like_travel_plan(text):
            secondary.append(WORK_OBJECT_TRAVEL_PLAN)
        if _looks_like_case_progress(text):
            secondary.append(WORK_OBJECT_CASE_PROGRESS)
        return WORK_OBJECT_DAILY_REPORT, secondary, "daily report object is explicit or implied by daily work language"
    if _looks_like_case_data(text):
        return WORK_OBJECT_CASE_DATA, secondary, "case data/statistics object is explicit"
    if _looks_like_case_progress(text):
        if _looks_like_travel_plan(text):
            secondary.append(WORK_OBJECT_TRAVEL_PLAN)
        return WORK_OBJECT_CASE_PROGRESS, secondary, "case progress object is explicit"
    if _looks_like_travel_plan(text):
        return WORK_OBJECT_TRAVEL_PLAN, secondary, "travel coordination object is explicit"
    if _looks_like_legal_research(text):
        return WORK_OBJECT_LEGAL_RESEARCH, secondary, "legal research object is explicit"
    if _looks_like_internal_qa(text):
        return WORK_OBJECT_INTERNAL_QA, secondary, "question asks for internal knowledge rather than a report write"
    if _can_inherit_active_object(text, active_workflows):
        return active_workflows[-1], secondary, "short follow-up inherits the active workflow object"
    return WORK_OBJECT_UNKNOWN, secondary, "no stable work object detected"


def _resolve_operation(text: str, *, work_object: str) -> tuple[str, str]:
    if work_object == WORK_OBJECT_CHAT:
        return OP_CHAT, "chat turn has no business operation"
    if _contains_any(text, _CONFIRM_MARKERS):
        return OP_CONFIRM, "confirmation marker is explicit"
    if _contains_any(text, _CLEAR_MARKERS):
        return OP_CLEAR, "clear operation marker is explicit"
    if _looks_like_revoke(text, work_object=work_object):
        return OP_REVOKE, "revoke operation marker targets the report object"
    if _contains_any(text, _COPY_MARKERS):
        return OP_COPY, "copy operation marker is explicit"
    if work_object == WORK_OBJECT_DAILY_REPORT and _looks_like_daily_begin_edit(text):
        return OP_BEGIN_EDIT, "user opens an edit context without concrete edit content"
    if _contains_any(text, _EDIT_MARKERS):
        return OP_EDIT, "edit operation marker is explicit"
    if work_object == WORK_OBJECT_MONTHLY_REPORT and _looks_like_status_query(text):
        return OP_STATUS_QUERY, "monthly status query marker is explicit"
    if _looks_like_view(text, work_object=work_object):
        return OP_VIEW, "view/query-current marker is explicit"
    if _looks_like_question(text) or work_object in {WORK_OBJECT_CASE_DATA, WORK_OBJECT_INTERNAL_QA, WORK_OBJECT_LEGAL_RESEARCH}:
        return OP_ASK, "question or data lookup operation is explicit"
    if work_object in {
        WORK_OBJECT_DAILY_REPORT,
        WORK_OBJECT_MONTHLY_REPORT,
        WORK_OBJECT_TRAVEL_PLAN,
        WORK_OBJECT_CASE_PROGRESS,
    }:
        return OP_WRITE, "business object with work-like content implies a write/candidate operation"
    return OP_UNKNOWN, "operation is not stable enough to execute"


def _resolve_temporal_hint(text: str, *, received_at: datetime | None) -> str:
    if _contains_any(text, _DAY_BEFORE_YESTERDAY_MARKERS):
        return "day_before_yesterday"
    if _contains_any(text, _YESTERDAY_MARKERS):
        return "yesterday"
    hint = date_hint_from_text(text, received_at=received_at)
    if hint in {"today", "tomorrow", "future_weekday", "past_weekday", "next_week"}:
        return hint
    if _contains_any(text, _LAST_WEEK_MARKERS):
        return "past_weekday"
    return "unknown"


def _resolve_report_date_policy(
    text: str,
    *,
    work_object: str,
    operation: str,
    temporal_hint: str,
    received_at: datetime | None,
    active_workflows: tuple[str, ...],
) -> str:
    if work_object != WORK_OBJECT_DAILY_REPORT:
        return "none"
    if temporal_hint in {"yesterday", "day_before_yesterday", "past_weekday"}:
        return "explicit_history"
    if temporal_hint == "today" and _contains_any(text, _DAILY_OBJECT_MARKERS):
        return "explicit_today"
    if operation in {OP_BEGIN_EDIT, OP_EDIT, OP_WRITE, OP_COPY, OP_CONFIRM, OP_CLEAR, OP_REVOKE} and _is_before_nine(received_at):
        return "before_nine_defaults_yesterday"
    if WORK_OBJECT_DAILY_REPORT in active_workflows:
        return "active_context"
    return "current_date"


def _resolve_write_intent(*, work_object: str, operation: str) -> str:
    if work_object == WORK_OBJECT_CHAT:
        return WRITE_INTENT_NO_WRITE
    if operation in {OP_VIEW, OP_STATUS_QUERY, OP_ASK, OP_BEGIN_EDIT, OP_CHAT}:
        return WRITE_INTENT_READ_ONLY if operation != OP_CHAT else WRITE_INTENT_NO_WRITE
    if operation in {OP_WRITE, OP_EDIT, OP_COPY, OP_CONFIRM, OP_CLEAR, OP_REVOKE}:
        if work_object in {WORK_OBJECT_TRAVEL_PLAN, WORK_OBJECT_CASE_PROGRESS}:
            return WRITE_INTENT_SANDBOX
        return WRITE_INTENT_WRITE
    return WRITE_INTENT_PENDING


def _confidence(*, work_object: str, operation: str, temporal_hint: str) -> float:
    score = 0.55
    if work_object != WORK_OBJECT_UNKNOWN:
        score += 0.2
    if operation != OP_UNKNOWN:
        score += 0.15
    if temporal_hint != "unknown":
        score += 0.05
    if work_object == WORK_OBJECT_CHAT and operation == OP_CHAT:
        score += 0.05
    return min(round(score, 2), 0.95)


def _looks_like_daily_report(text: str) -> bool:
    if _contains_any(text, _DAILY_OBJECT_MARKERS):
        return True
    if _contains_any(text, _DAILY_HISTORY_REFERENCES) and _contains_any(text, _DAILY_REPORT_WORDS):
        return True
    if _contains_any(text, _TODAY_MARKERS) and _contains_any(text, _DAILY_WORK_VERBS):
        return True
    if _contains_any(text, _TODAY_MARKERS) and _contains_any(text, _PLAN_OR_WORK_VERBS) and _contains_any(text, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(text, _TOMORROW_MARKERS) and _contains_any(text, _PLAN_OR_WORK_VERBS):
        return True
    if _contains_any(text, _COMPLETED_PREVIOUS_PLAN_MARKERS):
        return True
    return False


def _looks_like_daily_begin_edit(text: str) -> bool:
    if not _contains_any(text, _EDIT_MARKERS):
        return False
    if not _contains_any(text, _DAILY_REPORT_WORDS):
        return False
    if _contains_any(text, _CONCRETE_EDIT_MARKERS):
        return False
    return True


def _looks_like_monthly_report(text: str) -> bool:
    if _contains_any(text, ("\u6708\u62a5", "\u7ee9\u6548")):
        return True
    if _contains_any(text, ("\u672c\u6b21\u9700\u8981\u586b\u5199\u7684\u6307\u6807", "\u6307\u6807\u5b8c\u6210\u60c5\u51b5\u6982\u89c8")):
        return True
    if _contains_any(text, ("\u6307\u6807", "\u672a\u5b8c\u6210\u539f\u56e0", "\u4e0b\u6708\u76ee\u6807", "\u884c\u52a8\u65b9\u6848")) and _contains_any(
        text,
        ("\u6708\u5ea6", "\u672c\u6708", "\u4e0b\u6708", "\u586b\u62a5", "\u63d0\u4ea4", "\u586b\u5199"),
    ):
        return True
    return False


def _looks_like_weekly_report(text: str) -> bool:
    return "\u5468\u62a5" in text


def _looks_like_case_data(text: str) -> bool:
    if not _contains_any(text, _CASE_DATA_MARKERS):
        return False
    return _looks_like_question(text) or _contains_any(text, _DATA_LOOKUP_MARKERS)


def _looks_like_case_progress(text: str) -> bool:
    if "\u6848\u4ef6" not in text and "\u6848" not in text:
        return False
    if _looks_like_case_data(text):
        return False
    return _contains_any(text, _CASE_PROGRESS_MARKERS)


def _looks_like_travel_plan(text: str) -> bool:
    if "\u51fa\u5dee" in text:
        return True
    if "\u53bb" in text and "\u5f00\u5ead" in text and not _contains_any(text, ("\u6848", "\u6848\u4ef6")):
        return True
    return False


def _looks_like_legal_research(text: str) -> bool:
    return _contains_any(text, ("\u6cd5\u5f8b\u7814\u7a76", "\u6cd5\u6761", "\u5224\u4f8b", "\u6848\u4f8b\u68c0\u7d22")) or (
        "\u7814\u7a76" in text and _contains_any(text, ("\u6cd5\u5f8b", "\u6cd5\u89c4", "\u89c4\u5b9a"))
    )


def _looks_like_internal_qa(text: str) -> bool:
    if _looks_like_case_data(text):
        return False
    return _looks_like_question(text) and _contains_any(
        text,
        ("\u6d41\u7a0b", "\u5236\u5ea6", "\u6a21\u677f", "\u8d44\u6599\u5e93", "\u89c4\u5b9a", "\u600e\u4e48\u529e", "\u662f\u4ec0\u4e48"),
    )


def _looks_like_chat(text: str) -> bool:
    compact = _compact(text)
    if text in _CHAT_EXACT_MARKERS:
        return True
    if compact in _CHAT_EXACT_MARKERS:
        return True
    if _looks_like_daily_meta_status_statement(text):
        return True
    if _contains_any(text, ("\u8ba9\u6211\u6d4b\u8bd5\u4e0b", "\u6211\u6d4b\u4e00\u4e0b", "\u968f\u4fbf\u6d4b\u6d4b")):
        return True
    if _contains_any(text, ("\u65e9\u4e0a\u597d", "\u665a\u4e0a\u597d", "\u4f60\u597d")) and not _contains_any(text, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(text, ("\u5403\u5c4e", "\u62c9\u5c4e", "\u4e00\u5768\u5c4e", "\u50bb\u903c")) and not _contains_any(
        text,
        _BUSINESS_OBJECT_MARKERS,
    ):
        return True
    if _contains_any(text, ("\u5403\u5c0f\u756a\u8304", "\u5403\u756a\u8304", "\u4eca\u5929\u5403\u4e86", "\u7a7f\u5565", "\u7a7f\u4ec0\u4e48")) and not _contains_any(
        text,
        _BUSINESS_OBJECT_MARKERS,
    ):
        return True
    return False


def _looks_like_daily_meta_status_statement(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    if any(marker in compact for marker in ("今日工作", "今天工作", "问题风险", "明日计划", "明天计划")):
        return False
    return compact in {
        "写日报了",
        "我写日报了",
        "已经写日报了",
        "我已经写日报了",
        "日报写了",
        "日报写过了",
        "我日报写了",
        "在写日报",
        "我在写日报",
        "正在写日报",
        "我正在写日报",
        "填日报了",
        "我填日报了",
        "已经填日报了",
        "我已经填日报了",
    }


def _looks_like_status_query(text: str) -> bool:
    return _contains_any(
        text,
        (
            "\u63d0\u4ea4\u60c5\u51b5",
            "\u586b\u62a5\u60c5\u51b5",
            "\u586b\u7684\u600e\u6837",
            "\u586b\u5f97\u600e\u6837",
            "\u8c01\u6ca1\u4ea4",
            "\u6ca1\u4ea4",
            "\u6709\u54ea\u4e9b\u6ca1\u586b",
        ),
    )


def _looks_like_view(text: str, *, work_object: str) -> bool:
    if work_object == WORK_OBJECT_DAILY_REPORT and _contains_any(text, ("\u770b", "\u67e5", "\u5c55\u793a", "\u53d1\u6211", "\u7ed9\u6211\u770b")):
        return True
    return False


def _looks_like_question(text: str) -> bool:
    return "\uff1f" in text or "?" in text or _contains_any(text, _QUESTION_MARKERS)


def _looks_like_revoke(text: str, *, work_object: str) -> bool:
    if "\u64a4\u56de" not in text:
        return False
    return work_object == WORK_OBJECT_DAILY_REPORT or _contains_any(text, _DAILY_REPORT_WORDS)


def _can_inherit_active_object(text: str, active_workflows: tuple[str, ...]) -> bool:
    if not active_workflows:
        return False
    if len(text) > 10:
        return False
    return text.endswith("\u5462") or _contains_any(text, ("\u90a3", "\u8fd9\u4e2a", "\u4e0a\u9762", "\u540c\u4e0a"))


def _is_before_nine(received_at: datetime | None) -> bool:
    if received_at is None:
        return False
    return received_at.hour < 9


def _active_workflows(active_tasks: tuple[Any, ...] | list[Any]) -> tuple[str, ...]:
    workflows: list[str] = []
    for task in active_tasks or ():
        workflow = str(getattr(task, "workflow", "") or "")
        if workflow and workflow not in workflows:
            workflows.append(workflow)
    return tuple(workflows)


def _join_reason(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


_TODAY_MARKERS = ("\u4eca\u5929", "\u4eca\u65e5", "\u672c\u65e5")
_YESTERDAY_MARKERS = ("\u6628\u5929", "\u6628\u65e5")
_DAY_BEFORE_YESTERDAY_MARKERS = ("\u524d\u5929", "\u524d\u65e5")
_TOMORROW_MARKERS = ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u4e2a")
_LAST_WEEK_MARKERS = ("\u4e0a\u5468", "\u4e0a\u661f\u671f", "\u4e0a\u793c\u62dc")

_DAILY_REPORT_WORDS = ("\u65e5\u62a5", "\u65e5\u5fd7", "\u6c47\u62a5")
_DAILY_OBJECT_MARKERS = (
    "\u65e5\u62a5",
    "\u65e5\u5fd7",
    "\u4eca\u65e5\u5de5\u4f5c",
    "\u4eca\u5929\u5de5\u4f5c",
    "\u660e\u65e5\u8ba1\u5212",
    "\u660e\u5929\u8ba1\u5212",
    "\u95ee\u9898/\u98ce\u9669",
    "\u95ee\u9898\u98ce\u9669",
    "\u5f53\u524d\u8349\u7a3f",
)
_DAILY_HISTORY_REFERENCES = _YESTERDAY_MARKERS + _DAY_BEFORE_YESTERDAY_MARKERS + ("\u4e0a\u6b21",)
_DAILY_WORK_VERBS = ("\u5b8c\u6210", "\u5904\u7406", "\u5ba1\u6838", "\u8ddf\u8fdb", "\u6c9f\u901a", "\u6574\u7406", "\u7f16\u8f91", "\u4f18\u5316", "\u95ee", "\u54a8\u8be2", "\u8054\u7cfb", "\u5bf9\u4e86", "\u4fee\u4e86")
_PLAN_OR_WORK_VERBS = _DAILY_WORK_VERBS + ("\u8ba1\u5212", "\u51c6\u5907", "\u62df", "\u53bb", "\u51fa\u5dee")
_COMPLETED_PREVIOUS_PLAN_MARKERS = (
    "\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210",
    "\u6628\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210",
    "\u6628\u5929\u90a3\u4e9b\u90fd\u5b8c\u6210\u4e86",
    "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b",
)

_EDIT_MARKERS = ("\u6539", "\u4fee\u6539", "\u8c03\u6574", "\u7f16\u8f91", "\u5220", "\u5220\u6389", "\u66ff\u6362")
_CONCRETE_EDIT_MARKERS = ("\u7b2c", "\u6761", "\u628a", "\u6539\u6210", "\u6539\u4e3a", "\u5220\u6389", "\u52a0\u4e0a", "\u8865\u5145")
_CLEAR_MARKERS = ("\u6e05\u7a7a", "\u5168\u90e8\u5220", "\u5220\u5149")
_COPY_MARKERS = ("\u590d\u5236", "\u5e26\u8fc7\u6765", "\u6cbf\u7528", "\u8ddf\u6628\u5929\u4e00\u6837", "\u548c\u6628\u5929\u4e00\u6837")
_CONFIRM_MARKERS = ("\u786e\u8ba4\u63d0\u4ea4", "\u786e\u8ba4", "\u6ca1\u95ee\u9898")

_QUESTION_MARKERS = (
    "\u591a\u5c11",
    "\u51e0\u4e2a",
    "\u51e0\u4ef6",
    "\u54ea\u4e9b",
    "\u600e\u6837",
    "\u600e\u4e48",
    "\u4ec0\u4e48",
    "\u4e3a\u4ec0\u4e48",
    "\u67e5\u4e0b",
    "\u67e5\u4e00\u4e0b",
    "\u770b\u4e0b",
    "\u770b\u4e00\u4e0b",
    "\u53d1\u6211",
    "\u7ed9\u6211",
)
_DATA_LOOKUP_MARKERS = _QUESTION_MARKERS + ("\u7edf\u8ba1", "\u6570\u636e", "\u60c5\u51b5", "\u660e\u7ec6", "\u540d\u5355")
_CASE_DATA_MARKERS = ("\u88ab\u544a", "\u539f\u544a", "\u5b58\u91cf", "\u65b0\u589e", "\u4e0b\u964d\u7387", "\u672a\u7ed3\u6848", "\u7ed3\u6848", "\u6848\u4ef6\u5e95\u8868", "\u6848\u4ef6\u53f0\u8d26")
_CASE_PROGRESS_MARKERS = ("\u6848\u4ef6\u8fdb\u5c55", "\u8fdb\u5c55", "\u5f00\u5ead", "\u6c9f\u901a", "\u8ddf\u8fdb", "\u5904\u7406", "\u6267\u884c", "\u8c03\u89e3", "\u8d77\u8bc9", "\u6750\u6599")
_BUSINESS_OBJECT_MARKERS = _DAILY_OBJECT_MARKERS + (
    "\u5408\u540c",
    "\u6848\u4ef6",
    "\u6848",
    "\u88ab\u544a",
    "\u539f\u544a",
    "\u6708\u62a5",
    "\u7ee9\u6548",
    "\u51fa\u5dee",
    "\u5f00\u5ead",
    "\u5370\u7ae0",
    "\u6cd5\u52a1",
    "\u6cd5\u5b98",
    "\u6cd5\u9662",
    "\u5ba2\u6237",
    "\u94f6\u884c",
    "\u7814\u53d1",
    "\u8fed\u4ee3",
    "\u9700\u6c42",
    "\u63a5\u53e3",
    "\u5546\u52a1",
    "bug",
    "BUG",
)
_CHAT_EXACT_MARKERS = (
    "\u6d4b\u8bd5",
    "\u8bd5\u4e0b",
    "\u8ba9\u6211\u6d4b\u8bd5\u4e0b",
    "\u968f\u4fbf\u8bf4\u8bf4",
    "\u54c8\u54c8",
    "\u54c8\u54e6",
    "\u54ce",
    "\u5509",
    "\u989d",
    "\u5443",
    "\u54e6",
    "\u54e6\u54e6",
    "\u55ef",
    "\u55ef\u55ef",
)
