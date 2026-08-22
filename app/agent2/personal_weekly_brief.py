from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import re
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
PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED = False
PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED = False
PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED = True
PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS = 4000
PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS = 2000
PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS = 4000
PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS = 3
PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES = 3
_MAX_SECTION_ITEMS = {
    "completed": 12,
    "possible_open_loops": 8,
}
_EMPTY_SECTION_NOTES = {
    "completed": "本周没有找到已保存的今日工作记录。",
    "plan_progress": "本周没有找到可核对的周计划事项。",
    "possible_open_loops": "本周暂时没有找到需要单独提醒的未闭环事项。",
}
_CRITICAL_LITERAL_PATTERNS = (
    re.compile(r"(?<!\d)\d{1,4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"(?<!\d)\d{1,2}月\d{1,2}日"),
    re.compile(r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"),
    re.compile(r"(?<!\d)\d{1,2}[-/]\d{1,2}(?!\d)"),
    re.compile(r"[¥￥]\s?\d[\d,]*(?:\.\d+)?"),
    re.compile(
        r"(?<!\d)\d[\d,]*(?:\.\d+)?"
        r"(?:亿元|万元|千元|百万元|亿|万|元|%|笔|份|项|人|天)"
    ),
)
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
        explanation_basis = {
            "snapshot_fingerprint": self.snapshot.fingerprint,
            "source_count": len(self.snapshot.sources),
            "daily_report_dates": [
                value.isoformat() for value in self.snapshot.daily_report_dates
            ],
            "weekly_plan_found": self.snapshot.weekly_plan_found,
        }

        def system_explanation() -> dict[str, Any]:
            return {
                "classification": "system_explanation",
                "business_conclusion": False,
                "basis": dict(explanation_basis),
            }

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
            "system_explanations": {
                "intro": system_explanation(),
                "empty_notes": {
                    section_name: system_explanation()
                    for section_name, section in (
                        ("completed", self.completed),
                        ("plan_progress", self.plan_progress),
                        ("possible_open_loops", self.possible_open_loops),
                    )
                    if section.empty_note
                },
            },
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
        max_tokens: int = 8000,
    ) -> None:
        if not model.strip():
            raise ValueError("Agent2 model is required")
        self._llm_client = llm_client
        self.model = model
        self._thinking_enabled = thinking_enabled
        if (
            timeout_seconds <= 0
            or max_retries < 0
            or max_retries > 1
            or max_tokens < 500
            or max_tokens > 8000
        ):
            raise ValueError("Agent2 weekly brief request limits are invalid")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_tokens = max_tokens

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
            max_tokens=self._max_tokens,
        )
        try:
            payload = extract_json_object(response)
            content = _validated_content(payload, snapshot=snapshot)
            message_text = _render_message(content, snapshot=snapshot)
        except (TypeError, ValueError) as exc:
            detail = str(exc)
            if "JSON" not in detail and "JSON object" not in detail:
                detail = f"invalid output: {detail}"
            raise PersonalWeeklyBriefModelOutputInvalid(
                repair_detail=detail
            ) from None
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
        max_tokens: int = 8000,
        review_mode: Literal["general", "critical_facts"] = "general",
    ) -> None:
        if not model.strip():
            raise ValueError("Agent2 review model is required")
        if review_mode not in {"general", "critical_facts"}:
            raise ValueError("Agent2 weekly brief review mode is invalid")
        self._llm_client = llm_client
        self.model = model
        self._thinking_enabled = thinking_enabled
        if (
            timeout_seconds <= 0
            or max_retries < 0
            or max_retries > 1
            or max_tokens < 500
            or max_tokens > 8000
        ):
            raise ValueError("Agent2 weekly brief review limits are invalid")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._system_prompt = (
            _CRITICAL_FACT_REVIEW_SYSTEM_PROMPT
            if review_mode == "critical_facts"
            else _REVIEW_SYSTEM_PROMPT
        )

    async def review(
        self,
        *,
        snapshot: PersonalWeeklyBriefSnapshot,
        content: PersonalWeeklyBriefContent,
        disputed_issues: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        user_payload: dict[str, Any] = {
            "trusted_snapshot": snapshot.as_payload(),
            "draft": content.as_payload(),
            "server_checks": {
                "recognized_amount_date_literals_complete": True,
                "frozen_source_dispositions_complete": True,
                "weekly_plan_sources_exactly_once": True,
                "section_source_bindings_valid": True,
            },
        }
        if disputed_issues is not None:
            user_payload["disputed_issues"] = disputed_issues[:20]
        raw = await self._llm_client.complete_json(
            system_prompt=self._system_prompt,
            user_prompt=json.dumps(
                user_payload,
                ensure_ascii=False,
                sort_keys=True,
            ),
            model=self.model,
            thinking_enabled=self._thinking_enabled,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            max_tokens=self._max_tokens,
        )
        try:
            payload = extract_json_object(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("independent model review returned invalid JSON") from exc
        required_review_fields = {
            "approved",
            "reviewed_matter_keys",
            "issues",
        }
        if not required_review_fields.issubset(payload):
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
                matter_key = "__brief__"
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
    """Keep user-derived repair detail away from logs and tracebacks."""

    def __init__(self, *, repair_detail: str) -> None:
        super().__init__("personal weekly brief model output failed validation")
        self.repair_detail = repair_detail


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
        critical_reviewer: Agent2PersonalWeeklyBriefReviewer | None = None,
        max_semantic_attempts: int = 2,
        review_votes: int = 1,
    ) -> None:
        if max_semantic_attempts not in {2, 3}:
            raise ValueError("personal weekly brief semantic attempts must be bounded")
        if review_votes not in {1, 3}:
            raise ValueError("personal weekly brief review votes must be one or three")
        self._generator = generator
        self._reviewer = reviewer
        self._critical_reviewer = critical_reviewer
        self._max_semantic_attempts = max_semantic_attempts
        self._review_votes = review_votes

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
                            "reason": exc.repair_detail,
                        }
                    ],
                }
                continue
            generation_durations.append(perf_counter() - generation_started)
            approvals: list[dict[str, Any]] = []
            rejections: list[dict[str, str]] = []
            rejection_votes = 0
            invalid_reviews = 0

            async def cast_review_vote(
                *,
                disputed_issues: list[dict[str, str]] | None = None,
            ) -> None:
                nonlocal model_calls, rejection_votes, invalid_reviews
                review_started = perf_counter()
                model_calls += 1
                try:
                    vote = await self._reviewer.review(
                        snapshot=snapshot,
                        content=content,
                        disputed_issues=disputed_issues,
                    )
                except PersonalWeeklyBriefReviewRejected as exc:
                    rejection_votes += 1
                    rejections.extend(dict(issue) for issue in exc.issues)
                except ValueError:
                    invalid_reviews += 1
                else:
                    approvals.append(vote)
                finally:
                    review_durations.append(perf_counter() - review_started)

            initial_votes = 1 if self._review_votes == 1 else 2
            for _vote in range(initial_votes):
                await cast_review_vote()

            required_votes = self._review_votes // 2 + 1
            if (
                self._review_votes == 3
                and len(approvals) < required_votes
                and rejection_votes < required_votes
            ):
                disputed: list[dict[str, str]] = []
                seen_disputes: set[tuple[str, str]] = set()
                for issue in rejections:
                    key = (issue["matter_key"], issue["reason"])
                    if key not in seen_disputes:
                        seen_disputes.add(key)
                        disputed.append(issue)
                if invalid_reviews:
                    disputed.append(
                        {
                            "matter_key": "__review_output__",
                            "reason": "一次初审输出无效，请独立裁决现有草稿。",
                        }
                    )
                await cast_review_vote(disputed_issues=disputed)

            if len(approvals) >= required_votes:
                review = dict(approvals[0])
                if self._review_votes > 1:
                    review["review_consensus"] = {
                        "required": required_votes,
                        "approved": len(approvals),
                        "rejected": rejection_votes,
                        "invalid": invalid_reviews,
                        "votes_cast": len(approvals)
                        + rejection_votes
                        + invalid_reviews,
                        "dispute_adjudicated": (
                            len(approvals) + rejection_votes + invalid_reviews
                            > initial_votes
                        ),
                    }
                critical_issues: list[dict[str, str]] = []
                critical_invalid = False
                if self._critical_reviewer is not None:
                    critical_started = perf_counter()
                    model_calls += 1
                    try:
                        critical_review = await self._critical_reviewer.review(
                            snapshot=snapshot,
                            content=content,
                        )
                    except PersonalWeeklyBriefReviewRejected as exc:
                        critical_issues.extend(dict(issue) for issue in exc.issues)
                    except ValueError:
                        critical_invalid = True
                    else:
                        review["critical_fact_review"] = critical_review
                    finally:
                        review_durations.append(perf_counter() - critical_started)
                if not critical_issues and not critical_invalid:
                    return PersonalWeeklyBriefModelOutcome(
                        content=content,
                        review=review,
                        semantic_attempts=attempt,
                        model_calls=model_calls,
                        generation_seconds=tuple(generation_durations),
                        review_seconds=tuple(review_durations),
                        total_seconds=perf_counter() - started,
                    )
                if critical_issues:
                    rejection_votes = max(rejection_votes, required_votes)
                    rejections.extend(critical_issues)
                if critical_invalid:
                    invalid_reviews += 1

            unique_issues: list[dict[str, str]] = []
            seen_issues: set[tuple[str, str]] = set()
            for issue in rejections:
                key = (issue["matter_key"], issue["reason"])
                if key not in seen_issues:
                    seen_issues.add(key)
                    unique_issues.append(issue)
            if attempt >= self._max_semantic_attempts:
                if rejection_votes:
                    raise PersonalWeeklyBriefReviewRejected(unique_issues[:20])
                raise ValueError("independent model review returned invalid JSON")
            if invalid_reviews:
                unique_issues.append(
                    {
                        "matter_key": "__review_output__",
                        "reason": "独立复核输出无效，需要重新生成后再次复核。",
                    }
                )
            repair_context = {
                "instruction": (
                    "上一版未通过独立事实复核多数判断。保持同一可信快照，"
                    "逐项修复下列问题并重新返回完整 JSON；不得删除未被指出的关键事实。"
                    "只能返回 intro、completed、plan_progress、possible_open_loops、"
                    "source_dispositions 五个顶层字段，不得增加说明或其他字段。"
                ),
                "previous_draft": content.as_payload(),
                "issues": unique_issues[:20],
            }
            continue
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
    _validate_critical_literal_coverage(
        source_by_id=source_by_id,
        sections=(completed, plan_progress, possible_open_loops),
        source_dispositions=source_dispositions,
    )
    return {
        "intro": intro,
        "completed": completed,
        "plan_progress": plan_progress,
        "possible_open_loops": possible_open_loops,
        "source_dispositions": source_dispositions,
    }


