from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ColumnMappingItem:
    field_key: str
    field_name: str
    source_header: str
    method: str
    source_position: int | None = None


@dataclass(frozen=True)
class ColumnMappingProposal:
    mapping: dict[str, str]
    items: tuple[ColumnMappingItem, ...]
    complete: bool
    requires_confirmation: bool


def propose_column_mapping(
    table: Mapping[str, Any],
    workbook_headers: Sequence[str],
    *,
    skill_markdown: str = "",
    saved_mapping: Mapping[str, str] | None = None,
) -> ColumnMappingProposal:
    """Map rule fields to an unchanged business workbook.

    Exact saved choices and explicit Skill column positions are authoritative.
    Anything that cannot be resolved uniquely remains visible for confirmation.
    """

    headers = tuple(str(value or "").strip() for value in workbook_headers)
    header_set = {value for value in headers if value}
    saved = {
        str(key): str(value).strip()
        for key, value in (saved_mapping or {}).items()
        if str(key).strip() and str(value).strip()
    }
    skill_positions = _skill_column_positions(
        skill_markdown,
        table.get("columns") or [],
    )
    used_headers: set[str] = set()
    items: list[ColumnMappingItem] = []
    mapping: dict[str, str] = {}

    for raw_column in table.get("columns") or []:
        if not isinstance(raw_column, Mapping):
            continue
        field_key = str(raw_column.get("key") or "").strip()
        field_name = str(raw_column.get("name") or field_key).strip()
        if not field_key:
            continue

        source_header = ""
        method = "unmapped"
        source_position: int | None = None

        saved_header = saved.get(field_key, "")
        if saved_header in header_set:
            source_header = saved_header
            method = "saved"
        else:
            exact_candidates = [
                candidate
                for candidate in _declared_headers(raw_column, field_name)
                if candidate in header_set
            ]
            exact_candidates = list(dict.fromkeys(exact_candidates))
            if len(exact_candidates) == 1:
                source_header = exact_candidates[0]
                method = (
                    "exact"
                    if source_header == field_name
                    else "declared_alias"
                )
            elif len(exact_candidates) > 1:
                method = "ambiguous"

        if not source_header and method != "ambiguous":
            source_position = _declared_position(raw_column)
            if source_position is None:
                source_position = skill_positions.get(field_key)
            if (
                source_position is not None
                and 1 <= source_position <= len(headers)
                and headers[source_position - 1]
            ):
                source_header = headers[source_position - 1]
                method = "skill_position"

        if not source_header and method != "ambiguous":
            normalized_name = _normalize_label(field_name)
            normalized_candidates = [
                header
                for header in headers
                if header and _normalize_label(header) == normalized_name
            ]
            if len(normalized_candidates) == 1:
                source_header = normalized_candidates[0]
                method = "normalized_exact"
            elif len(normalized_candidates) > 1:
                method = "ambiguous"

        if source_header and source_header in used_headers:
            source_header = ""
            method = "ambiguous"
        if source_header:
            mapping[field_key] = source_header
            used_headers.add(source_header)

        items.append(
            ColumnMappingItem(
                field_key=field_key,
                field_name=field_name,
                source_header=source_header,
                method=method,
                source_position=source_position,
            )
        )

    complete = bool(items) and all(item.source_header for item in items)
    requires_confirmation = any(
        item.method in {"unmapped", "ambiguous"} for item in items
    )
    return ColumnMappingProposal(
        mapping=mapping,
        items=tuple(items),
        complete=complete,
        requires_confirmation=requires_confirmation,
    )


