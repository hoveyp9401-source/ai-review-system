from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.services.dingtalk import validate_dingtalk_outbound_text


PlanProgressStatus = Literal[
    "已完成",
    "持续推进",
    "安排调整",
    "后续安排",
    "暂时没有找到后续记录",
]
PLAN_PROGRESS_STATUSES = frozenset(
    {
        "已完成",
        "持续推进",
        "安排调整",
        "后续安排",
        "暂时没有找到后续记录",
    }
)
_MAX_MESSAGE_CHARS = 3600
_MAX_SECTION_ITEMS = {
    "completed": 12,
    "plan_progress": 12,
    "possible_open_loops": 8,
}


@dataclass(frozen=True)
class PersonalWeeklyBriefWindow:
    week_start: date
    week_end: date
    report_dates: tuple[date, ...]
    snapshot_at: datetime


def derive_personal_weekly_brief_window(
    observed_at: datetime,
    *,
    timezone_name: str,
) -> PersonalWeeklyBriefWindow:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("personal weekly brief time must be timezone-aware")
    local = observed_at.astimezone(ZoneInfo(timezone_name))
    week_start = local.date() - timedelta(days=local.weekday())
    week_end = week_start + timedelta(days=4)
    return PersonalWeeklyBriefWindow(
        week_start=week_start,
        week_end=week_end,
        report_dates=tuple(week_start + timedelta(days=index) for index in range(5)),
        snapshot_at=local,
    )


@dataclass(frozen=True)
class SourceEvidence:
    source_id: str
    source_kind: Literal["daily_report", "weekly_plan"]
    source_record_id: str
    source_date: date
    section: str
    original_text: str

    def __post_init__(self) -> None:
        required = (
            self.source_id,
            self.source_record_id,
            self.section,
            self.original_text,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("personal weekly brief source is incomplete")
        if self.source_kind not in {"daily_report", "weekly_plan"}:
            raise ValueError("personal weekly brief source kind is invalid")

    def as_payload(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_record_id": self.source_record_id,
            "source_date": self.source_date.isoformat(),
            "section": self.section,
            "original_text": self.original_text,
        }


@dataclass(frozen=True)
class PersonalWeeklyBriefSnapshot:
    tenant_id: str
    owner_user_id: str
    week_start: date
    week_end: date
    snapshot_at: datetime
    daily_report_dates: tuple[date, ...]
    weekly_plan_found: bool
    sources: tuple[SourceEvidence, ...]

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.owner_user_id.strip():
            raise ValueError("personal weekly brief snapshot scope is invalid")
        if self.snapshot_at.tzinfo is None or self.snapshot_at.utcoffset() is None:
            raise ValueError("personal weekly brief snapshot time is invalid")
        if self.week_start.weekday() != 0 or self.week_end != self.week_start + timedelta(days=4):
            raise ValueError("personal weekly brief snapshot week is invalid")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("personal weekly brief source ids must be unique")
        if any(
            source.source_kind == "daily_report"
            and not self.week_start <= source.source_date <= self.week_end
            for source in self.sources
        ):
            raise ValueError("daily report source is outside Monday to Friday")
        if any(
            value < self.week_start or value > self.week_end
            for value in self.daily_report_dates
        ):
            raise ValueError("daily report coverage is outside Monday to Friday")

    @property
    def fingerprint(self) -> str:
        stable = {
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "daily_report_dates": [value.isoformat() for value in self.daily_report_dates],
            "weekly_plan_found": self.weekly_plan_found,
            "sources": [source.as_payload() for source in self.sources],
        }
        encoded = json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "snapshot_at": self.snapshot_at.isoformat(),
            "daily_report_dates": [value.isoformat() for value in self.daily_report_dates],
            "weekly_plan_found": self.weekly_plan_found,
            "sources": [source.as_payload() for source in self.sources],
            "source_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PersonalWeeklyBriefSnapshot":
        return cls(
            tenant_id=str(payload["tenant_id"]),
            owner_user_id=str(payload["owner_user_id"]),
            week_start=date.fromisoformat(str(payload["week_start"])),
            week_end=date.fromisoformat(str(payload["week_end"])),
            snapshot_at=datetime.fromisoformat(str(payload["snapshot_at"])),
            daily_report_dates=tuple(
                date.fromisoformat(str(value))
                for value in payload.get("daily_report_dates", ())
            ),
            weekly_plan_found=payload.get("weekly_plan_found") is True,
            sources=tuple(
                SourceEvidence(
                    source_id=str(item["source_id"]),
                    source_kind=str(item["source_kind"]),
                    source_record_id=str(item["source_record_id"]),
                    source_date=date.fromisoformat(str(item["source_date"])),
                    section=str(item["section"]),
                    original_text=str(item["original_text"]),
                )
                for item in payload.get("sources", ())
            ),
        )


@dataclass(frozen=True)
class PersonalWeeklyBriefItem:
    matter_key: str
    text: str
    source_ids: tuple[str, ...]
    status: PlanProgressStatus | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "matter_key": self.matter_key,
            "text": self.text,
            "source_ids": list(self.source_ids),
        }
        if self.status is not None:
            payload["status"] = self.status
        return payload


