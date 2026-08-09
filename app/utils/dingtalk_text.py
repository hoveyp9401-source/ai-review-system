from __future__ import annotations

import re


_HORIZONTAL_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_HEADING = re.compile(r"(^|[，,。.!！?？;；:：]\s*)#{1,6}\s*")
_LEADING_BULLET = re.compile(r"^(\s*)[-+*]\s+")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\(([^)]+)\)")


def format_dingtalk_plain_text(value: str) -> str:
    """Turn common Markdown into readable DingTalk ``text`` message layout."""

    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        headers = _table_headers(lines, index)
        if headers is not None:
            rows, next_index = _table_rows(lines, index + 2)
            if rows:
                _append_block(rendered, _render_table(headers, rows))
                index = next_index
                continue

        line = lines[index]
        if _HORIZONTAL_RULE.fullmatch(line):
            _append_blank(rendered)
            index += 1
            continue

        _append_line(rendered, _clean_text_line(line))
        index += 1

    while rendered and not rendered[-1]:
        rendered.pop()
    return "\n".join(rendered)


def _table_headers(lines: list[str], index: int) -> list[str] | None:
    if index + 1 >= len(lines):
        return None
    headers = _split_table_row(lines[index])
    separators = _split_table_row(lines[index + 1])
    if (
        headers is None
        or separators is None
        or len(headers) != len(separators)
        or not all(_TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in separators)
    ):
        return None
    return [_clean_inline(cell) for cell in headers]


def _table_rows(lines: list[str], index: int) -> tuple[list[list[str]], int]:
    rows: list[list[str]] = []
    while index < len(lines):
        row = _split_table_row(lines[index])
        if row is None:
            break
        rows.append([_clean_inline(cell) for cell in row])
        index += 1
    return rows, index


def _split_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [cell.strip() for cell in stripped.split("|")]
    return cells if len(cells) >= 2 else None


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells = row + [""] * max(0, len(headers) - len(row))
        first_value = cells[0] if cells else ""
        first_label = headers[0] if headers else "事项"
        headline = first_value or f"{first_label}（未填写）"
        output.append(f"{row_number}. {headline}")
        for column_number, value in enumerate(cells[1:], start=1):
            label = (
                headers[column_number]
                if column_number < len(headers) and headers[column_number]
                else f"字段{column_number + 1}"
            )
            output.append(f"   {label}：{value or '—'}")
        if row_number < len(rows):
            output.append("")
    return output


def _clean_text_line(line: str) -> str:
    cleaned = _HEADING.sub(r"\1", line.rstrip())
    cleaned = _LEADING_BULLET.sub(r"\1• ", cleaned)
    if cleaned.lstrip().startswith("> "):
        indentation = cleaned[: len(cleaned) - len(cleaned.lstrip())]
        cleaned = indentation + cleaned.lstrip()[2:]
    return _clean_inline(cleaned)


def _clean_inline(value: str) -> str:
    cleaned = _MARKDOWN_LINK.sub(r"\1（\2）", value)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    return cleaned.strip() if cleaned.strip() else ""


def _append_block(target: list[str], block: list[str]) -> None:
    if target and target[-1]:
        target.append("")
    for line in block:
        _append_line(target, line)
    _append_blank(target)


def _append_line(target: list[str], line: str) -> None:
    if line:
        target.append(line)
    else:
        _append_blank(target)


def _append_blank(target: list[str]) -> None:
    if target and target[-1]:
        target.append("")
