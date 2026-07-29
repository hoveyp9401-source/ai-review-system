from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    EvidenceRecord,
    MemberRecord,
    ReviewSuggestionRecord,
    TeamRecord,
    WorkItemEntryRecord,
    WorkItemRecord,
)

SYSTEM_PROMPT = """你负责理解多日法务日报，并只输出结构化的“建议复核”和事项时间线。

必须遵守：
1. 不评价员工是否认真，不输出个人分数、排名或态度标签。
2. 内容重复本身不能作为员工表现异常的结论。
3. “等待外部反馈”和“正常持续事项”必须与“没有新增进展”分开。
4. 每条建议必须引用 evidence_catalog 中真实存在的 evidence_id；不得自行复制、改写或拼接原文。
5. 只理解输入日报，不使用中文关键词名单、正则或单句特殊分支。
6. 管理者此前确认或排除的结果属于重要上下文，避免相同证据重复误报。
7. reason_type、status 和 owner_level 必须各自选择一个允许值，不能返回候选值清单。
8. 根据 output_contract 返回“实例数据”，不得复述结构定义、字段说明或候选值。
9. 只返回 JSON，不返回说明文字。
"""

ALLOWED_REASON_TYPES = {
    "missing_object_action_or_result",
    "no_new_progress",
    "plan_repeatedly_delayed",
    "unresolved_problem",
    "missing_section",
    "work_plan_disconnect",
    "too_general_to_assess",
    "disappeared_without_completion",
}

ALLOWED_WORK_ITEM_STATUSES = {
    "normal_progress",
    "no_new_progress",
    "plan_delayed",
    "unresolved_problem",
    "disappeared_without_completion",
    "completed",
    "waiting_external",
    "normal_continuing",
}


class JsonCompletionClient(Protocol):
    async def complete_json(self, **kwargs: object) -> str: ...


class AnalysisValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AnalysisResult:
    suggestions: tuple[ReviewSuggestionRecord, ...]
    work_items: tuple[WorkItemRecord, ...]


@dataclass(frozen=True)
class EvidenceCatalogEntry:
    evidence_id: str
    evidence_date: date
    section: str
    quote: str