@dataclass(frozen=True)
class PersonalWeeklyBriefSection:
    empty_note: str
    items: tuple[PersonalWeeklyBriefItem, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "empty_note": self.empty_note,
            "items": [item.as_payload() for item in self.items],
        }


@dataclass(frozen=True)
class PersonalWeeklyBriefContent:
    intro: str
    completed: PersonalWeeklyBriefSection
    plan_progress: PersonalWeeklyBriefSection
    possible_open_loops: PersonalWeeklyBriefSection
    message_text: str
    snapshot: PersonalWeeklyBriefSnapshot

    def as_payload(self) -> dict[str, Any]:
        return {
            "intro": self.intro,
            "completed": self.completed.as_payload(),
            "plan_progress": self.plan_progress.as_payload(),
            "possible_open_loops": self.possible_open_loops.as_payload(),
        }

    def trace_payload(self) -> dict[str, Any]:
        by_id = {source.source_id: source for source in self.snapshot.sources}

        def traced(section: PersonalWeeklyBriefSection) -> list[dict[str, Any]]:
            return [
                {
                    **item.as_payload(),
                    "sources": [by_id[source_id].as_payload() for source_id in item.source_ids],
                }
                for item in section.items
            ]

        return {
            "snapshot_fingerprint": self.snapshot.fingerprint,
            "completed": traced(self.completed),
            "plan_progress": traced(self.plan_progress),
            "possible_open_loops": traced(self.possible_open_loops),
        }