def validate_column_mapping(
    table: Mapping[str, Any],
    workbook_headers: Sequence[str],
    mapping: Mapping[str, str],
) -> ColumnMappingProposal:
    """Validate a human-confirmed field mapping without guessing."""

    headers = tuple(str(value or "").strip() for value in workbook_headers)
    header_set = {value for value in headers if value}
    normalized_mapping = {
        str(key).strip(): str(value).strip()
        for key, value in mapping.items()
        if str(key).strip() and str(value).strip()
    }
    items: list[ColumnMappingItem] = []
    used_headers: set[str] = set()
    accepted: dict[str, str] = {}
    for raw_column in table.get("columns") or []:
        if not isinstance(raw_column, Mapping):
            continue
        field_key = str(raw_column.get("key") or "").strip()
        field_name = str(raw_column.get("name") or field_key).strip()
        source_header = normalized_mapping.get(field_key, "")
        method = "confirmed"
        if (
            not source_header
            or source_header not in header_set
            or source_header in used_headers
        ):
            source_header = ""
            method = "unmapped"
        else:
            accepted[field_key] = source_header
            used_headers.add(source_header)
        items.append(
            ColumnMappingItem(
                field_key=field_key,
                field_name=field_name,
                source_header=source_header,
                method=method,
            )
        )
    complete = bool(items) and all(item.source_header for item in items)
    return ColumnMappingProposal(
        mapping=accepted,
        items=tuple(items),
        complete=complete,
        requires_confirmation=not complete,
    )


def mapping_payload(proposal: ColumnMappingProposal) -> dict[str, Any]:
    return {
        "mapping": dict(proposal.mapping),
        "complete": proposal.complete,
        "requires_confirmation": proposal.requires_confirmation,
        "items": [
            {
                "field_key": item.field_key,
                "field_name": item.field_name,
                "source_header": item.source_header,
                "method": item.method,
                "method_label": {
                    "saved": "沿用上次确认",
                    "exact": "表头一致",
                    "declared_alias": "按规则别名识别",
                    "skill_position": "按 Skill 明确列位置识别",
                    "normalized_exact": "忽略空格和标点后一致",
                    "confirmed": "人工确认",
                    "ambiguous": "存在多个可能字段",
                    "unmapped": "需要确认",
                }.get(item.method, item.method),
                "source_position": item.source_position,
            }
            for item in proposal.items
        ],
    }


def _declared_headers(
    column: Mapping[str, Any],
    field_name: str,
) -> tuple[str, ...]:
    values: list[str] = [field_name]
    for key in ("source_header", "header"):
        value = str(column.get(key) or "").strip()
        if value:
            values.append(value)
    for key in ("source_headers", "aliases", "header_aliases"):
        raw_values = column.get(key) or []
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if isinstance(raw_values, Sequence):
            values.extend(
                str(value).strip()
                for value in raw_values
                if str(value).strip()
            )
    return tuple(values)


def _declared_position(column: Mapping[str, Any]) -> int | None:
    for key in ("source_position", "column_position", "position"):
        value = column.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
            if parsed > 0:
                return parsed
    return None


def _skill_column_positions(
    skill_markdown: str,
    columns: Sequence[Any],
) -> dict[str, int]:
    declared: list[tuple[int, str]] = []
    pattern = re.compile(
        r"(?:^|\s)(?:Col(?:umn)?\s*(?P<number>\d+)"
        r"(?:\([A-Za-z]{1,3}\))?|(?P<letter>[A-Za-z]{1,3})列)"
        r"\s*[:：]\s*(?P<label>.+)$",
        re.IGNORECASE,
    )
    for raw_line in str(skill_markdown or "").splitlines():
        match = pattern.search(raw_line.strip())
        if not match:
            continue
        if match.group("number"):
            position = int(match.group("number"))
        else:
            position = _excel_column_number(match.group("letter") or "")
        label = match.group("label").strip()
        if position > 0 and label:
            declared.append((position, label))

    output: dict[str, int] = {}
    for raw_column in columns:
        if not isinstance(raw_column, Mapping):
            continue
        field_key = str(raw_column.get("key") or "").strip()
        field_name = str(raw_column.get("name") or field_key).strip()
        normalized_name = _normalize_label(field_name)
        matches = {
            position
            for position, label in declared
            if normalized_name
            and normalized_name in _normalize_label(label)
        }
        if field_key and len(matches) == 1:
            output[field_key] = next(iter(matches))
    return output


def _normalize_label(value: str) -> str:
    text = re.sub(r"[`*_#]", "", str(value or "")).casefold()
    return re.sub(r"[\s\-_/\\:：,，。；;（）()\[\]【】]+", "", text)


def _excel_column_number(value: str) -> int:
    result = 0
    for character in str(value or "").strip().upper():
        if not ("A" <= character <= "Z"):
            return 0
        result = result * 26 + (ord(character) - ord("A") + 1)
    return result
