from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from time import perf_counter
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.services.dingtalk import validate_dingtalk_outbound_text
from app.utils.json import extract_json_object


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
_MAX_SOURCE_COUNT = 120
_MAX_SOURCE_TEXT_CHARS = 2000
_MAX_TOTAL_SOURCE_CHARS = 30000
_MAX_SECTION_ITEMS = {
    "completed": 12,
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
        if len(self.original_text) > _MAX_SOURCE_TEXT_CHARS:
            raise ValueError("personal weekly brief source text is too long")

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
        if len(self.sources) > _MAX_SOURCE_COUNT:
            raise ValueError("personal weekly brief has too many sources")
        if sum(len(source.original_text) for source in self.sources) > _MAX_TOTAL_SOURCE_CHARS:
            raise ValueError("personal weekly brief total source text is too long")
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
class PersonalWeeklyBriefSourceDisposition:
    source_id: str
    disposition: Literal["cited", "safely_excluded"]
    reason: str = ""

    def as_payload(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "disposition": self.disposition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PersonalWeeklyBriefContent:
    intro: str
    completed: PersonalWeeklyBriefSection
    plan_progress: PersonalWeeklyBriefSection
    possible_open_loops: PersonalWeeklyBriefSection
    source_dispositions: tuple[PersonalWeeklyBriefSourceDisposition, ...]
    message_text: str
    snapshot: PersonalWeeklyBriefSnapshot

    def as_payload(self) -> dict[str, Any]:
        return {
            "intro": self.intro,
            "completed": self.completed.as_payload(),
            "plan_progress": self.plan_progress.as_payload(),
            "possible_open_loops": self.possible_open_loops.as_payload(),
            "source_dispositions": [
                disposition.as_payload() for disposition in self.source_dispositions
            ],
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
            "source_dispositions": [
                disposition.as_payload() for disposition in self.source_dispositions
            ],
        }


class Agent2PersonalWeeklyBriefGenerator:
    """Use the configured Agent2 model for every semantic conclusion."""

    def __init__(
        self,
        llm_client: Any,
        *,
        model: str,
        thinking_enabled: bool = True,
        timeout_seconds: float = 60.0,
        max_retries: int = 1,
    ) -> None:
        if not model.strip():
            raise ValueError("Agent2 model is required")
        self._llm_client = llm_client
        self.model = model
        self._thinking_enabled = thinking_enabled
        if timeout_seconds <= 0 or max_retries < 0 or max_retries > 1:
            raise ValueError("Agent2 weekly brief request limits are invalid")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    async def generate(
        self,
        *,
        snapshot: PersonalWeeklyBriefSnapshot,
        recipient_name: str,
        personal_memory: dict[str, Any],
        repair_context: dict[str, Any] | None = None,
    ) -> PersonalWeeklyBriefContent:
        user_payload: dict[str, Any] = {
            "task": "生成个人本周工作简报",
            "recipient": {
                "authenticated_display_name": recipient_name,
                "personal_memory": personal_memory,
            },
            "trusted_snapshot": snapshot.as_payload(),
        }
        if repair_context is not None:
            encoded_repair = json.dumps(
                repair_context,
                ensure_ascii=False,
                sort_keys=True,
            )
            if len(encoded_repair) > 12000:
                raise ValueError("personal weekly brief repair context is too long")
            user_payload["repair_context"] = repair_context
        response = await self._llm_client.complete_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=json.dumps(
                user_payload,
                ensure_ascii=False,
                sort_keys=True,
            ),
            model=self.model,
            thinking_enabled=self._thinking_enabled,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            max_tokens=8000,
        )
        try:
            payload = extract_json_object(response)
            content = _validated_content(payload, snapshot=snapshot)
            message_text = _render_message(content, snapshot=snapshot)
        except (TypeError, ValueError) as exc:
            detail = str(exc)
            if "JSON" not in detail and "JSON object" not in detail:
                detail = f"invalid output: {detail}"
            raise PersonalWeeklyBriefModelOutputInvalid(detail) from exc
        return PersonalWeeklyBriefContent(
            intro=content["intro"],
            completed=content["completed"],
            plan_progress=content["plan_progress"],
            possible_open_loops=content["possible_open_loops"],
            source_dispositions=content["source_dispositions"],
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
        timeout_seconds: float = 60.0,
        max_retries: int = 1,
    ) -> None:
        if not model.strip():
            raise ValueError("Agent2 review model is required")
        self._llm_client = llm_client
        self.model = model
        self._thinking_enabled = thinking_enabled
        if timeout_seconds <= 0 or max_retries < 0 or max_retries > 1:
            raise ValueError("Agent2 weekly brief review limits are invalid")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

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
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            max_tokens=8000,
        )
        try:
            payload = extract_json_object(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("independent model review returned invalid JSON") from exc
        if set(payload) != {
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
            if matter_key not in expected_keys and matter_key not in {
                source.source_id for source in snapshot.sources
            }:
                raise ValueError("independent model review issue has unknown matter")
            normalized_issues.append({"matter_key": matter_key, "reason": reason})
        if payload["approved"] is not True or normalized_issues:
            raise PersonalWeeklyBriefReviewRejected(normalized_issues)
        return {
            "approved": True,
            "reviewed_matter_keys": sorted(reviewed_keys),
            "issues": [],
            "model": self.model,
        }


class PersonalWeeklyBriefReviewRejected(ValueError):
    """A safe rejection whose message never includes user-derived content."""

    def __init__(self, issues: list[dict[str, str]]) -> None:
        super().__init__("independent model review rejected the brief")
        self.issues = tuple(dict(issue) for issue in issues)


class PersonalWeeklyBriefModelOutputInvalid(ValueError):
    """A model response failed JSON or deterministic source validation."""


@dataclass(frozen=True)
class PersonalWeeklyBriefModelOutcome:
    content: PersonalWeeklyBriefContent
    review: dict[str, Any]
    semantic_attempts: int
    model_calls: int
    generation_seconds: tuple[float, ...]
    review_seconds: tuple[float, ...]
    total_seconds: float


class Agent2PersonalWeeklyBriefModelPipeline:
    """Generate, independently review, and perform at most one semantic repair."""

    def __init__(
        self,
        *,
        generator: Agent2PersonalWeeklyBriefGenerator,
        reviewer: Agent2PersonalWeeklyBriefReviewer,
        max_semantic_attempts: int = 2,
    ) -> None:
        if max_semantic_attempts != 2:
            raise ValueError("personal weekly brief semantic attempts must equal two")
        self._generator = generator
        self._reviewer = reviewer
        self._max_semantic_attempts = max_semantic_attempts

    async def generate_and_review(
        self,
        *,
        snapshot: PersonalWeeklyBriefSnapshot,
        recipient_name: str,
        personal_memory: dict[str, Any],
    ) -> PersonalWeeklyBriefModelOutcome:
        started = perf_counter()
        repair_context: dict[str, Any] | None = None
        generation_durations: list[float] = []
        review_durations: list[float] = []
        model_calls = 0
        for attempt in range(1, self._max_semantic_attempts + 1):
            generation_started = perf_counter()
            model_calls += 1
            try:
                content = await self._generator.generate(
                    snapshot=snapshot,
                    recipient_name=recipient_name,
                    personal_memory=personal_memory,
                    repair_context=repair_context,
                )
            except PersonalWeeklyBriefModelOutputInvalid as exc:
                generation_durations.append(perf_counter() - generation_started)
                if attempt >= self._max_semantic_attempts:
                    raise
                repair_context = {
                    "instruction": (
                        "上一版模型输出未通过服务器结构或来源校验。"
                        "保持完整 trusted_snapshot，重新返回严格的完整 JSON；"
                        "只能包含规定的五个顶层字段。"
                    ),
                    "issues": [
                        {
                            "matter_key": "__model_output__",
                            "reason": str(exc),
                        }
                    ],
                }
                continue
            generation_durations.append(perf_counter() - generation_started)
            review_started = perf_counter()
            model_calls += 1
            try:
                review = await self._reviewer.review(
                    snapshot=snapshot,
                    content=content,
                )
            except PersonalWeeklyBriefReviewRejected as exc:
                review_durations.append(perf_counter() - review_started)
                if attempt >= self._max_semantic_attempts:
                    raise
                repair_context = {
                    "instruction": (
                        "上一版未通过独立事实复核。保持同一可信快照，"
                        "逐项修复下列问题并重新返回完整 JSON；不得删除未被指出的关键事实。"
                        "只能返回 intro、completed、plan_progress、possible_open_loops、"
                        "source_dispositions 五个顶层字段，不得增加说明或其他字段。"
                    ),
                    "previous_draft": content.as_payload(),
                    "issues": list(exc.issues)[:20],
                }
                continue
            except ValueError:
                review_durations.append(perf_counter() - review_started)
                if attempt >= self._max_semantic_attempts:
                    raise
                repair_context = {
                    "instruction": (
                        "上一轮独立复核输出未通过服务器 JSON/结构校验。"
                        "保持完整 trusted_snapshot，重新生成完整五字段 JSON，"
                        "随后服务器会重新执行独立复核。"
                    ),
                    "previous_draft": content.as_payload(),
                    "issues": [
                        {
                            "matter_key": "__review_output__",
                            "reason": "独立复核输出无效，需要重新生成后再次复核。",
                        }
                    ],
                }
                continue
            review_durations.append(perf_counter() - review_started)
            return PersonalWeeklyBriefModelOutcome(
                content=content,
                review=review,
                semantic_attempts=attempt,
                model_calls=model_calls,
                generation_seconds=tuple(generation_durations),
                review_seconds=tuple(review_durations),
                total_seconds=perf_counter() - started,
            )
        raise AssertionError("personal weekly brief pipeline exited unexpectedly")


def _validated_content(
    payload: dict[str, Any],
    *,
    snapshot: PersonalWeeklyBriefSnapshot,
) -> dict[str, Any]:
    required = {"intro", "completed", "plan_progress", "possible_open_loops"}
    allowed = required | {"source_dispositions"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        missing = sorted(required - set(payload))
        unexpected = sorted(set(payload) - allowed)
        raise ValueError(
            "personal weekly brief model payload keys are invalid "
            f"missing={missing!r} unexpected={unexpected!r}"
        )
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
    expected_plan_source_ids = {
        source_id
        for source_id, source in source_by_id.items()
        if source.source_kind == "weekly_plan"
    }
    cited_plan_source_ids = [
        source_id
        for item in plan_progress.items
        for source_id in item.source_ids
        if source_by_id[source_id].source_kind == "weekly_plan"
    ]
    if (
        set(cited_plan_source_ids) != expected_plan_source_ids
        or len(cited_plan_source_ids) != len(set(cited_plan_source_ids))
    ):
        raise ValueError("plan progress must cover every weekly plan source once")
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
    plan_progress_source_ids = {
        source_id
        for item in plan_progress.items
        for source_id in item.source_ids
    }
    open_source_ids = {
        source_id
        for item in possible_open_loops.items
        for source_id in item.source_ids
    }
    if expected_plan_source_ids & open_source_ids:
        raise ValueError("weekly plan source cannot repeat in open loops")
    if plan_progress_source_ids & open_source_ids:
        raise ValueError("plan progress source cannot repeat in open loops")
    all_cited_source_ids = {
        source_id
        for section in (completed, plan_progress, possible_open_loops)
        for item in section.items
        for source_id in item.source_ids
    }
    source_dispositions = _validated_source_dispositions(
        payload.get("source_dispositions"),
        source_by_id=source_by_id,
        cited_source_ids=all_cited_source_ids,
    )
    return {
        "intro": intro,
        "completed": completed,
        "plan_progress": plan_progress,
        "possible_open_loops": possible_open_loops,
        "source_dispositions": source_dispositions,
    }


def _validated_source_dispositions(
    value: Any,
    *,
    source_by_id: dict[str, SourceEvidence],
    cited_source_ids: set[str],
) -> tuple[PersonalWeeklyBriefSourceDisposition, ...]:
    expected_source_ids = set(source_by_id)
    if value is None:
        if cited_source_ids != expected_source_ids:
            raise ValueError("personal weekly brief frozen source universe is incomplete")
        return tuple(
            PersonalWeeklyBriefSourceDisposition(
                source_id=source_id,
                disposition="cited",
            )
            for source_id in source_by_id
        )
    if not isinstance(value, list) or len(value) != len(expected_source_ids):
        raise ValueError("personal weekly brief source dispositions are incomplete")
    dispositions: list[PersonalWeeklyBriefSourceDisposition] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "source_id",
            "disposition",
            "reason",
        }:
            raise ValueError("personal weekly brief source disposition is invalid")
        source_id = str(raw["source_id"] or "").strip()
        disposition = str(raw["disposition"] or "").strip()
        reason = str(raw["reason"] or "").strip()
        if source_id not in expected_source_ids or source_id in seen:
            raise ValueError("personal weekly brief source disposition identity is invalid")
        seen.add(source_id)
        if disposition == "cited":
            if source_id not in cited_source_ids or reason:
                raise ValueError("cited source disposition does not match the brief")
        elif disposition == "safely_excluded":
            if source_id in cited_source_ids or not reason or len(reason) > 500:
                raise ValueError("excluded source requires one safe reason")
        else:
            raise ValueError("personal weekly brief source disposition type is invalid")
        dispositions.append(
            PersonalWeeklyBriefSourceDisposition(
                source_id=source_id,
                disposition=disposition,
                reason=reason,
            )
        )
    if seen != expected_source_ids:
        raise ValueError("personal weekly brief frozen source universe is incomplete")
    return tuple(dispositions)


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
    if name == "plan_progress":
        maximum_items = sum(
            source.source_kind == "weekly_plan" for source in source_by_id.values()
        )
    else:
        maximum_items = _MAX_SECTION_ITEMS[name]
    if not isinstance(raw_items, list) or len(raw_items) > maximum_items:
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
3. 同一项目、案件或事项跨多天重复且指向明确时，必须合并为一条，保留所有关键进展和事实；只有无法可靠判断是否同一事项时才分开，不得靠猜测强行合并。
4. 计划进展状态只能是：已完成、持续推进、安排调整、后续安排、暂时没有找到后续记录。除最后一种外，必须同时引用周计划和当日或后来日报证据；没有后来记录时只能用最后一种，绝不能说“未完成”。trusted_snapshot 中每个 source_kind=weekly_plan 的 source_id 都必须在 plan_progress 中恰好引用一次；同一事项跨日时可以在一条进展中引用多条周计划来源，但不得漏项或重复。
5. plan_progress 与 possible_open_loops 中同一事项只能出现一次，并使用相同的稳定 matter_key 来帮助服务器去重；plan_progress 已引用的任何 source_id 都不能再次用于 possible_open_loops，即使换了 matter_key 也不行。
6. 没有数据、只有部分日期、没有周计划或没有风险栏时如实说明，不得编造。
7. 简报只读，不得建议系统已经修改、补写、确认或提交日报、周计划。
8. 不要在文字中称呼用户；称呼由服务器根据个人记忆安全添加。
9. 把 trusted_snapshot.sources 当作完整来源清单。每个 source_id 必须在 source_dispositions 中恰好出现一次：被成品条目引用时标为 cited 且 reason 为空；未引用时只能标为 safely_excluded，并给出具体、谨慎的安全排除理由。不得静默遗漏整条来源，也不得用“内容不重要”等空泛理由排除。
10. 如果 user_prompt 含 repair_context，上一版已被独立复核拒绝。必须根据 issues 修复上一版，重新输出完整结构；可信来源仍只有 trusted_snapshot，不能为了通过复核编造或删掉其他关键事实。修复结果也只能包含下方规定的五个顶层字段，不能附加修复说明或其他字段。

仅返回 JSON，严格使用以下结构，不得增加字段：
{
  "intro": "一句简短开场",
  "completed": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "结论", "source_ids": ["来源ID"]}]},
  "plan_progress": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "进展", "status": "五种状态之一", "source_ids": ["来源ID"]}]},
  "possible_open_loops": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "谨慎说明", "source_ids": ["来源ID"]}]},
  "source_dispositions": [{"source_id": "来源ID", "disposition": "cited或safely_excluded", "reason": "cited时为空；安全排除时写具体理由"}]
}"""


_REVIEW_SYSTEM_PROMPT = """你是 Agent2 内独立的个人周简报事实复核步骤。你不能改写草稿，只能逐项判断是否安全通过。