class Agent2PersonalWeeklyBriefGenerator:
    """Use the configured Agent2 model for every semantic conclusion."""

    def __init__(
        self,
        llm_client: Any,
        *,
        model: str,
        thinking_enabled: bool = True,
    ) -> None:
        if not model.strip():
            raise ValueError("Agent2 model is required")
        self._llm_client = llm_client
        self.model = model
        self._thinking_enabled = thinking_enabled

    async def generate(
        self,
        *,
        snapshot: PersonalWeeklyBriefSnapshot,
        recipient_name: str,
        personal_memory: dict[str, Any],
    ) -> PersonalWeeklyBriefContent:
        response = await self._llm_client.complete_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=json.dumps(
                {
                    "task": "生成个人本周工作简报",
                    "recipient": {
                        "authenticated_display_name": recipient_name,
                        "personal_memory": personal_memory,
                    },
                    "trusted_snapshot": snapshot.as_payload(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            model=self.model,
            thinking_enabled=self._thinking_enabled,
            max_tokens=3000,
        )
        try:
            payload = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("personal weekly brief model returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("personal weekly brief model returned invalid payload")
        content = _validated_content(payload, snapshot=snapshot)
        message_text = _render_message(content, snapshot=snapshot)
        return PersonalWeeklyBriefContent(
            intro=content["intro"],
            completed=content["completed"],
            plan_progress=content["plan_progress"],
            possible_open_loops=content["possible_open_loops"],
            message_text=message_text,
            snapshot=snapshot,
        )


class Agent2PersonalWeeklyBriefReviewer:
    """A separate Agent2 pass that cannot rewrite the generated brief."""

    def __init__(
        self,
        llm_client: Any,
        *,
        model: str,
        thinking_enabled: bool = False,
    ) -> None:
        if not model.strip():
            raise ValueError("Agent2 review model is required")
        self._llm_client = llm_client
        self.model = model
        self._thinking_enabled = thinking_enabled

    async def review(
        self,
        *,
        snapshot: PersonalWeeklyBriefSnapshot,
        content: PersonalWeeklyBriefContent,
    ) -> dict[str, Any]:
        raw = await self._llm_client.complete_json(
            system_prompt=_REVIEW_SYSTEM_PROMPT,
            user_prompt=json.dumps(
                {
                    "trusted_snapshot": snapshot.as_payload(),
                    "draft": content.as_payload(),
                    "trace": content.trace_payload(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            model=self.model,
            thinking_enabled=self._thinking_enabled,
            max_tokens=1600,
        )
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("independent model review returned invalid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "approved",
            "reviewed_matter_keys",
            "issues",
        }:
            raise ValueError("independent model review returned invalid payload")
        reviewed = payload["reviewed_matter_keys"]
        issues = payload["issues"]
        if (
            type(payload["approved"]) is not bool
            or not isinstance(reviewed, list)
            or not isinstance(issues, list)
        ):
            raise ValueError("independent model review returned invalid fields")
        expected_keys = {
            item.matter_key
            for section in (
                content.completed,
                content.plan_progress,
                content.possible_open_loops,
            )
            for item in section.items
        }
        reviewed_keys = {str(value).strip() for value in reviewed if str(value).strip()}
        if reviewed_keys != expected_keys or len(reviewed) != len(reviewed_keys):
            raise ValueError("independent model review did not cover every matter")
        normalized_issues: list[dict[str, str]] = []
        for issue in issues:
            if not isinstance(issue, dict) or set(issue) != {"matter_key", "reason"}:
                raise ValueError("independent model review issue is invalid")
            matter_key = _bounded_text(
                issue["matter_key"], field="review.matter_key", maximum=128
            )
            reason = _bounded_text(issue["reason"], field="review.reason", maximum=500)
            if matter_key not in expected_keys:
                raise ValueError("independent model review issue has unknown matter")
            normalized_issues.append({"matter_key": matter_key, "reason": reason})
        if payload["approved"] is not True or normalized_issues:
            raise ValueError("independent model review rejected the brief")
        return {
            "approved": True,
            "reviewed_matter_keys": sorted(reviewed_keys),
            "issues": [],
            "model": self.model,
        }


def _validated_content(
    payload: dict[str, Any],
    *,
    snapshot: PersonalWeeklyBriefSnapshot,
) -> dict[str, Any]:
    expected = {"intro", "completed", "plan_progress", "possible_open_loops"}
    if set(payload) != expected:
        raise ValueError("personal weekly brief model payload keys are invalid")
    intro = _bounded_text(payload["intro"], field="intro", maximum=500)
    source_by_id = {source.source_id: source for source in snapshot.sources}
    completed = _validated_section(
        payload["completed"],
        name="completed",
        source_by_id=source_by_id,
        require_status=False,
    )
    plan_progress = _validated_section(
        payload["plan_progress"],
        name="plan_progress",
        source_by_id=source_by_id,
        require_status=True,
    )
    possible_open_loops = _validated_section(
        payload["possible_open_loops"],
        name="possible_open_loops",
        source_by_id=source_by_id,
        require_status=False,
    )
    plan_keys = {item.matter_key for item in plan_progress.items}
    open_keys = {item.matter_key for item in possible_open_loops.items}
    if plan_keys & open_keys:
        raise ValueError("duplicate matter across sections")
    return {
        "intro": intro,
        "completed": completed,
        "plan_progress": plan_progress,
        "possible_open_loops": possible_open_loops,
    }


def _validated_section(
    value: Any,
    *,
    name: str,
    source_by_id: dict[str, SourceEvidence],
    require_status: bool,
) -> PersonalWeeklyBriefSection:
    if not isinstance(value, dict) or set(value) != {"empty_note", "items"}:
        raise ValueError(f"personal weekly brief {name} section is invalid")
    raw_items = value["items"]
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_SECTION_ITEMS[name]:
        raise ValueError(f"personal weekly brief {name} items are invalid")
    empty_note = str(value["empty_note"] or "").strip()
    if raw_items and empty_note:
        raise ValueError(f"personal weekly brief {name} cannot mix items and empty note")
    if not raw_items:
        empty_note = _bounded_text(empty_note, field=f"{name}.empty_note", maximum=500)
    items: list[PersonalWeeklyBriefItem] = []
    for raw in raw_items:
        expected = {"matter_key", "text", "source_ids"}
        if require_status:
            expected.add("status")
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError(f"personal weekly brief {name} item is invalid")
        matter_key = _bounded_text(raw["matter_key"], field="matter_key", maximum=128)
        text = _bounded_text(raw["text"], field="item.text", maximum=1000)
        source_ids_raw = raw["source_ids"]
        if (
            not isinstance(source_ids_raw, list)
            or not source_ids_raw
            or len(source_ids_raw) > 32
        ):
            raise ValueError("personal weekly brief item sources are invalid")
        source_ids = tuple(str(source_id).strip() for source_id in source_ids_raw)
        if len(source_ids) != len(set(source_ids)) or any(
            not source_id or source_id not in source_by_id for source_id in source_ids
        ):
            raise ValueError("personal weekly brief item has unknown sources")
        resolved_sources = tuple(source_by_id[source_id] for source_id in source_ids)
        if name == "completed" and not any(
            source.source_kind == "daily_report" and source.section == "today_work"
            for source in resolved_sources
        ):
            raise ValueError("completed item requires today_work evidence")
        status = str(raw.get("status") or "").strip() if require_status else None
        if require_status:
            if status not in PLAN_PROGRESS_STATUSES:
                raise ValueError("personal weekly brief plan status is invalid")
            _validate_plan_evidence(
                status=status,
                sources=resolved_sources,
            )
        items.append(
            PersonalWeeklyBriefItem(
                matter_key=matter_key,
                text=text,
                source_ids=source_ids,
                status=status,
            )
        )
    matter_keys = [item.matter_key for item in items]
    if len(matter_keys) != len(set(matter_keys)):
        raise ValueError(f"duplicate matter within {name}")
    return PersonalWeeklyBriefSection(empty_note=empty_note, items=tuple(items))


def _validate_plan_evidence(
    *,
    status: str,
    sources: tuple[SourceEvidence, ...],
) -> None:
    plan_sources = tuple(source for source in sources if source.source_kind == "weekly_plan")
    if not plan_sources:
        raise ValueError("plan progress requires weekly plan evidence")
    earliest_plan_date = min(source.source_date for source in plan_sources)
    later_daily_sources = tuple(
        source
        for source in sources
        if source.source_kind == "daily_report" and source.source_date >= earliest_plan_date
    )
    if status == "暂时没有找到后续记录":
        if later_daily_sources:
            raise ValueError("no-follow-up status cannot cite later daily evidence")
        return
    if not later_daily_sources:
        raise ValueError("plan status requires later daily evidence")


def _render_message(
    content: dict[str, Any],
    *,
    snapshot: PersonalWeeklyBriefSnapshot,
) -> str:
    lines = [
        content["intro"],
        "",
        "一、本周完成事项",
        *_render_section(content["completed"], include_status=False),
        "",
        "二、本周计划事项及进展",
        *_render_section(content["plan_progress"], include_status=True),
        "",
        "三、可能未闭环事项",
        *_render_section(content["possible_open_loops"], include_status=False),
        "",
        (
            f"数据范围：{snapshot.week_start.isoformat()} 至 "
            f"{snapshot.week_end.isoformat()}，以周六生成时系统已保存的记录为准。"
        ),
        "“暂时没有找到后续记录”只表示系统没有找到后来记录，不等于未完成。",
    ]
    message = "\n".join(lines).strip()
    validate_dingtalk_outbound_text(message)
    if len(message) > _MAX_MESSAGE_CHARS:
        raise ValueError("personal weekly brief message is too long")
    return message


def _render_section(
    section: PersonalWeeklyBriefSection,
    *,
    include_status: bool,
) -> list[str]:
    if not section.items:
        return [section.empty_note]
    return [
        (
            f"{index}. 【{item.status}】{item.text}"
            if include_status
            else f"{index}. {item.text}"
        )
        for index, item in enumerate(section.items, start=1)
    ]


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"personal weekly brief {field} is invalid")
    return normalized


_SYSTEM_PROMPT = """你是 Agent2 内的“个人本周工作简报”总结能力。你只处理服务器给出的本人可信快照，不得查询、猜测或提及他人内容。

请把当周周一至周五的日报与本周周计划整理成自然、简洁、像服务同事的中文。所有语义判断、跨日合并、计划进展判断和未闭环判断都由你完成，但必须遵守：
1. 只能使用 trusted_snapshot.sources；每条结论必须列出实际支持它的 source_ids，不得伪造来源，也不得声称“已核对”。completed 中每一项必须至少引用一条 section=today_work 的日报来源；问题、明日计划或周计划只能作为补充证据，不能单独成为“本周完成事项”。
2. 今日工作中的“跟进、沟通、准备、起草、计划”等不得改写成“完成”。金额、日期、对象、条件、否定等关键事实不能遗漏或改变。
3. 同一事项跨多天可合并为一条，保留所有关键进展和事实。
4. 计划进展状态只能是：已完成、持续推进、安排调整、后续安排、暂时没有找到后续记录。除最后一种外，必须同时引用周计划和当日或后来日报证据；没有后来记录时只能用最后一种，绝不能说“未完成”。
5. plan_progress 与 possible_open_loops 中同一事项只能出现一次，并使用相同的稳定 matter_key 来帮助服务器去重。
6. 没有数据、只有部分日期、没有周计划或没有风险栏时如实说明，不得编造。
7. 简报只读，不得建议系统已经修改、补写、确认或提交日报、周计划。
8. 不要在文字中称呼用户；称呼由服务器根据个人记忆安全添加。

仅返回 JSON，严格使用以下结构，不得增加字段：
{
  "intro": "一句简短开场",
  "completed": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "结论", "source_ids": ["来源ID"]}]},
  "plan_progress": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "进展", "status": "五种状态之一", "source_ids": ["来源ID"]}]},
  "possible_open_loops": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "谨慎说明", "source_ids": ["来源ID"]}]}
}"""


_REVIEW_SYSTEM_PROMPT = """你是 Agent2 内独立的个人周简报事实复核步骤。你不能改写草稿，只能逐项判断是否安全通过。

对每个 matter_key 检查：
1. 结论引用的 source_ids 是否真的支持文字，是否有伪造来源；completed 每项是否至少引用 today_work 日报来源；
2. 是否改变或遗漏金额、日期、对象、条件、否定、归属和完成状态；
3. 是否把跟进、沟通、准备、起草或计划武断写成完成；
4. 计划状态是否与周计划及后来日报证据一致；没有后来记录时是否只使用“暂时没有找到后续记录”；
5. 计划进展和可能未闭环中是否重复同一事项；
6. 无数据或部分数据时是否编造。

必须覆盖草稿里的每个唯一 matter_key。只返回 JSON：
{
  "approved": true或false,
  "reviewed_matter_keys": ["逐个唯一事项键"],
  "issues": [{"matter_key": "事项键", "reason": "不通过原因"}]
}
只要一项不安全，approved 必须为 false。不得自行生成替换文字。"""


__all__ = [
    "Agent2PersonalWeeklyBriefGenerator",
    "Agent2PersonalWeeklyBriefReviewer",
    "PLAN_PROGRESS_STATUSES",
    "PersonalWeeklyBriefContent",
    "PersonalWeeklyBriefItem",
    "PersonalWeeklyBriefSection",
    "PersonalWeeklyBriefSnapshot",
    "PersonalWeeklyBriefWindow",
    "SourceEvidence",
    "derive_personal_weekly_brief_window",
]
