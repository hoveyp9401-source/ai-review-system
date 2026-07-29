from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


class ImportBlocked(ValueError):
    pass


@dataclass(frozen=True)
class ImportErrorDetail:
    code: str
    message: str
    field: str = ""
    critical: bool = True


@dataclass(frozen=True)
class PreviewRow:
    row_number: int
    action: str
    raw: dict[str, Any]
    normalized: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    errors: tuple[ImportErrorDetail, ...] = ()


@dataclass(frozen=True)
class ImportPreview:
    rows: tuple[PreviewRow, ...]
    counts: dict[str, int]
    deletes: tuple[str, ...] = ()

    @property
    def publishable(self) -> bool:
        return not any(error.critical for row in self.rows for error in row.errors)

    def require_publishable(self) -> None:
        if not self.publishable:
            raise ImportBlocked("批次存在关键错误，不能发布")


class CaseMasterIndex:
    def __init__(self, cases: Iterable[dict[str, Any]]):
        self.cases = tuple(dict(case) for case in cases)
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_source: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_external_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for case in self.cases:
            case_id = _text(case.get("case_id"))
            if case_id:
                self.by_id[case_id] = case
            source_system = str(
                case.get("source_system")
                or case.get("source_type")
                or (case.get("source_json") or {}).get("source_system")
                or ""
            ).strip()
            source_case_id = str(
                case.get("source_case_id") or case.get("external_case_id") or ""
            ).strip()
            if source_system and source_case_id:
                self.by_source[(source_system.casefold(), source_case_id)] = case
            if source_case_id:
                self.by_external_id[source_case_id].append(case)
            number = _text(case.get("case_number"))
            if number:
                self.by_number[number].append(case)

    def find_by_source(
        self, source_system: str, source_case_id: str
    ) -> dict[str, Any] | None:
        return self.by_source.get((source_system.casefold(), source_case_id))


class CaseProgressIndex:
    def __init__(self, progresses: Iterable[dict[str, Any]]):
        self.progresses = tuple(dict(progress) for progress in progresses)
        self.by_external: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_fingerprint: dict[str, dict[str, Any]] = {}
        for progress in self.progresses:
            if progress.get("content_origin") != "imported_record":
                continue
            source_system = _text(progress.get("source_system"))
            external_id = _text(progress.get("external_progress_id"))
            if source_system and external_id:
                self.by_external[(source_system.casefold(), external_id)] = progress
            fingerprint = _text(progress.get("fingerprint"))
            if fingerprint:
                self.by_fingerprint[fingerprint] = progress


_MASTER_FIELDS = {
    "case_number",
    "case_name",
    "case_type",
    "status",
    "owner_user_id",
    "team_id",
    "company_id",
    "department_id",
}
_ERP_SOURCE_FIELDS = {
    "plaintiff",
    "defendant",
    "third_party",
    "our_litigation_position",
    "court",
    "amount",
    "filing_date",
    "erp_updated_at",
}
_SOURCE_LINEAGE_FIELDS = {
    "owner_display_name",
    "owner_link_status",
    "owner_source_stable_id",
    "owner_source_value",
    "source_profile_key",
    "source_profile_version",
    "source_sheet",
    "source_header_hash",
    "team_link_status",
    "team_source_value",
}
_LOCAL_SOURCE_FIELDS = {
    "next_plan",
    "internal_note",
    "custom_tags",
    "followup_config",
    "external_clues",
    "unverified_facts",
}


