from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


_DAILY_SECTION_LABELS = {
    "今日完成": "today_work",
    "今日工作": "today_work",
    "今天完成": "today_work",
    "今天工作": "today_work",
    "当日完成": "today_work",
    "当日工作": "today_work",
    "问题与风险": "problems",
    "风险与问题": "problems",
    "问题和风险": "problems",
    "风险和问题": "problems",
    "问题/风险": "problems",
    "风险/问题": "problems",
    "明日计划": "tomorrow_plan",
    "明天计划": "tomorrow_plan",
    "次日计划": "tomorrow_plan",
}
_LABEL_PATTERN = "|".join(
    re.escape(value)
    for value in sorted(_DAILY_SECTION_LABELS, key=len, reverse=True)
)
_SECTION_HEADER = re.compile(
    rf"(?m)(?:^|\n)[ \t]*(?:【(?P<bracket>{_LABEL_PATTERN})】"
    rf"|\[(?P<ascii>{_LABEL_PATTERN})\]"
    rf"|(?P<plain>{_LABEL_PATTERN})[：:])[ \t]*(?:[：:][ \t]*)?"
)
_ITEM_NUMBER = re.compile(r"^(?:\d+|[一二三四五六七八九十]+)[.、．)）][ \t]*")
_ITEM_SEPARATOR = re.compile(r"[；;\n]+")


@dataclass(frozen=True)
class StructuredReportItem:
    field: str
    value: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class StructuredDailyDocument:
    items: tuple[StructuredReportItem, ...]
    fields: frozenset[str]


def parse_structured_daily_document(text: str) -> StructuredDailyDocument | None:
    """Parse a self-identifying Daily document without consulting conversation focus.

    A high-confidence document must identify both current work and the next-day
    plan.  This is deliberately a document-shape contract, not a classifier
    fallback: old Weekly/Monthly focus is never evidence about the document type.
    """

    source = str(text or "")
    headers = list(_SECTION_HEADER.finditer(source))
    if len(headers) < 2:
        return None
    items: list[StructuredReportItem] = []
    fields: set[str] = set()
    for index, header in enumerate(headers):
        label = next(
            value
            for value in (
                header.group("bracket"),
                header.group("ascii"),
                header.group("plain"),
            )
            if value
        )
        field = _DAILY_SECTION_LABELS[label]
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
        section_items = _parse_section_items(
            source,
            field=field,
            start_offset=header.end(),
            end_offset=section_end,
        )
        if section_items:
            fields.add(field)
            items.extend(section_items)
    if not {"today_work", "tomorrow_plan"}.issubset(fields):
        return None
    return StructuredDailyDocument(items=tuple(items), fields=frozenset(fields))


def structured_daily_semantic_payload(text: str) -> dict[str, Any] | None:
    document = parse_structured_daily_document(text)
    if document is None:
        return None
    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    for index, item in enumerate(document.items, start=1):
        entity_id = f"structured-daily-event-{index}"
        action_id = f"structured-daily-capture-{index}"
        entity_ids.append(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "daily_event",
                "value": item.value,
                "confidence": 1.0,
                "attributes": {"field": item.field},
            }
        )
        actions.append(
            {
                "action_id": action_id,
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        )
        segments.append(
            {
                "segment_id": f"structured-daily-segment-{index}",
                "text": text[item.start_offset:item.end_offset],
                "intents": ["daily_append"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
            }
        )
    return {
        "intents": ["daily_append"],
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": entity_ids,
            "remember_turn": True,
        },
    }


def _parse_section_items(
    source: str,
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> tuple[StructuredReportItem, ...]:
    items: list[StructuredReportItem] = []
    cursor = start_offset
    for separator in _ITEM_SEPARATOR.finditer(source, start_offset, end_offset):
        items.extend(_parse_item(source, field=field, start_offset=cursor, end_offset=separator.start()))
        cursor = separator.end()
    items.extend(_parse_item(source, field=field, start_offset=cursor, end_offset=end_offset))
    return tuple(items)


def _parse_item(
    source: str,
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> tuple[StructuredReportItem, ...]:
    raw = source[start_offset:end_offset]
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw.rstrip())
    local_start = leading
    local_end = trailing
    numbered = _ITEM_NUMBER.match(raw[local_start:local_end])
    if numbered is not None:
        local_start += numbered.end()
    value = raw[local_start:local_end].strip()
    if not value:
        return ()
    value_leading = len(raw[local_start:local_end]) - len(raw[local_start:local_end].lstrip())
    absolute_start = start_offset + local_start + value_leading
    absolute_end = absolute_start + len(value)
    return (
        StructuredReportItem(
            field=field,
            value=value,
            start_offset=absolute_start,
            end_offset=absolute_end,
        ),
    )
