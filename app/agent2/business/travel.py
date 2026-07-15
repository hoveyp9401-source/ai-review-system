from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Protocol
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True)
class LocationDefinition:
    canonical_name: str
    city_code: str
    province_code: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LocationResolution:
    status: str
    destination_normalized: str = ""
    city_code: str = ""
    province_code: str = ""


class LocationRegistry:
    def __init__(self, definitions: tuple[LocationDefinition, ...]):
        self.definitions = definitions

    @classmethod
    def default(cls) -> "LocationRegistry":
        return cls(
            (
                LocationDefinition("南京市", "320100", "320000", ("南京", "南京市", "江苏南京")),
                LocationDefinition("无锡市", "320200", "320000", ("无锡", "无锡市", "江苏无锡")),
                LocationDefinition("徐州市", "320300", "320000", ("徐州", "徐州市", "江苏徐州")),
                LocationDefinition("常州市", "320400", "320000", ("常州", "常州市", "江苏常州")),
                LocationDefinition("苏州市", "320500", "320000", ("苏州", "苏州市", "江苏苏州")),
                LocationDefinition("南通市", "320600", "320000", ("南通", "南通市", "江苏南通")),
                LocationDefinition("连云港市", "320700", "320000", ("连云港", "连云港市", "江苏连云港")),
                LocationDefinition("淮安市", "320800", "320000", ("淮安", "淮安市", "江苏淮安")),
                LocationDefinition("盐城市", "320900", "320000", ("盐城", "盐城市", "江苏盐城")),
                LocationDefinition("扬州市", "321000", "320000", ("扬州", "扬州市", "江苏扬州")),
                LocationDefinition("镇江市", "321100", "320000", ("镇江", "镇江市", "江苏镇江")),
                LocationDefinition("泰州市", "321200", "320000", ("泰州", "泰州市", "江苏泰州")),
                LocationDefinition("宿迁市", "321300", "320000", ("宿迁", "宿迁市", "江苏宿迁")),
                LocationDefinition("上海市", "310100", "310000", ("上海", "上海市")),
                LocationDefinition("北京市", "110100", "110000", ("北京", "北京市")),
                LocationDefinition("深圳市", "440300", "440000", ("深圳", "深圳市", "广东深圳")),
                LocationDefinition("昆明市", "530100", "530000", ("昆明", "昆明市", "云南昆明")),
                LocationDefinition("西安市", "610100", "610000", ("西安", "西安市", "陕西西安")),
            )
        )

    def resolve(self, raw: str) -> LocationResolution:
        normalized = _compact(raw)
        matches = [
            item
            for item in self.definitions
            if any(_compact(alias) and _compact(alias) in normalized for alias in item.aliases)
        ]
        unique = {item.city_code: item for item in matches}
        if len(unique) == 1:
            item = next(iter(unique.values()))
            return LocationResolution("resolved", item.canonical_name, item.city_code, item.province_code)
        return LocationResolution("needs_clarification") if normalized else LocationResolution("not_found")


@dataclass(frozen=True)
class TravelWindow:
    start_date: date
    end_date: date
    precision: str


def resolve_travel_window(raw: str, *, reference_date: date) -> TravelWindow:
    text = _compact(raw)
    duration_match = re.search(r"去(\d+|[一二两三四五六七八九十]+)天", text)
    duration = _number(duration_match.group(1)) if duration_match else 1
    if "两天后" in text:
        start = reference_date + timedelta(days=2)
    elif "后天" in text:
        start = reference_date + timedelta(days=2)
    elif "明天" in text:
        start = reference_date + timedelta(days=1)
    elif "下周一" in text:
        days = (7 - reference_date.weekday()) % 7
        start = reference_date + timedelta(days=days or 7)
    else:
        iso_date_match = re.search(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text)
        if iso_date_match:
            start = date.fromisoformat(iso_date_match.group(0))
            return TravelWindow(
                start,
                start + timedelta(days=max(1, duration) - 1),
                "day",
            )
        day_match = re.search(r"(\d{1,2})号", text)
        if not day_match:
            raise ValueError("travel time needs clarification")
        day = int(day_match.group(1))
        month = reference_date.month + (1 if day < reference_date.day else 0)
        year = reference_date.year + (1 if month == 13 else 0)
        month = 1 if month == 13 else month
        start = date(year, month, day)
    return TravelWindow(start, start + timedelta(days=max(1, duration) - 1), "day")