必须先按 trace 中每一条实际引用的 source_id 逐条对照原文，再按 matter_key 汇总结论；不能只看草稿是否通顺。对每个 matter_key 检查：
1. 结论引用的 source_ids 是否真的支持文字，是否有伪造来源；completed 每项是否至少引用 today_work 日报来源；
2. 对该事项引用的每个 source_id，是否改变或遗漏其中任何金额、日期、对象、条件、否定、归属和完成状态；只要一个来源中的关键事实没有进入结论，就必须拒绝；
3. 是否把跟进、沟通、准备、起草或计划武断写成完成；
4. 计划状态是否与周计划及后来日报证据一致；没有后来记录时是否只使用“暂时没有找到后续记录”；每条 weekly_plan 来源是否在计划进展中恰好出现一次。“后续安排”可以由后来日报的 tomorrow_plan 支持，它不表示已完成；“安排调整”只需后来日报明确记录安排发生变化，不要求再有调整后事项的最终完成证据。
5. 计划进展和可能未闭环中是否重复同一事项。仅来自日报、并非周计划的谨慎未闭环事项可以只出现在 possible_open_loops，不要求进入 plan_progress；已经在 plan_progress 中说明调整或未找到后续记录的计划事项，不应再复制到 possible_open_loops。
6. 无数据或部分数据时是否编造。
7. source_dispositions 是否逐条覆盖冻结来源全集；所有 cited 是否真的被条目引用；所有 safely_excluded 是否未被引用且理由具体、安全，没有借排除理由静默丢失应汇总事实。

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
    "Agent2PersonalWeeklyBriefModelPipeline",
    "PLAN_PROGRESS_STATUSES",
    "PersonalWeeklyBriefContent",
    "PersonalWeeklyBriefItem",
    "PersonalWeeklyBriefModelOutcome",
    "PersonalWeeklyBriefModelOutputInvalid",
    "PersonalWeeklyBriefSection",
    "PersonalWeeklyBriefSnapshot",
    "PersonalWeeklyBriefSourceDisposition",
    "PersonalWeeklyBriefReviewRejected",
    "PersonalWeeklyBriefWindow",
    "SourceEvidence",
    "derive_personal_weekly_brief_window",
]