def preview_case_master(
    rows: list[dict[str, Any]],
    *,
    existing: CaseMasterIndex,
    import_mode: str,
    known_people: set[str],
    known_teams: set[str],
) -> ImportPreview:
    if import_mode not in {"full", "incremental"}:
        raise ImportBlocked("案件主表导入模式只能是全量或增量")
    identities = [
        (_text(row.get("source_system")).casefold(), _text(row.get("source_case_id")))
        for row in rows
    ]
    duplicate_identities = {
        identity
        for identity, count in Counter(identities).items()
        if all(identity) and count > 1
    }
    output: list[PreviewRow] = []
    present: set[tuple[str, str]] = set()
    for source in rows:
        row_number = int(source.get("__row_number__") or 0)
        normalized = _normalize_master(source)
        identity = (
            _text(normalized.get("source_system")).casefold(),
            _text(normalized.get("source_case_id")),
        )
        errors: list[ImportErrorDetail] = []
        for key, label in (
            ("source_system", "数据源"),
            ("source_case_id", "ERP案件ID"),
            ("case_name", "案件名称"),
            ("owner_user_id", "承办法务"),
            ("team_id", "所属团队"),
            ("status", "案件状态"),
        ):
            if not normalized.get(key):
                errors.append(ImportErrorDetail("required", f"{label}不能为空", key))
        if identity in duplicate_identities:
            errors.append(
                ImportErrorDetail(
                    "duplicate_source_case_id",
                    "同一来源案件ID在文件内重复",
                    "source_case_id",
                )
            )
        owner = _text(normalized.get("owner_user_id"))
        if owner and owner not in known_people:
            errors.append(
                ImportErrorDetail(
                    "person_not_found", "承办法务无法匹配", "owner_user_id"
                )
            )
        team = _text(normalized.get("team_id"))
        if team and team not in known_teams:
            errors.append(
                ImportErrorDetail("team_not_found", "所属团队无法匹配", "team_id")
            )
        before = existing.find_by_source(
            _text(normalized.get("source_system")),
            _text(normalized.get("source_case_id")),
        )
        after: dict[str, Any] | None = None
        action = "失败" if errors else "新增"
        if not errors:
            present.add(identity)
            after = _merge_case(before, normalized)
            if before is not None:
                action = "无变化" if _business_equal(before, after) else "更新"
        output.append(
            PreviewRow(
                row_number,
                action,
                dict(source.get("__raw_snapshot__") or source),
                normalized,
                before,
                after,
                tuple(errors),
            )
        )

    if import_mode == "full":
        source_systems = {identity[0] for identity in present}
        for case in existing.cases:
            identity = (
                _text(
                    case.get("source_system")
                    or case.get("source_type")
                    or (case.get("source_json") or {}).get("source_system")
                ).casefold(),
                _text(case.get("source_case_id") or case.get("external_case_id")),
            )
            if identity[0] in source_systems and identity not in present:
                output.append(
                    PreviewRow(
                        0,
                        "待核验",
                        {},
                        {},
                        case,
                        None,
                        (
                            ImportErrorDetail(
                                "missing_from_full_snapshot",
                                "本次全量来源文件未出现，保留原案件并等待人工核验",
                                critical=False,
                            ),
                        ),
                    )
                )
    keys = ("新增", "更新", "无变化", "冲突", "失败", "待核验")
    counts = {key: sum(row.action == key for row in output) for key in keys}
    return ImportPreview(tuple(output), counts)


def _normalize_master(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        key: _text(value)
        for key, value in row.items()
        if not key.startswith("__")
    }
    normalized["source_system"] = _text(row.get("source_system")).upper()
    normalized["source_case_id"] = _text(row.get("source_case_id"))
    return normalized


