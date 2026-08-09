from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.legal_ops_data_intake.workbook import (
    FileValidationError,
    FormulaCell,
    ParsedRow,
    ParsedTable,
    RowError,
    TableColumn,
    normalize_cell_value,
    read_tabular_sheets,
)


class CaseProgressSourceProfileError(FileValidationError):
    pass


@dataclass(frozen=True)
class CaseProgressSourceProfile:
    key: str
    version: str
    label: str
    source_system: str
    detection_headers: frozenset[str]
    case_id_header: str
    case_number_header: str
    status_header: str
    content_headers: tuple[str, ...]
    plan_headers: tuple[str, ...]


@dataclass(frozen=True)
class ParsedCaseProgressSource:
    profile: CaseProgressSourceProfile
    parsed: ParsedTable
    header_hash: str
    ignored_empty_rows: int

    @property
    def source_system(self) -> str:
        return self.profile.source_system

    def metadata(self) -> dict[str, Any]:
        return {
            "source_profile_key": self.profile.key,
            "source_profile_version": self.profile.version,
            "source_profile_label": self.profile.label,
            "detected_sheet": self.parsed.sheet_name,
            "detected_columns": len(self.parsed.headers),
            "header_hash": self.header_hash,
            "ignored_empty_progress_rows": self.ignored_empty_rows,
            "field_mapping": [
                {"business_field": "ERP案件ID", "source_header": self.profile.case_id_header},
                {"business_field": "案号", "source_header": self.profile.case_number_header},
                {"business_field": "程序节点候选", "source_header": self.profile.status_header},
                {
                    "business_field": "进展内容",
                    "source_header": "、".join(self.profile.content_headers),
                },
                {
                    "business_field": "下一步计划",
                    "source_header": "、".join(self.profile.plan_headers),
                },
            ],
        }


PLAINTIFF_PROGRESS_SNAPSHOT = CaseProgressSourceProfile(
    key="erp_plaintiff_progress_snapshot_v1",
    version="1",
    label="ERP原告案件进展快照",
    source_system="ERP_PLAINTIFF_CASES",
    detection_headers=frozenset(
        {
            "诉讼仲裁编号",
            "案件状态",
            "承办法务",
            "本月中旬计划",
            "本月中旬计划完成情况",
        }
    ),
    case_id_header="诉讼仲裁编号",
    case_number_header="诉讼或仲裁案号",
    status_header="案件状态",
    content_headers=(
        "调解情况进展",
        "本月中旬计划完成情况",
        "本月下旬计划完成情况",
        "外部资源协作进展跟踪",
        "终本后跟踪财产查控情况",
        "保全结果",
    ),
    plan_headers=("本月中旬计划", "本月下旬计划"),
)


DEFENDANT_PROGRESS_SNAPSHOT = CaseProgressSourceProfile(
    key="erp_defendant_progress_snapshot_v1",
    version="1",
    label="ERP被告案件进展快照",
    source_system="ERP_DEFENDANT_CASES",
    detection_headers=frozenset(
        {
            "案件编号",
            "被告案件状态名称",
            "法务被告案件负责人",
            "最新开庭进展",
            "本月月度计划",
        }
    ),
    case_id_header="案件编号",
    case_number_header="被告案件受理案号",
    status_header="被告案件状态名称",
    content_headers=(
        "最新开庭进展",
        "本月上旬计划完成情况",
        "本月月度计划完成情况",
    ),
    plan_headers=("本月上旬计划", "本月中旬计划", "本月月度计划"),
)


CASE_PROGRESS_SOURCE_PROFILES = (
    PLAINTIFF_PROGRESS_SNAPSHOT,
    DEFENDANT_PROGRESS_SNAPSHOT,
)
_PROFILE_BY_KEY = {profile.key: profile for profile in CASE_PROGRESS_SOURCE_PROFILES}
_TEXT = TableColumn("value", "内容", "text")
_DATE = TableColumn("value", "日期", "date")