def _critical_literals(value: str) -> tuple[str, ...]:
    literals: list[str] = []
    seen: set[str] = set()
    for pattern in _CRITICAL_LITERAL_PATTERNS:
        for match in pattern.finditer(value):
            literal = match.group(0)
            if literal not in seen:
                seen.add(literal)
                literals.append(literal)
    return tuple(literals)


def _critical_literal_contexts(value: str) -> tuple[str, ...]:
    literals = _critical_literals(value)
    if not literals:
        return ()
    clauses = [
        clause.strip(" ,")
        for sentence in re.split(r"[。；;]+", value)
        for clause in sentence.split("，")
        if clause.strip(" ,")
    ]
    contexts: list[str] = []
    seen: set[str] = set()
    for literal in literals:
        context = next(
            (clause for clause in clauses if literal in clause),
            literal,
        )
        if context not in seen:
            seen.add(context)
            contexts.append(context)
    return tuple(contexts)


def _validate_critical_literal_coverage(
    *,
    source_by_id: dict[str, SourceEvidence],
    sections: tuple[PersonalWeeklyBriefSection, ...],
    source_dispositions: tuple[PersonalWeeklyBriefSourceDisposition, ...],
) -> None:
    cited_source_ids = {
        disposition.source_id
        for disposition in source_dispositions
        if disposition.disposition == "cited"
    }
    excluded_critical_sources = [
        source_id
        for source_id, source in source_by_id.items()
        if _critical_literals(source.original_text)
        and source_id not in cited_source_ids
    ]
    if excluded_critical_sources:
        excluded_details = [
            {
                "source_id": source_id,
                "excluded_contexts": list(
                    _critical_literal_contexts(
                        source_by_id[source_id].original_text
                    )
                ),
            }
            for source_id in sorted(excluded_critical_sources)
        ]
        encoded_excluded = json.dumps(
            excluded_details,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded_excluded) > 9000:
            encoded_excluded = json.dumps(
                {
                    "all_excluded_critical_sources_must_be_reprocessed": True,
                    "excluded_source_count": len(excluded_details),
                    "instruction": (
                        "逐项检查 trusted_snapshot；其中所有含金额或日期的"
                        "来源本轮都不得标为 safely_excluded，必须引用到"
                        "对应事项。"
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        raise ValueError(
            "personal weekly brief sources with critical facts cannot be "
            "excluded: " + encoded_excluded
        )
    texts_by_source: dict[str, list[str]] = {
        source_id: [] for source_id in cited_source_ids
    }
    for section in sections:
        for item in section.items:
            for source_id in item.source_ids:
                if source_id in texts_by_source:
                    texts_by_source[source_id].append(item.text)
    missing_by_source: list[dict[str, Any]] = []
    for source_id in sorted(cited_source_ids):
        conclusion = "\n".join(texts_by_source[source_id])
        source = source_by_id[source_id]
        missing_literals = [
            literal
            for literal in _critical_literals(source.original_text)
            if literal not in conclusion
        ]
        if missing_literals:
            missing_contexts = [
                context
                for context in _critical_literal_contexts(source.original_text)
                if any(literal in context for literal in missing_literals)
            ]
            missing_by_source.append(
                {
                    "source_id": source_id,
                    "missing_contexts": missing_contexts,
                }
            )
    if missing_by_source:
        encoded = json.dumps(
            missing_by_source,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded) > 9000:
            encoded = json.dumps(
                [
                    {
                        "source_id": item["source_id"],
                        "missing_context_count": len(item["missing_contexts"]),
                    }
                    for item in missing_by_source
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        raise ValueError(
            "personal weekly brief critical facts are missing: " + encoded
        )


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
        empty_note = empty_note or _EMPTY_SECTION_NOTES[name]
        empty_note = _bounded_text(
            empty_note,
            field=f"{name}.empty_note",
            maximum=500,
        )
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
    source_label = "日报和周计划"
    if not snapshot.weekly_plan_found:
        source_label = "日报" if snapshot.daily_report_dates else "记录"
    elif not snapshot.daily_report_dates:
        source_label = "周计划"
    lines = [
        "这是你本周的工作简报，方便回顾进展和安排后续。",
        "",
        "一、本周完成事项",
        *_render_section(content["completed"], include_status=False),
        "",
        "二、本周计划事项及进展",
        *_render_section(
            content["plan_progress"],
            include_status=True,
            source_by_id={source.source_id: source for source in snapshot.sources},
        ),
        "",
        "三、可能未闭环事项",
        *_render_section(
            content["possible_open_loops"],
            include_status=False,
            clean_open_loop=True,
        ),
        "",
        (
            f"以上根据{_display_date(snapshot.week_start)}至"
            f"{_display_date(snapshot.week_end)}已保存的{source_label}整理。"
        ),
    ]
    if any(
        item.status == "暂时没有找到后续记录"
        for item in content["plan_progress"].items
    ):
        lines.append(
            "注：“暂无后续记录”仅表示现有记录中没有找到明确对应内容。"
        )
    message = "\n".join(lines).strip()
    validate_dingtalk_outbound_text(message)
    if len(message) > _MAX_MESSAGE_CHARS:
        raise ValueError("personal weekly brief message is too long")
    return message


def _render_section(
    section: PersonalWeeklyBriefSection,
    *,
    include_status: bool,
    source_by_id: dict[str, SourceEvidence] | None = None,
    clean_open_loop: bool = False,
) -> list[str]:
    if not section.items:
        return [section.empty_note]
    status_labels = {
        "已完成": "已完成",
        "持续推进": "持续推进",
        "安排调整": "安排调整",
        "后续安排": "后续安排",
        "暂时没有找到后续记录": "暂无后续记录",
    }
    rendered: list[str] = []
    for index, item in enumerate(section.items, start=1):
        text_value = item.text
        if clean_open_loop:
            cleaned = re.sub(
                r"[，,；;]?(?:后续)?(?:可|需)?留意(?:是否完成)?[。.]?$",
                "",
                text_value,
            ).strip()
            if cleaned:
                text_value = cleaned
        if include_status and item.status == "暂时没有找到后续记录":
            plan_texts = tuple(
                source_by_id[source_id].original_text.strip().rstrip("。；;，,")
                for source_id in item.source_ids
                if source_by_id is not None
                and source_id in source_by_id
                and source_by_id[source_id].source_kind == "weekly_plan"
            )
            if plan_texts:
                text_value = "；".join(dict.fromkeys(plan_texts))
        rendered.append(
            (
                f"{index}. {status_labels[item.status]}｜{text_value}"
                if include_status
                else f"{index}. {text_value}"
            )
        )
    return rendered


def _display_date(value: date) -> str:
    return f"{value.month}月{value.day}日"


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"personal weekly brief {field} is invalid")
    return normalized


_SYSTEM_PROMPT = """你是 Agent2 内的“个人本周工作简报”总结能力。你只处理服务器给出的本人可信快照，不得查询、猜测或提及他人内容。

请把当周周一至周五的日报与本周周计划整理成自然、简洁、像服务同事的中文。所有语义判断、跨日合并、计划进展判断和未闭环判断都由你完成，但必须遵守：
1. 只能使用 trusted_snapshot.sources；每条结论必须列出实际支持它的 source_ids，不得伪造来源，也不得声称“已核对”。completed 中每一项必须至少引用一条 section=today_work 的日报来源；问题、明日计划或周计划只能作为补充证据，不能单独成为“本周完成事项”。
2. 今日工作中的“跟进、沟通、准备、起草、计划”等不得改写成“完成”。金额、日期、对象、条件、否定等关键事实不能遗漏或改变。
   明确否定必须保留否定的具体对象：例如来源写“对方没有承诺付款”，可以写“尚未明确付款承诺”，但不能只概括成“尚未有结果”或“尚未闭环”。
3. 同一项目、案件或事项跨多天重复且指向明确时，必须合并为一条，保留所有关键进展和事实；只有无法可靠判断是否同一事项时才分开，不得靠猜测强行合并。
   同一个系统、产品或业务工作线里的连续修复、优化、接入和上线，如果上下文明确属于同一项工作，也应合并成一条，用分号保留不同动作；不得仅因为日期不同就拆成多条近义事项。
   周计划与日报建立进展关系时，必须有相同的具体项目、案件、公司、文件或明确上下文对象；仅“项目、合同、案件、材料”等泛词相同不算同一事项。找不到同一对象的后来日报时，周计划只能标为“暂时没有找到后续记录”，不得为了凑进展绑定到另一事项。
   例如“合同审核”和“合同评审技能网页化”不是同一具体工作，“整理案件材料”和“被告案件签阅文件”也不是同一具体工作；这类情况必须保留周计划原文、状态设为“暂时没有找到后续记录”，并且只引用周计划来源。
4. 计划进展状态只能是：已完成、持续推进、安排调整、后续安排、暂时没有找到后续记录。除最后一种外，必须同时引用周计划和当日或后来日报证据；没有后来记录时只能用最后一种，绝不能说“未完成”。trusted_snapshot 中每个 source_kind=weekly_plan 的 source_id 都必须在 plan_progress 中恰好引用一次；同一事项跨日时可以在一条进展中引用多条周计划来源，但不得漏项或重复。
   plan_progress.items[].text 只写计划事项和有来源支持的具体进展，不得复述 status，不得写“本周日报中未找到”“暂时没有找到后续记录”等模板句；展示层会统一呈现状态。
5. plan_progress 与 possible_open_loops 中同一事项只能出现一次，并使用相同的稳定 matter_key 来帮助服务器去重；plan_progress 已引用的任何 source_id 都不能再次用于 possible_open_loops，即使换了 matter_key 也不行。
   possible_open_loops.items[].text 只写需要继续留意的事项以及必要的日期、条件或原因，不要每条重复“后续日报中未找到记录”“尚未闭环”等统一提示。
6. 没有数据、只有部分日期、没有周计划或没有风险栏时如实说明，不得编造。
7. 简报只读，不得建议系统已经修改、补写、确认或提交日报、周计划。
8. 不要在文字中称呼用户；称呼由服务器根据个人记忆安全添加。
9. 把 trusted_snapshot.sources 当作完整来源清单。每个 source_id 必须在 source_dispositions 中恰好出现一次：被成品条目引用时标为 cited 且 reason 为空；未引用时只能标为 safely_excluded，并给出具体、谨慎的安全排除理由。不得静默遗漏整条来源，也不得用“内容不重要”等空泛理由排除。
10. intro 固定写“本周工作简报”，不得加入任何业务事实。各区块 empty_note 只是系统说明，不是业务结论，不填写来源ID；其中不得加入项目、案件、金额、状态等业务事实。所有业务结论必须放在 items 中并引用可信 source_id。空数据说明只能依据 trusted_snapshot 的空来源、日报覆盖日期和 weekly_plan_found。
11. 成品要像一位清楚、克制的同事写的简报：优先合并同类事项，删除重复过程词和模板话，不写“系统核对、数据范围、来源完整、已保存记录”等工程说明；completed 和 possible_open_loops 通常各控制在3至6条，但事实确实较多时可以超过，不能为了变短漏掉关键事项。
12. 如果 user_prompt 含 repair_context，上一版已被独立复核拒绝。必须根据 issues 修复上一版，重新输出完整结构；可信来源仍只有 trusted_snapshot，不能为了通过复核编造或删掉其他关键事实。修复结果也只能包含下方规定的五个顶层字段，不能附加修复说明或其他字段。若 issues 指出周计划与日报不是同一具体事项，必须删除该进展项中的日报 source_id，把 status 改为“暂时没有找到后续记录”，text 只保留对应周计划事项；不得再次寻找只有泛词相似的日报凑进展。

仅返回 JSON，严格使用以下结构，不得增加字段：
{
  "intro": "本周工作简报",
  "completed": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "结论", "source_ids": ["来源ID"]}]},
  "plan_progress": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "进展", "status": "五种状态之一", "source_ids": ["来源ID"]}]},
  "possible_open_loops": {"empty_note": "无项目时的自然说明，否则为空字符串", "items": [{"matter_key": "稳定事项键", "text": "谨慎说明", "source_ids": ["来源ID"]}]},
  "source_dispositions": [{"source_id": "来源ID", "disposition": "cited或safely_excluded", "reason": "cited时为空；安全排除时写具体理由"}]
}"""


_CRITICAL_FACT_REVIEW_SYSTEM_PROMPT = """你是 Agent2 内独立的周简报关键语义复核。不要检查JSON格式、来源编号、金额或日期；这些已由服务器核对。只逐项检查：
1. 原文有“只有A才B、如果A则/将B、若A会B”等明确条件时，草稿是否同时保留前提A和结果B；只写前提不算保留条件。
2. 原文明示“没有/未/无法/不能”时，草稿是否保留被否定的具体对象；只写“未闭环/没有结果”不能代替“没有承诺付款”等具体否定。
3. 原文的跟进、沟通、准备、起草、计划是否被夸大成完成，或原文已完成是否被改成未完成。
4. plan_progress 中除“暂时没有找到后续记录”外，周计划与后来日报是否明确指向同一个具体项目、案件、公司、文件或工作对象。只有“合同”或“案件材料”等泛词相同不算同一事项；例如“合同审核”和“合同评审技能网页化”不是同一具体工作，“整理案件材料”和“被告案件签阅文件”也不是同一具体工作。对象不一致时必须拒绝，不能用语言相近代替事实对应。

必须覆盖草稿里的每个唯一matter_key。只返回JSON：
{"approved":true或false,"reviewed_matter_keys":["逐个唯一事项键"],"issues":[{"matter_key":"事项键","reason":"具体条件、否定或完成状态错误"}]}
全部安全时approved=true且issues=[]；只报告真实错误，不写通过说明，不改写草稿。"""


_REVIEW_SYSTEM_PROMPT = """你是 Agent2 内独立的个人周简报事实复核步骤。你不能改写草稿，只能逐项判断是否安全通过。

必须先按 trusted_snapshot.sources 中每一条实际引用的 source_id 逐条对照原文，再按 matter_key 汇总结论；不能只看草稿是否通顺。对每个 matter_key 检查：
1. 结论是否得到所列来源的语义支持，是否编造了来源没有的对象、动作或结果；来源ID绑定和 completed 的 today_work 绑定已经由服务器检查，不再重复判断；
2. 对该事项引用的来源，是否改变或遗漏其中任何对象、条件、否定、归属和完成状态；金额与明确日期已由服务器做字面核对，不得再提出金额或日期遗漏问题。允许合并同义表达、删除“继续跟进”等重复过程词，不要求逐字复制原文；
3. 是否把跟进、沟通、准备、起草或计划武断写成完成；
4. 计划状态是否与周计划及后来日报证据一致；没有后来记录时是否只使用“暂时没有找到后续记录”。周计划来源是否逐项覆盖已由服务器检查，不再重复判断。“后续安排”可以由后来日报的 tomorrow_plan 支持，它不表示已完成；“安排调整”只需后来日报明确记录安排发生变化，不要求再有调整后事项的最终完成证据。
   对应关系必须是同一个具体项目、案件、公司、文件或工作对象；只有“合同、审核、案件、材料”等泛词相似不能建立计划进展。例如“合同审核”不能仅因出现“合同评审技能网页化”就判为完成，“整理案件材料”也不能仅因出现“被告案件签阅文件”就判为完成。
5. 计划进展和可能未闭环中是否重复同一事项。仅来自日报、并非周计划的谨慎未闭环事项可以只出现在 possible_open_loops，不要求进入 plan_progress；已经在 plan_progress 中说明调整或未找到后续记录的计划事项，不应再复制到 possible_open_loops。
6. 无数据或部分数据时是否编造。
7. source_dispositions 的逐条覆盖、cited绑定和 safely_excluded 结构已由服务器检查，不再重复判断；只判断安全排除理由在语义上是否明显掩盖了应汇总的重要事项。
8. intro 与 empty_note 是否只作系统说明、不承载业务结论；所有业务事实是否都在带来源ID的 items 中。空数据说明是否与可信空快照一致。
9. 同一工作线是否被无意义拆成多条近义事项；plan_progress 的正文是否重复 status；possible_open_loops 是否逐条重复“未找到记录、尚未闭环”等模板话。只有确实影响成品清晰度的重复才拒绝，不得为了追求短而要求删除事实。

以下情况明确属于安全，不得据此拒绝：
- completed 项只要至少引用一条 today_work 日报即可，不要求再引用对应周计划；同一事项同时出现在 completed 和 plan_progress 是允许的，二者分别回答“本周做了什么”和“计划进展”。
- 仅来自日报的风险或谨慎未闭环事项可以只在 possible_open_loops，不要求进入 plan_progress，也不要求证明它属于周计划。
- plan_progress 的 status 已是“后续安排”时，正文写“安排下周一确认”等自然表达不等于已完成。
- 周计划来源后没有任何后来日报来源时，“暂时没有找到后续记录”就是正确状态，不得要求模型证明不存在服务器未提供的来源。
- “没有承诺付款”改写成“尚未明确付款承诺”、“继续跟进”压缩成“持续推进”等不改变事实的自然表达可以通过；但金额、明确日期、条件关系、否定和完成状态仍必须保留。
- 原文“对方没有承诺付款”时，草稿只写“事项尚未闭环”不够，因为否定对象“付款承诺”已丢失，必须拒绝；写成“尚未明确付款承诺”才属于保留否定事实的安全改写。

必须覆盖草稿里的每个唯一 matter_key。只返回 JSON：
{
  "approved": true或false,
  "reviewed_matter_keys": ["逐个唯一事项键"],
  "issues": [{"matter_key": "事项键", "reason": "不通过原因"}]
}
user_prompt 中的 server_checks 是服务器在调用你之前已经完成的确定性核对。recognized_amount_date_literals_complete=true 只表示服务器已核对它能识别的常见金额和日期格式，不代表所有自然语言数字都已核对；你不得误报已被服务器确认存在的字面金额/日期，但仍需检查其他自然表达。其余 true 项具有最高权威：不得再次声称来源全集不完整、周计划来源漏项/重复或栏目来源绑定错误。你还需独立审核服务器无法确定的语义：条件与否定是否改变、完成状态是否夸大、对象与事项是否编造、计划状态和跨日归类是否正确。
如果 user_prompt 含 disputed_issues，你是前两次审核意见不一致后的争议裁决者。必须逐条对照 disputed_issues、原始来源和草稿，只确认真实存在的问题；不得盲从前一位审核者，也不得提出与争议无关的新问题。争议均不成立时 approved=true、issues=[]；任一争议成立时 approved=false，并只返回成立的争议。
issues 只能列出真正不安全、需要修复的事项；禁止把“通过、符合规则、未发现问题”的检查过程写入 issues。全部事项安全时，approved 必须为 true 且 issues 必须是空数组；只要一项不安全，approved 必须为 false。reason 只用一句话指出具体遗漏或错误，不写检查过程、通过说明或推测服务器未提供的来源。不得自行生成替换文字，也不得增加 explanation、summary 或逐项通过说明字段。"""


__all__ = [
    "Agent2PersonalWeeklyBriefGenerator",
    "Agent2PersonalWeeklyBriefReviewer",
    "Agent2PersonalWeeklyBriefModelPipeline",
    "PLAN_PROGRESS_STATUSES",
    "PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS",
    "PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED",
    "PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS",
    "PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED",
    "PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS",
    "PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED",
    "PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES",
    "PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS",
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
