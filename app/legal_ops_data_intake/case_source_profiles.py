from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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


class CaseSourceProfileError(FileValidationError):
    pass


@dataclass(frozen=True)
class SourceField:
    key: str
    source_header: str


@dataclass(frozen=True)
class CaseSourceProfile:
    key: str
    version: str
    label: str
    technical_source_system: str | None
    detection_headers: frozenset[str]
    fields: tuple[SourceField, ...]
    constants: tuple[tuple[str, Any], ...] = ()
    preserve_unlinked_owner: bool = False
    preserve_unlinked_team: bool = False

    @property
    def field_map(self) -> dict[str, str]:
        return {field.key: field.source_header for field in self.fields}


@dataclass(frozen=True)
class ParsedCaseSource:
    profile: CaseSourceProfile
    parsed: ParsedTable
    source_system: str
    header_hash: str

    def metadata(self) -> dict[str, Any]:
        return {
            "source_profile_key": self.profile.key,
            "source_profile_version": self.profile.version,
            "source_profile_label": self.profile.label,
            "detected_sheet": self.parsed.sheet_name,
            "detected_columns": len(self.parsed.headers),
            "header_hash": self.header_hash,
            "owner_link_policy": (
                "未匹配现有账号时保留来源负责人并标记待关联"
                if self.profile.preserve_unlinked_owner
                else "必须匹配现有人员稳定标识"
            ),
            "team_link_policy": (
                "未匹配现有团队时保留来源团队并标记待关联"
                if self.profile.preserve_unlinked_team
                else "必须匹配现有团队"
            ),
            "field_mapping": [
                {
                    "business_field": _COLUMNS_BY_KEY[item.key].name,
                    "source_header": item.source_header,
                }
                for item in self.profile.fields
            ],
        }


_CANONICAL_COLUMNS = (
    TableColumn("source_case_id", "ERP案件ID", "text", required=True, unique=True),
    TableColumn("case_name", "案件名称", "text", required=True),
    TableColumn("case_number", "案号", "text"),
    TableColumn("case_type", "案由", "text"),
    TableColumn("plaintiff", "原告", "text"),
    TableColumn("defendant", "被告", "text"),
    TableColumn("third_party", "第三人", "text"),
    TableColumn("our_litigation_position", "我方诉讼地位", "text"),
    TableColumn("owner_user_id", "承办法务", "text", required=True),
    TableColumn("team_id", "所属团队", "text", required=True),
    TableColumn("court", "法院/仲裁机构", "text"),
    TableColumn("amount", "涉案金额", "decimal"),
    TableColumn("filing_date", "立案/受理日期", "date"),
    TableColumn("status", "案件状态", "text", required=True),
    TableColumn("erp_updated_at", "ERP更新时间", "date"),
)
_COLUMNS_BY_KEY = {column.key: column for column in _CANONICAL_COLUMNS}


PLAINTIFF_CASE_MASTER = CaseSourceProfile(
    key="erp_plaintiff_case_master_v1",
    version="1",
    label="ERP原告案件底表",
    technical_source_system="ERP_PLAINTIFF_CASES",
    detection_headers=frozenset(
        {
            "法务部门",
            "诉讼仲裁编号",
            "案件名称",
            "承办法务",
            "案件状态",
        }
    ),
    fields=(
        SourceField("source_case_id", "诉讼仲裁编号"),
        SourceField("case_name", "案件名称"),
        SourceField("case_number", "诉讼或仲裁案号"),
        SourceField("case_type", "案由"),
        SourceField("defendant", "对方单位"),
        SourceField("owner_user_id", "承办法务"),
        SourceField("team_id", "法务部门"),
        SourceField("court", "受理机构全称"),
        SourceField("amount", "涉案金额"),
        SourceField("filing_date", "正式立案日期"),
        SourceField("status", "案件状态"),
    ),
    constants=(("our_litigation_position", "原告"),),
    preserve_unlinked_owner=True,
    preserve_unlinked_team=True,
)