def _merge_case(
    before: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    output = dict(before or {})
    for field in _MASTER_FIELDS:
        if field in incoming:
            output[field] = incoming[field]
    output["source_system"] = incoming["source_system"]
    output["source_case_id"] = incoming["source_case_id"]
    output["external_case_id"] = incoming["source_case_id"]
    output["source_type"] = incoming["source_system"]
    source_json = dict((before or {}).get("source_json") or {})
    for field in _ERP_SOURCE_FIELDS:
        if field in incoming:
            source_json[field] = incoming[field]
    for field in _SOURCE_LINEAGE_FIELDS:
        if field in incoming:
            source_json[field] = incoming[field]
    for field in _LOCAL_SOURCE_FIELDS:
        if field in (before or {}).get("source_json", {}):
            source_json[field] = (before or {})["source_json"][field]
    source_json["source_system"] = incoming["source_system"]
    output["source_json"] = source_json
    return output


def _business_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = _MASTER_FIELDS | {"external_case_id", "source_type", "source_json"}
    return all(before.get(key) == after.get(key) for key in keys)


def preview_case_progress(
    rows: list[dict[str, Any]],
    *,
    cases: CaseMasterIndex,
    existing_progress: CaseProgressIndex,
    known_nodes: set[str],
    known_people: set[str],
) -> ImportPreview:
    output: list[PreviewRow] = []
    seen_identities: set[tuple[str, str]] = set()
    seen_fingerprints: set[str] = set()
    for source in rows:
        row_number = int(source.get("__row_number__") or 0)
        normalized = {
            key: _text(value)
            for key, value in source.items()
            if key not in {"__row_number__", "__raw_snapshot__"}
        }
        normalized["source_system"] = _text(source.get("source_system")).upper()
        errors: list[ImportErrorDetail] = []
        matched_case, match_error = _match_case(normalized, cases)
        if match_error:
            errors.append(match_error)
        occurred = _parse_date(normalized.get("progress_date"))
        if occurred is None:
            errors.append(
                ImportErrorDetail("invalid_date", "进展日期格式不正确", "progress_date")
            )
        content = _text(normalized.get("content"))
        if not content:
            errors.append(
                ImportErrorDetail("missing_content", "缺少进展内容", "content")
            )
        node = _text(normalized.get("procedure_node"))
        if node not in known_nodes:
            errors.append(
                ImportErrorDetail(
                    "unknown_procedure_node",
                    "程序节点未在现有节点中定义",
                    "procedure_node",
                )
            )
        reporter = _text(normalized.get("reporter_id"))
        if reporter and reporter not in known_people:
            errors.append(
                ImportErrorDetail("person_not_found", "录入人员无法匹配", "reporter_id")
            )
        if not reporter:
            errors.append(
                ImportErrorDetail("required", "录入人员不能为空", "reporter_id")
            )

        before: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        action = (
            "冲突"
            if match_error and match_error.code == "multiple_case_candidates"
            else "失败"
        )
        if not errors and matched_case is not None and occurred is not None:
            case_id = _text(matched_case.get("case_id"))
            external_id = _text(normalized.get("external_progress_id"))
            fingerprint = _progress_fingerprint(
                case_id,
                occurred.isoformat(),
                _text(normalized.get("progress_type")),
                content,
                normalized["source_system"],
            )
            normalized["case_id"] = case_id
            normalized["occurred_at"] = occurred.isoformat()
            normalized["fingerprint"] = fingerprint
            identity = (normalized["source_system"].casefold(), external_id)
            if external_id and identity in seen_identities:
                errors.append(
                    ImportErrorDetail(
                        "duplicate_progress",
                        "同一外部进展ID在文件内重复",
                        "external_progress_id",
                    )
                )
            elif not external_id and fingerprint in seen_fingerprints:
                errors.append(
                    ImportErrorDetail(
                        "duplicate_progress", "同一进展在文件内重复", "content"
                    )
                )
            if errors:
                action = "失败"
            else:
                if external_id:
                    seen_identities.add(identity)
                    before = existing_progress.by_external.get(identity)
                else:
                    seen_fingerprints.add(fingerprint)
                    before = existing_progress.by_fingerprint.get(fingerprint)
                after = _merge_progress(before, normalized)
                if before is None:
                    action = "新增"
                elif _progress_equal(before, after):
                    action = "重复跳过"
                else:
                    action = "更新"
        output.append(
            PreviewRow(
                row_number,
                action,
                dict(source.get("__raw_snapshot__") or source),
                normalized,
                before,
                after,
                tuple(errors),
            )
        )
    keys = ("新增", "更新", "重复跳过", "冲突", "失败")
    counts = {key: sum(row.action == key for row in output) for key in keys}
    return ImportPreview(tuple(output), counts)


def _match_case(
    row: dict[str, Any], cases: CaseMasterIndex
) -> tuple[dict[str, Any] | None, ImportErrorDetail | None]:
    manually_selected = _text(row.get("manual_case_id"))
    if manually_selected:
        case = cases.by_id.get(manually_selected)
        if case is not None:
            return case, None
        return None, ImportErrorDetail(
            "case_not_found",
            "人工选择的案件已不存在，请重新选择",
            "manual_case_id",
        )
    source_system = _text(row.get("source_system"))
    source_case_id = _text(row.get("source_case_id") or row.get("erp_case_id"))
    if source_system and source_case_id:
        case = cases.find_by_source(source_system, source_case_id)
        if case is not None:
            return case, None
    if source_case_id:
        external_candidates = cases.by_external_id.get(source_case_id, [])
        if len(external_candidates) == 1:
            return external_candidates[0], None
        if len(external_candidates) > 1:
            return None, ImportErrorDetail(
                "multiple_case_candidates",
                "ERP案件ID匹配到多个案件，必须人工处理",
                "source_case_id",
            )
    number = _text(row.get("case_number"))
    if number:
        candidates = cases.by_number.get(number, [])
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            return None, ImportErrorDetail(
                "multiple_case_candidates",
                "案号匹配到多个案件，必须人工处理",
                "case_number",
            )
    return None, ImportErrorDetail(
        "case_not_found", "无法通过稳定案件ID或唯一案号匹配案件", "source_case_id"
    )


def _merge_progress(
    before: dict[str, Any] | None, row: dict[str, Any]
) -> dict[str, Any]:
    output = dict(before or {})
    output.update(
        {
            "case_id": row["case_id"],
            "content_origin": "imported_record",
            "source_system": row["source_system"],
            "external_progress_id": _text(row.get("external_progress_id")),
            "fingerprint": row["fingerprint"],
            "occurred_at": row["occurred_at"],
            "progress_type": _text(row.get("progress_type")),
            "summary": _text(row.get("content")),
            "details": _text(row.get("content")),
            "procedure_node": _text(row.get("procedure_node")),
            "next_plan": _text(row.get("next_plan")),
            "plan_date": _text(row.get("plan_date")),
            "reporter_id": _text(row.get("reporter_id")),
        }
    )
    if before is not None:
        output["progress_id"] = before.get("progress_id")
        output["version"] = int(before.get("version") or 1) + (
            0 if _progress_equal(before, output) else 1
        )
    return output


def _progress_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = {
        "case_id",
        "content_origin",
        "source_system",
        "external_progress_id",
        "fingerprint",
        "occurred_at",
        "progress_type",
        "summary",
        "details",
        "procedure_node",
        "next_plan",
        "plan_date",
        "reporter_id",
    }
    return all(_text(before.get(key)) == _text(after.get(key)) for key in keys)


def _progress_fingerprint(
    case_id: str,
    progress_date: str,
    progress_type: str,
    content: str,
    source_system: str,
) -> str:
    payload = {
        "case_id": case_id,
        "date": progress_date,
        "type": _normalize_text(progress_type),
        "content": _normalize_text(content),
        "source": source_system.casefold(),
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip().casefold()


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            # The parsed value is a calendar date, so a timezone is intentionally absent.
            return datetime.strptime(text, pattern).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None