class DirectReviewAnalyzer:
    """One direct model call from source reports to evidenced review output."""

    def __init__(
        self,
        *,
        client: JsonCompletionClient,
        model: str,
        timeout_seconds: float = 120.0,
        max_retries: int = 0,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    async def analyze(
        self,
        *,
        member: MemberRecord,
        team: TeamRecord,
        reports: tuple[DailyReportRecord, ...],
        previous_decisions: tuple[dict[str, object], ...] = (),
    ) -> AnalysisResult:
        if not reports:
            return AnalysisResult(suggestions=(), work_items=())
        ordered_reports = tuple(sorted(reports, key=lambda report: report.report_date))
        evidence_catalog = _build_evidence_catalog(ordered_reports)
        evidence_by_id = {entry.evidence_id: entry for entry in evidence_catalog}
        evidence_ids_by_date: dict[date, list[str]] = {}
        for entry in evidence_catalog:
            evidence_ids_by_date.setdefault(entry.evidence_date, []).append(
                entry.evidence_id
            )
        payload = {
            "member": {"name": member.name, "team": team.name},
            "reports": [
                {
                    "date": report.report_date.isoformat(),
                    "status": report.status,
                    "confirmation_type": report.confirmation_type,
                    "confirmed_by_user": report.confirmed_by_user,
                    "section_presence": {
                        "今日工作": bool(report.today_work),
                        "问题风险": bool(report.problems),
                        "明日计划": bool(report.tomorrow_plan),
                        "员工原文": bool(report.raw_input.strip()),
                    },
                    "evidence_ids": evidence_ids_by_date.get(
                        report.report_date,
                        [],
                    ),
                }
                for report in ordered_reports
            ],
            "evidence_catalog": [
                {
                    "evidence_id": entry.evidence_id,
                    "date": entry.evidence_date.isoformat(),
                    "section": entry.section,
                    "quote": entry.quote,
                }
                for entry in evidence_catalog
            ],
            "previous_manager_decisions": list(previous_decisions),
            "output_contract": _output_contract(),
        }
        raw = await self._client.complete_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            model=self._model,
            thinking_enabled=True,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AnalysisValidationError("model output is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise AnalysisValidationError("model output must be an object")
        suggestions = tuple(
            self._parse_suggestion(
                item=item,
                member=member,
                team=team,
                evidence_by_id=evidence_by_id,
            )
            for item in _object_list(parsed.get("review_suggestions"))
            if item.get("suggest_review") is not False
        )
        work_items = tuple(
            self._parse_work_item(
                item=item,
                member=member,
                team=team,
                evidence_by_id=evidence_by_id,
            )
            for item in _object_list(parsed.get("work_items"))
        )
        return AnalysisResult(
            suggestions=suggestions,
            work_items=work_items,
        )

    def _parse_suggestion(
        self,
        *,
        item: dict[str, Any],
        member: MemberRecord,
        team: TeamRecord,
        evidence_by_id: dict[str, EvidenceCatalogEntry],
    ) -> ReviewSuggestionRecord:
        reason_type = _required_text(item, "reason_type")
        if reason_type not in ALLOWED_REASON_TYPES:
            raise AnalysisValidationError("unsupported review reason type")
        evidence_entries = _catalog_entries_from_ids(
            item.get("evidence_ids"),
            evidence_by_id=evidence_by_id,
        )
        evidence = _evidence_records(evidence_entries)
        if not evidence:
            raise AnalysisValidationError("review suggestion must include evidence")
        comparison_value = item.get("comparison_evidence_ids")
        comparison_entries = (
            evidence_entries
            if comparison_value is None
            else _catalog_entries_from_ids(
                comparison_value,
                evidence_by_id=evidence_by_id,
            )
        )
        compared_dates = tuple(
            sorted(
                {
                    entry.evidence_date
                    for entry in (*evidence_entries, *comparison_entries)
                }
            )
        )
        owner_level = _required_text(item, "owner_level")
        if owner_level not in {"team_lead", "legal_head"}:
            raise AnalysisValidationError("unsupported owner level")
        work_item_title = _required_text(item, "work_item_title")
        reason = _required_text(item, "reason")
        support_needed = _required_text(item, "support_needed")
        confidence = _confidence(item.get("confidence"))
        suggestion_ref = _content_ref(
            "suggestion",
            {
                "member_ref": member.ref,
                "team_ref": team.ref,
                "reason_type": reason_type,
                "reason": reason,
                "evidence": [
                    {
                        "date": value.evidence_date.isoformat(),
                        "section": value.section,
                        "quote": value.quote,
                    }
                    for value in evidence
                ],
                "compared_dates": [value.isoformat() for value in compared_dates],
                "confidence": confidence,
                "work_item_title": work_item_title,
                "support_needed": support_needed,
                "owner_level": owner_level,
                "model_version": self._model,
            },
        )
        return ReviewSuggestionRecord(
            ref=suggestion_ref,
            member_ref=member.ref,
            team_ref=team.ref,
            report_date=max(compared_dates),
            reason_type=reason_type,
            reason=reason,
            evidence=evidence,
            compared_dates=compared_dates,
            confidence=confidence,
            work_item_title=work_item_title,
            support_needed=support_needed,
            owner_level=owner_level,  # type: ignore[arg-type]
            model_version=self._model,
        )

    def _parse_work_item(
        self,
        *,
        item: dict[str, Any],
        member: MemberRecord,
        team: TeamRecord,
        evidence_by_id: dict[str, EvidenceCatalogEntry],
    ) -> WorkItemRecord:
        status = _required_text(item, "status")
        if status not in ALLOWED_WORK_ITEM_STATUSES:
            raise AnalysisValidationError("unsupported work item status")
        title = _required_text(item, "title")
        entries = tuple(
            sorted(
                (
                    _parse_work_item_entry(
                        value,
                        member_ref=member.ref,
                        evidence_by_id=evidence_by_id,
                    )
                    for value in _object_list(item.get("entries"))
                ),
                key=lambda value: (
                    value.entry_date,
                    value.section,
                    value.quote,
                ),
            )
        )
        if not entries:
            raise AnalysisValidationError("work item must include entries")
        entry_dates = tuple(value.entry_date for value in entries)
        first_seen = min(entry_dates)
        last_seen = max(entry_dates)
        summary = _required_text(item, "summary")
        confidence = _confidence(item.get("confidence"))
        item_ref = _content_ref(
            "item",
            {
                "member_ref": member.ref,
                "team_ref": team.ref,
                "title": title,
                "status": status,
                "summary": summary,
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "entries": [
                    {
                        "date": value.entry_date.isoformat(),
                        "section": value.section,
                        "quote": value.quote,
                        "object": value.object_text,
                        "action": value.action,
                        "result": value.result,
                        "next_step": value.next_step,
                        "blocker": value.blocker,
                    }
                    for value in entries
                ],
                "confidence": confidence,
                "model_version": self._model,
            },
        )
        return WorkItemRecord(
            ref=item_ref,
            team_ref=team.ref,
            member_refs=(member.ref,),
            title=title,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            first_seen=first_seen,
            last_seen=last_seen,
            entries=entries,
            confidence=confidence,
            model_version=self._model,
        )


def _build_evidence_catalog(
    reports: tuple[DailyReportRecord, ...],
) -> tuple[EvidenceCatalogEntry, ...]:
    rows: list[tuple[date, str, str]] = []
    seen: set[tuple[date, str, str]] = set()
    for report in reports:
        sections: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("今日工作", report.today_work),
            ("问题风险", report.problems),
            ("明日计划", report.tomorrow_plan),
            (
                "员工原文",
                (report.raw_input,) if report.raw_input.strip() else (),
            ),
        )
        for section, values in sections:
            for value in values:
                quote = str(value).strip()
                key = (report.report_date, section, quote)
                if not quote or key in seen:
                    continue
                seen.add(key)
                rows.append(key)
    return tuple(
        EvidenceCatalogEntry(
            evidence_id=f"E{index:03d}",
            evidence_date=evidence_date,
            section=section,
            quote=quote,
        )
        for index, (evidence_date, section, quote) in enumerate(rows, start=1)
    )


def _catalog_entries_from_ids(
    value: object,
    *,
    evidence_by_id: dict[str, EvidenceCatalogEntry],
) -> tuple[EvidenceCatalogEntry, ...]:
    evidence_ids = _string_list(value)
    entries: list[EvidenceCatalogEntry] = []
    seen: set[str] = set()
    for evidence_id in evidence_ids:
        entry = evidence_by_id.get(evidence_id)
        if entry is None:
            raise AnalysisValidationError(
                "evidence id does not exist in the supplied catalog"
            )
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        entries.append(entry)
    return tuple(entries)


def _evidence_records(
    entries: tuple[EvidenceCatalogEntry, ...],
) -> tuple[EvidenceRecord, ...]:
    return tuple(
        EvidenceRecord(
            evidence_date=entry.evidence_date,
            section=entry.section,
            quote=entry.quote,
        )
        for entry in sorted(
            entries,
            key=lambda value: (
                value.evidence_date,
                value.section,
                value.quote,
            ),
        )
    )


def _parse_work_item_entry(
    item: dict[str, Any],
    *,
    member_ref: str,
    evidence_by_id: dict[str, EvidenceCatalogEntry],
) -> WorkItemEntryRecord:
    evidence_id = _required_text(item, "evidence_id")
    evidence = evidence_by_id.get(evidence_id)
    if evidence is None:
        raise AnalysisValidationError(
            "evidence id does not exist in the supplied catalog"
        )
    return WorkItemEntryRecord(
        entry_date=evidence.evidence_date,
        member_ref=member_ref,
        section=evidence.section,
        quote=evidence.quote,
        object_text=_required_text(item, "object"),
        action=_required_text(item, "action"),
        result=str(item.get("result") or "").strip(),
        next_step=str(item.get("next_step") or "").strip(),
        blocker=str(item.get("blocker") or "").strip(),
    )


def _output_contract() -> dict[str, object]:
    non_empty_string = {"type": "string", "minLength": 1}
    evidence_id_array = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "string",
            "description": "必须是 evidence_catalog 中的 evidence_id",
        },
    }
    work_entry = {
        "type": "object",
        "additionalProperties": False,
        "required": ["evidence_id", "object", "action"],
        "properties": {
            "evidence_id": {
                "type": "string",
                "description": "必须是 evidence_catalog 中的一个 evidence_id",
            },
            "object": non_empty_string,
            "action": non_empty_string,
            "result": {"type": "string"},
            "next_step": {"type": "string"},
            "blocker": {"type": "string"},
        },
    }
    suggestion = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "reason_type",
            "reason",
            "evidence_ids",
            "comparison_evidence_ids",
            "confidence",
            "work_item_title",
            "support_needed",
            "owner_level",
        ],
        "properties": {
            "reason_type": {
                "type": "string",
                "enum": sorted(ALLOWED_REASON_TYPES),
            },
            "reason": non_empty_string,
            "evidence_ids": evidence_id_array,
            "comparison_evidence_ids": evidence_id_array,
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "work_item_title": non_empty_string,
            "support_needed": non_empty_string,
            "owner_level": {
                "type": "string",
                "enum": ["team_lead", "legal_head"],
            },
        },
    }
    work_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "status", "summary", "confidence", "entries"],
        "properties": {
            "title": non_empty_string,
            "status": {
                "type": "string",
                "enum": sorted(ALLOWED_WORK_ITEM_STATUSES),
            },
            "summary": non_empty_string,
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "entries": {
                "type": "array",
                "minItems": 1,
                "items": work_entry,
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["review_suggestions", "work_items"],
        "properties": {
            "review_suggestions": {
                "type": "array",
                "description": "只放需要建议复核的项目；没有则返回空数组",
                "items": suggestion,
            },
            "work_items": {
                "type": "array",
                "description": "从多日日报中识别出的事项时间线",
                "items": work_item,
            },
        },
    }


def _object_list(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AnalysisValidationError("expected a list of objects")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AnalysisValidationError("expected a list of strings")
    return [item.strip() for item in value]


def _required_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisValidationError(f"{key} must be non-empty text")
    return value.strip()


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise AnalysisValidationError("date must be ISO text")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisValidationError("invalid ISO date") from exc


def _confidence(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise AnalysisValidationError("confidence must be numeric")
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise AnalysisValidationError("confidence must be between 0 and 1")
    return parsed


def _stable_ref(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _content_ref(prefix: str, payload: dict[str, Any]) -> str:
    return _stable_ref(
        prefix,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