DEFENDANT_CASE_MASTER = CaseSourceProfile(
    key="erp_defendant_case_master_v1",
    version="1",
    label="ERP被告案件底表",
    technical_source_system="ERP_DEFENDANT_CASES",
    detection_headers=frozenset(
        {
            "法务部门",
            "法务被告案件负责人",
            "案件编号",
            "案件名称",
            "被告案件状态名称",
        }
    ),
    fields=(
        SourceField("source_case_id", "案件编号"),
        SourceField("case_name", "案件名称"),
        SourceField("case_number", "被告案件受理案号"),
        SourceField("case_type", "受理案由"),
        SourceField("plaintiff", "原告姓名"),
        SourceField("our_litigation_position", "我司身份"),
        SourceField("owner_user_id", "法务被告案件负责人"),
        SourceField("team_id", "法务部门"),
        SourceField("court", "受理_机构名称"),
        SourceField("amount", "标的额"),
        SourceField("filing_date", "受理日期"),
        SourceField("status", "被告案件状态名称"),
    ),
    preserve_unlinked_owner=True,
    preserve_unlinked_team=True,
)


STANDARD_CASE_MASTER = CaseSourceProfile(
    key="legal_ops_standard_case_master_v1",
    version="1",
    label="标准案件接口表",
    technical_source_system=None,
    detection_headers=frozenset(
        {
            "ERP案件ID",
            "案件名称",
            "承办法务员工编号",
            "所属团队编号",
            "案件状态",
        }
    ),
    fields=(
        SourceField("source_case_id", "ERP案件ID"),
        SourceField("case_name", "案件名称"),
        SourceField("case_number", "案号"),
        SourceField("plaintiff", "原告"),
        SourceField("defendant", "被告"),
        SourceField("third_party", "第三人"),
        SourceField("our_litigation_position", "我方诉讼地位"),
        SourceField("owner_user_id", "承办法务员工编号"),
        SourceField("team_id", "所属团队编号"),
        SourceField("court", "法院"),
        SourceField("amount", "涉案金额"),
        SourceField("filing_date", "立案日期"),
        SourceField("status", "案件状态"),
        SourceField("erp_updated_at", "ERP更新时间"),
    ),
)


CASE_MASTER_PROFILES = (
    PLAINTIFF_CASE_MASTER,
    DEFENDANT_CASE_MASTER,
    STANDARD_CASE_MASTER,
)
_PROFILE_BY_KEY = {profile.key: profile for profile in CASE_MASTER_PROFILES}