class TravelIntentLike(Protocol):
    travel_intent_id: str
    tenant_id: str
    company_id: str
    department_id: str
    team_id: str
    user_id: str
    city_code: str
    destination_normalized: str
    start_at: datetime
    end_at: datetime
    status: str
    confidence: float


@dataclass(frozen=True)
class TravelMatchCandidate:
    candidate_id: str
    tenant_id: str
    travel_intent_ids: tuple[str, ...]
    participant_ids: tuple[str, ...]
    destination: str
    overlap_start: datetime
    overlap_end: datetime
    match_reason: str
    match_score: float


class TravelMatcher:
    VALID_STATUSES = {"proposed", "planned", "confirmed", "changed"}

    def match(self, intents: Iterable[TravelIntentLike]) -> tuple[TravelMatchCandidate, ...]:
        eligible = [
            item
            for item in intents
            if item.status in self.VALID_STATUSES and item.confidence >= 0.85 and item.city_code
        ]
        partitions: dict[tuple[str, str, str, str, str], list[TravelIntentLike]] = {}
        for item in eligible:
            partitions.setdefault(
                (
                    item.tenant_id,
                    item.company_id,
                    item.department_id,
                    item.team_id,
                    item.city_code,
                ),
                [],
            ).append(item)

        raw_candidates: list[TravelMatchCandidate] = []
        for (
            tenant_id,
            _company_id,
            _department_id,
            _team_id,
            _city_code,
        ), records in sorted(partitions.items()):
            records.sort(key=lambda item: (item.start_at, item.end_at, item.user_id, item.travel_intent_id))
            for point in sorted({item.start_at for item in records}):
                active_by_user: dict[str, TravelIntentLike] = {}
                for item in records:
                    if item.start_at <= point <= item.end_at:
                        current = active_by_user.get(item.user_id)
                        if current is None or (item.end_at, item.travel_intent_id) > (
                            current.end_at,
                            current.travel_intent_id,
                        ):
                            active_by_user[item.user_id] = item
                active = sorted(active_by_user.values(), key=lambda item: item.user_id)
                if len(active) < 2:
                    continue
                overlap_start = max(item.start_at for item in active)
                overlap_end = min(item.end_at for item in active)
                if overlap_start > overlap_end:
                    continue
                intent_ids = tuple(sorted(item.travel_intent_id for item in active))
                participant_ids = tuple(sorted(item.user_id for item in active))
                identity = (
                    f"{tenant_id}|{'|'.join(intent_ids)}|"
                    f"{overlap_start.isoformat()}|{overlap_end.isoformat()}"
                )
                raw_candidates.append(
                    TravelMatchCandidate(
                        str(uuid5(NAMESPACE_URL, identity)),
                        tenant_id,
                        intent_ids,
                        participant_ids,
                        active[0].destination_normalized,
                        overlap_start,
                        overlap_end,
                        "same_city_and_overlapping_date",
                        min(float(item.confidence) for item in active),
                    )
                )

        unique = {
            (item.tenant_id, item.travel_intent_ids, item.overlap_start, item.overlap_end): item
            for item in raw_candidates
        }
        candidates = list(unique.values())
        maximal = [
            item
            for item in candidates
            if not any(
                item.tenant_id == other.tenant_id
                and set(item.participant_ids) < set(other.participant_ids)
                and item.overlap_start <= other.overlap_end
                and other.overlap_start <= item.overlap_end
                for other in candidates
            )
        ]
        return tuple(
            sorted(
                maximal,
                key=lambda item: (
                    item.tenant_id,
                    item.destination,
                    item.overlap_start,
                    item.participant_ids,
                ),
            )
        )


def _compact(value: str) -> str:
    return re.sub(r"[\s,，.。]", "", unicodedata.normalize("NFKC", str(value or "")))


def _number(value: str) -> int:
    if value.isdigit():
        return int(value)
    mapping = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return mapping.get(value, 1)