def parse_case_progress_source_file(
    content: bytes,
    filename: str,
    *,
    profile_key: str = "auto",
    snapshot_date: str,
    reporter_id: str,
    max_bytes: int = 25 * 1024 * 1024,
    max_rows: int = 100_000,
) -> ParsedCaseProgressSource:
    safe_name, sheets = read_tabular_sheets(
        content,
        filename,
        max_bytes=max_bytes,
        max_rows=max_rows,
    )
    profile, sheet_name, raw_rows = _select_profile_and_sheet(sheets, profile_key)
    parsed_date = normalize_cell_value(snapshot_date, _DATE)
    reporter = str(reporter_id or "").strip()
    if not reporter:
        raise CaseProgressSourceProfileError("请选择本次文件的录入人员")
    parsed, ignored = _parse_rows(
        safe_name=safe_name,
        sheet_name=sheet_name,
        raw_rows=raw_rows,
        profile=profile,
        snapshot_date=parsed_date,
        reporter_id=reporter,
    )
    header_hash = hashlib.sha256(
        json.dumps(parsed.headers, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return ParsedCaseProgressSource(profile, parsed, header_hash, ignored)


def _select_profile_and_sheet(
    sheets: dict[str, list[list[Any]]],
    profile_key: str,
) -> tuple[CaseProgressSourceProfile, str, list[list[Any]]]:
    requested = str(profile_key or "auto").strip()
    if requested != "auto" and requested not in _PROFILE_BY_KEY:
        raise CaseProgressSourceProfileError("所选案件进展接入方案不存在")
    profiles = (
        CASE_PROGRESS_SOURCE_PROFILES
        if requested == "auto"
        else (_PROFILE_BY_KEY[requested],)
    )
    matches: list[tuple[CaseProgressSourceProfile, str, list[list[Any]]]] = []
    for sheet_name, rows in sheets.items():
        headers = set(_headers(rows))
        for profile in profiles:
            if headers and profile.detection_headers <= headers:
                matches.append((profile, sheet_name, rows))
    if not matches:
        expected = "、".join(profile.label for profile in profiles)
        raise CaseProgressSourceProfileError(
            f"无法识别该文件。当前可直接接收：{expected}；请保留原表头。"
        )
    if len(matches) > 1:
        raise CaseProgressSourceProfileError(
            "文件同时匹配多个案件进展数据表，请明确选择表格类型"
        )
    return matches[0]


def _headers(rows: list[list[Any]]) -> tuple[str, ...]:
    if not rows:
        return ()
    values = list(rows[0])
    while values and _blank(values[-1]):
        values.pop()
    if any(isinstance(value, FormulaCell) for value in values):
        return ()
    return tuple(str(value or "").strip() for value in values if not _blank(value))


def _parse_rows(
    *,
    safe_name: str,
    sheet_name: str,
    raw_rows: list[list[Any]],
    profile: CaseProgressSourceProfile,
    snapshot_date: date,
    reporter_id: str,
) -> tuple[ParsedTable, int]:
    if not raw_rows:
        raise CaseProgressSourceProfileError("数据工作表没有表头")
    header_values = list(raw_rows[0])
    while header_values and _blank(header_values[-1]):
        header_values.pop()
    headers = tuple(str(value or "").strip() for value in header_values)
    if any(not header for header in headers) or len(headers) != len(set(headers)):
        raise CaseProgressSourceProfileError("数据工作表存在空白或重复表头")
    required_headers = {
        profile.case_id_header,
        profile.status_header,
        *profile.content_headers,
        *profile.plan_headers,
    }
    missing = sorted(required_headers.difference(headers))
    if missing:
        raise CaseProgressSourceProfileError(
            f"接入方案要求的表头缺失：{'、'.join(missing)}"
        )

    rows: list[ParsedRow] = []
    ignored = 0
    selected_headers = required_headers | {profile.case_number_header}
    for row_number, values in enumerate(raw_rows[1:], start=2):
        padded = list(values) + [None] * max(0, len(headers) - len(values))
        if all(_blank(value) for value in padded[: len(headers)]):
            continue
        raw = {header: padded[index] for index, header in enumerate(headers)}
        if any(
            isinstance(raw.get(header), FormulaCell) for header in selected_headers
        ):
            rows.append(
                ParsedRow(
                    row_number,
                    raw,
                    {},
                    [
                        RowError(
                            "formula_not_allowed",
                            "file",
                            "案件进展所需字段包含公式，请上传公式计算后的值",
                        )
                    ],
                )
            )
            continue
        progress_parts = _labeled_values(raw, profile.content_headers)
        plan_parts = _labeled_values(raw, profile.plan_headers)
        if not progress_parts and not plan_parts:
            ignored += 1
            continue
        source_case_id = _text(raw.get(profile.case_id_header))
        content = "\n".join(progress_parts)
        if not content and plan_parts:
            content = "本次仅更新下一步计划"
        errors: list[RowError] = []
        if not source_case_id:
            errors.append(
                RowError("required", "source_case_id", f"{profile.case_id_header}不能为空")
            )
        normalized = {
            "source_case_id": source_case_id,
            "case_number": _text(raw.get(profile.case_number_header)),
            "external_progress_id": (
                f"snapshot:{profile.key}:{snapshot_date.isoformat()}:{source_case_id}"
                if source_case_id
                else ""
            ),
            "progress_date": snapshot_date,
            "progress_type": "ERP案件进展快照",
            "content": content,
            "procedure_node": _text(raw.get(profile.status_header)),
            "next_plan": "\n".join(plan_parts),
            "plan_date": None,
            "reporter_id": reporter_id,
            "source_updated_at": snapshot_date,
            "source_profile_key": profile.key,
            "source_profile_version": profile.version,
            "source_sheet": sheet_name,
        }
        rows.append(ParsedRow(row_number, raw, normalized, errors))
    return (
        ParsedTable(
            filename=safe_name,
            headers=headers,
            rows=tuple(rows),
            sheet_name=sheet_name,
        ),
        ignored,
    )


def _labeled_values(raw: dict[str, Any], headers: tuple[str, ...]) -> list[str]:
    values = []
    for header in headers:
        value = _text(raw.get(header))
        if value:
            values.append(f"{header}：{value}")
    return values


def _text(value: Any) -> str:
    if isinstance(value, FormulaCell) or value is None:
        return ""
    return str(normalize_cell_value(value, _TEXT) or "").strip()


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