def parse_case_master_source_file(
    content: bytes,
    filename: str,
    *,
    profile_key: str = "auto",
    source_system: str = "ERP",
    max_bytes: int = 25 * 1024 * 1024,
    max_rows: int = 100_000,
) -> ParsedCaseSource:
    safe_name, sheets = read_tabular_sheets(
        content,
        filename,
        max_bytes=max_bytes,
        max_rows=max_rows,
    )
    profile, sheet_name, raw_rows = _select_profile_and_sheet(sheets, profile_key)
    parsed = _parse_selected_rows(
        safe_name=safe_name,
        sheet_name=sheet_name,
        raw_rows=raw_rows,
        profile=profile,
    )
    technical_source = profile.technical_source_system or str(source_system or "").strip()
    if not technical_source:
        raise CaseSourceProfileError("标准案件接口表必须选择明确的数据源")
    header_hash = hashlib.sha256(
        json.dumps(
            parsed.headers,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ParsedCaseSource(
        profile=profile,
        parsed=parsed,
        source_system=technical_source,
        header_hash=header_hash,
    )


def _select_profile_and_sheet(
    sheets: dict[str, list[list[Any]]],
    profile_key: str,
) -> tuple[CaseSourceProfile, str, list[list[Any]]]:
    requested = str(profile_key or "auto").strip()
    if requested != "auto" and requested not in _PROFILE_BY_KEY:
        raise CaseSourceProfileError("所选接入方案不存在，请刷新页面后重试")
    profiles = CASE_MASTER_PROFILES if requested == "auto" else (_PROFILE_BY_KEY[requested],)
    matches: list[tuple[CaseSourceProfile, str, list[list[Any]]]] = []
    for sheet_name, rows in sheets.items():
        headers = set(_headers(rows))
        if not headers:
            continue
        for profile in profiles:
            if profile.detection_headers <= headers:
                matches.append((profile, sheet_name, rows))
    if not matches:
        expected = "、".join(profile.label for profile in profiles)
        raise CaseSourceProfileError(
            f"无法识别该文件。当前可直接接收：{expected}；"
            "请保留原表头，不要手工改列名。其他业务表需要先增加经过确认的接入方案。"
        )
    if len(matches) > 1:
        labels = "、".join(
            f"{profile.label}（工作表：{sheet_name or 'CSV'}）"
            for profile, sheet_name, _ in matches
        )
        raise CaseSourceProfileError(
            f"文件同时匹配多个数据表，无法安全自动选择：{labels}"
        )
    return matches[0]


def _headers(rows: list[list[Any]]) -> tuple[str, ...]:
    if not rows:
        return ()
    output = list(rows[0])
    while output and _is_blank(output[-1]):
        output.pop()
    if any(isinstance(value, FormulaCell) for value in output):
        return ()
    return tuple(str(value or "").strip() for value in output if not _is_blank(value))


def _parse_selected_rows(
    *,
    safe_name: str,
    sheet_name: str,
    raw_rows: list[list[Any]],
    profile: CaseSourceProfile,
) -> ParsedTable:
    if not raw_rows:
        raise CaseSourceProfileError("数据工作表没有表头")
    header_values = list(raw_rows[0])
    while header_values and _is_blank(header_values[-1]):
        header_values.pop()
    if any(isinstance(value, FormulaCell) for value in header_values):
        raise CaseSourceProfileError("数据工作表的表头不能使用公式")
    headers = tuple(str(value or "").strip() for value in header_values)
    if any(not header for header in headers) or len(headers) != len(set(headers)):
        raise CaseSourceProfileError("数据工作表存在空白或重复表头")
    if any(
        isinstance(value, FormulaCell)
        for values in raw_rows[1:]
        for value in values[: len(headers)]
    ):
        raise CaseSourceProfileError(
            f"数据工作表“{sheet_name or 'CSV'}”包含公式单元格，"
            "请上传公式计算后的值"
        )

    source_by_key = profile.field_map
    missing_required_headers = [
        source_by_key[column.key]
        for column in _CANONICAL_COLUMNS
        if column.required and source_by_key.get(column.key) not in headers
    ]
    if missing_required_headers:
        raise CaseSourceProfileError(
            f"接入方案要求的表头缺失：{'、'.join(missing_required_headers)}"
        )

    rows: list[ParsedRow] = []
    constants = dict(profile.constants)
    for row_number, values in enumerate(raw_rows[1:], start=2):
        padded = list(values) + [None] * max(0, len(headers) - len(values))
        if all(_is_blank(value) for value in padded[: len(headers)]):
            continue
        raw = {header: padded[index] for index, header in enumerate(headers)}
        normalized: dict[str, Any] = {}
        errors: list[RowError] = []
        for column in _CANONICAL_COLUMNS:
            if column.key in constants:
                normalized[column.key] = constants[column.key]
                continue
            source_header = source_by_key.get(column.key)
            value = raw.get(source_header) if source_header else None
            if _is_blank(value):
                normalized[column.key] = None
                if column.required:
                    errors.append(
                        RowError(
                            "required",
                            column.key,
                            f"{source_header or column.name}不能为空",
                        )
                    )
                continue
            try:
                normalized[column.key] = normalize_cell_value(value, column)
            except FileValidationError as exc:
                errors.append(
                    RowError(
                        _error_code(column.data_type),
                        column.key,
                        str(exc),
                    )
                )
        rows.append(
            ParsedRow(
                row_number=row_number,
                raw=raw,
                normalized=normalized,
                errors=errors,
            )
        )
    _add_duplicate_errors(rows)
    return ParsedTable(
        filename=safe_name,
        headers=headers,
        rows=tuple(rows),
        sheet_name=sheet_name,
    )


def _add_duplicate_errors(rows: list[ParsedRow]) -> None:
    seen: dict[str, ParsedRow] = {}
    for row in rows:
        value = str(row.normalized.get("source_case_id") or "").strip()
        if not value:
            continue
        if value not in seen:
            seen[value] = row
            continue
        message = "案件稳定ID在文件内重复"
        first = seen[value]
        if not any(error.code == "duplicate" for error in first.errors):
            first.errors.append(
                RowError("duplicate", "source_case_id", message)
            )
        row.errors.append(RowError("duplicate", "source_case_id", message))


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _error_code(data_type: str) -> str:
    return {
        "date": "invalid_date",
        "decimal": "invalid_decimal",
        "percentage": "invalid_percentage",
        "integer": "invalid_integer",
        "text": "invalid_text",
    }[data_type]
