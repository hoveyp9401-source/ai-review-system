from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Any, Literal, Protocol

from app.agent2.business.travel import LocationRegistry, resolve_travel_window


CaseTravelPendingStatus = Literal[
    "awaiting_confirmation",
    "awaiting_destination",
    "awaiting_city",
    "consumed",
    "cancelled",
    "expired",
    "conflicted",
]


@dataclass(frozen=True)
class CaseTravelClarificationOffer:
    case_id: str
    case_version: int
    case_name: str
    travel_date: str
    purpose_summary: str
    suggested_destination: str
    question: str


@dataclass(frozen=True)
class CaseTravelClarificationPending:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    case_id: str
    case_version: int
    case_name: str
    source_message_id: str
    raw_text: str
    travel_date: str
    purpose_summary: str
    suggested_destination: str
    status: CaseTravelPendingStatus
    version: int
    candidate_destination: str = ""


@dataclass(frozen=True)
class CaseTravelClarificationResolution:
    status: Literal[
        "ready",
        "needs_destination",
        "needs_city",
        "cancelled",
        "not_an_answer",
    ]
    case_id: str
    travel_date: str
    purpose_summary: str
    destination_raw: str = ""
    destination_normalized: str = ""
    city_code: str = ""
    province_code: str = ""
    question: str = ""
    actual_write: bool = False


@dataclass(frozen=True)
class CaseTravelClarificationScope:
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class CaseTravelClarificationTurnResult:
    handled: bool
    status: str
    reply: str
    pending: CaseTravelClarificationPending | None = None
    resolution: CaseTravelClarificationResolution | None = None
    receipt: Any | None = None


class CaseTravelClarificationStore(Protocol):
    async def list_active(
        self, scope: CaseTravelClarificationScope
    ) -> tuple[CaseTravelClarificationPending, ...]: ...

    async def save(
        self,
        pending: CaseTravelClarificationPending,
        *,
        expected_version: int,
        reply_message_id: str,
        receipt_id: str = "",
    ) -> CaseTravelClarificationPending: ...


class CaseTravelIntentWriter(Protocol):
    async def register(
        self,
        pending: CaseTravelClarificationPending,
        resolution: CaseTravelClarificationResolution,
        scope: CaseTravelClarificationScope,
    ) -> Any: ...


class InMemoryCaseTravelClarificationStore:
    def __init__(
        self, items: tuple[CaseTravelClarificationPending, ...] = ()
    ) -> None:
        self.items = {item.pending_id: item for item in items}

    async def list_active(
        self, scope: CaseTravelClarificationScope
    ) -> tuple[CaseTravelClarificationPending, ...]:
        return tuple(
            item
            for item in self.items.values()
            if item.tenant_id == scope.tenant_id
            and item.user_id == scope.user_id
            and item.conversation_id == scope.conversation_id
            and item.status
            in {"awaiting_confirmation", "awaiting_destination", "awaiting_city"}
        )

    async def save(
        self,
        pending: CaseTravelClarificationPending,
        *,
        expected_version: int,
        reply_message_id: str,
        receipt_id: str = "",
    ) -> CaseTravelClarificationPending:
        current = self.items.get(pending.pending_id)
        if current is None or current.version != expected_version:
            raise RuntimeError("case travel clarification version conflict")
        if pending.version != expected_version + 1:
            raise RuntimeError("case travel clarification version must advance")
        self.items[pending.pending_id] = pending
        return pending


class CaseTravelClarificationRuntime:
    def __init__(
        self,
        *,
        store: CaseTravelClarificationStore,
        writer: CaseTravelIntentWriter,
        locations: LocationRegistry | None = None,
    ) -> None:
        self.store = store
        self.writer = writer
        self.locations = locations or LocationRegistry.default()

    async def handle_reply(
        self,
        *,
        scope: CaseTravelClarificationScope,
        raw_text: str,
        allowed_case_ids: tuple[str, ...],
        other_active_pending_count: int = 0,
    ) -> CaseTravelClarificationTurnResult | None:
        pendings = await self.store.list_active(scope)
        assessed = tuple(
            (
                pending,
                resolve_case_travel_clarification_answer(
                    pending, raw_text, locations=self.locations
                ),
            )
            for pending in pendings
        )
        applicable = tuple(
            item for item in assessed if item[1].status != "not_an_answer"
        )
        if not applicable:
            return None
        if other_active_pending_count > 0 and _is_context_dependent_answer(raw_text):
            return CaseTravelClarificationTurnResult(
                handled=True,
                status="ambiguous",
                reply=(
                    "现在还有其他事项也在等你确认，我不确定你在确认哪件事。"
                    "请回复案件简称和具体法院；我没有登记出差。"
                ),
            )
        if len(applicable) != 1:
            return CaseTravelClarificationTurnResult(
                handled=True,
                status="ambiguous",
                reply="现在有多个案件的出差地点待确认，请带案件简称再回复；我没有登记出差。",
            )
        pending, resolution = applicable[0]
        if pending.case_id not in set(allowed_case_ids):
            conflicted = replace(
                pending, status="conflicted", version=pending.version + 1
            )
            await self.store.save(
                conflicted,
                expected_version=pending.version,
                reply_message_id=scope.source_message_id,
            )
            return CaseTravelClarificationTurnResult(
                handled=True,
                status="permission_revoked",
                reply="这个案件目前不在你的可操作范围内，我没有登记出差。",
                pending=conflicted,
                resolution=resolution,
            )
        if resolution.status == "cancelled":
            changed = replace(
                pending, status="cancelled", version=pending.version + 1
            )
            changed = await self.store.save(
                changed,
                expected_version=pending.version,
                reply_message_id=scope.source_message_id,
            )
            return CaseTravelClarificationTurnResult(
                True, "cancelled", resolution.question, changed, resolution
            )
        if resolution.status in {"needs_destination", "needs_city"}:
            changed = replace(
                pending,
                status=(
                    "awaiting_destination"
                    if resolution.status == "needs_destination"
                    else "awaiting_city"
                ),
                candidate_destination=(
                    resolution.destination_raw
                    if resolution.status == "needs_city"
                    else pending.candidate_destination
                ),
                version=pending.version + 1,
            )
            changed = await self.store.save(
                changed,
                expected_version=pending.version,
                reply_message_id=scope.source_message_id,
            )
            return CaseTravelClarificationTurnResult(
                True, resolution.status, resolution.question, changed, resolution
            )

        receipt = await self.writer.register(pending, resolution, scope)
        receipt_status = str(getattr(receipt, "status", "") or "")
        if receipt_status not in {"executed", "duplicate"}:
            return CaseTravelClarificationTurnResult(
                handled=True,
                status="failed",
                reply="出差暂时没有登记成功，案件进展仍然保留。",
                pending=pending,
                resolution=resolution,
                receipt=receipt,
            )
        consumed = replace(
            pending, status="consumed", version=pending.version + 1
        )
        consumed = await self.store.save(
            consumed,
            expected_version=pending.version,
            reply_message_id=scope.source_message_id,
            receipt_id=str(getattr(receipt, "receipt_id", "") or ""),
        )
        return CaseTravelClarificationTurnResult(
            handled=True,
            status="registered",
            reply="",
            pending=consumed,
            resolution=resolution,
            receipt=receipt,
        )


def build_case_travel_clarification(
    *,
    raw_text: str,
    case_id: str,
    case_version: int,
    case_name: str,
    suggested_destination: str,
    occurred_at: datetime,
) -> CaseTravelClarificationOffer | None:
    text = str(raw_text or "").strip()
    if not _looks_like_physical_case_visit(text):
        return None
    try:
        window = resolve_travel_window(text, reference_date=occurred_at.date())
    except (OverflowError, ValueError):
        return None
    date_label = _relative_date_label(window.start_date.isoformat(), occurred_at)
    destination = str(suggested_destination or "").strip()
    if destination:
        question = (
            f"底表显示受理机构为{destination}。你{date_label}是去这里吗？"
            "确认后我再登记出差。"
        )
    else:
        question = f"你{date_label}具体去哪个法院？确认地点后我再登记出差。"
    return CaseTravelClarificationOffer(
        case_id=str(case_id),
        case_version=int(case_version),
        case_name=str(case_name or "").strip(),
        travel_date=window.start_date.isoformat(),
        purpose_summary=text,
        suggested_destination=destination,
        question=question,
    )


def resolve_case_travel_clarification_answer(
    pending: CaseTravelClarificationPending,
    answer_text: str,
    *,
    locations: LocationRegistry | None = None,
) -> CaseTravelClarificationResolution:
    text = str(answer_text or "").strip()
    base = {
        "case_id": pending.case_id,
        "travel_date": pending.travel_date,
        "purpose_summary": pending.purpose_summary,
    }
    if _is_cancel_answer(text):
        return CaseTravelClarificationResolution(
            status="cancelled",
            question="好的，这次不登记出差，案件进展会保留。",
            **base,
        )
    if pending.status == "awaiting_city" and pending.candidate_destination.strip():
        city = (locations or LocationRegistry.default()).resolve(text)
        if city.status == "resolved":
            return CaseTravelClarificationResolution(
                status="ready",
                destination_raw=pending.candidate_destination.strip(),
                destination_normalized=city.destination_normalized,
                city_code=city.city_code,
                province_code=city.province_code,
                **base,
            )
        return CaseTravelClarificationResolution(
            status="needs_city",
            destination_raw=pending.candidate_destination.strip(),
            question=(
                f"我记住是{pending.candidate_destination.strip()}了。"
                "请再告诉我所在城市，我先不登记出差。"
            ),
            **base,
        )
    destination = ""
    if _is_affirmative_answer(text):
        destination = pending.suggested_destination.strip()
    else:
        destination = _explicit_destination(text)
    if not destination or _is_generic_destination(destination):
        if _looks_like_destination_answer(text):
            return CaseTravelClarificationResolution(
                status="needs_destination",
                question="好的，具体是哪个法院？我先不登记出差。",
                **base,
            )
        return CaseTravelClarificationResolution(status="not_an_answer", **base)

    location = (locations or LocationRegistry.default()).resolve(destination)
    if location.status != "resolved":
        return CaseTravelClarificationResolution(
            status="needs_city",
            destination_raw=destination,
            question=f"我记住是{destination}了。这个法院在哪个城市？我先不登记出差。",
            **base,
        )
    return CaseTravelClarificationResolution(
        status="ready",
        destination_raw=destination,
        destination_normalized=location.destination_normalized,
        city_code=location.city_code,
        province_code=location.province_code,
        **base,
    )


def _looks_like_physical_case_visit(text: str) -> bool:
    has_motion = any(marker in text for marker in ("去", "赴", "到", "前往", "出差", "拜访"))
    has_legal_destination = any(
        marker in text for marker in ("法院", "法官", "仲裁委", "开庭", "庭审", "出庭")
    )
    has_future_time = any(
        marker in text for marker in ("明天", "后天", "下周", "周一", "周二", "周三", "周四", "周五", "周六", "周日")
    ) or bool(re.search(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text))
    return has_motion and has_legal_destination and has_future_time


def _relative_date_label(iso_date: str, occurred_at: datetime) -> str:
    delta = (datetime.fromisoformat(iso_date).date() - occurred_at.date()).days
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return f"{datetime.fromisoformat(iso_date).date().month}月{datetime.fromisoformat(iso_date).date().day}日"


def _is_affirmative_answer(text: str) -> bool:
    compact = re.sub(r"[\s，,。.!！?？]", "", text)
    return compact in {"是", "是的", "对", "对的", "确认", "就是这里", "就是这个", "去这里", "没错"}


def _is_cancel_answer(text: str) -> bool:
    compact = re.sub(r"[\s，,。.!！?？]", "", text)
    return compact in {"不去了", "不去", "取消", "不出差", "不用登记", "只记案件", "不用"}


def _looks_like_destination_answer(text: str) -> bool:
    return any(marker in text for marker in ("法院", "仲裁委", "另一个", "不是", "去", "地点"))


def _explicit_destination(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^(?:不是|不对|错了)[，,、：:\s]*", "", value)
    value = re.sub(r"^(?:是|改成|改为|去|到|前往)[，,、：:\s]*", "", value)
    value = re.split(r"[。；;！!？?]", value, maxsplit=1)[0].strip(" ，,、：:")
    return value


def _is_generic_destination(value: str) -> bool:
    compact = re.sub(r"[\s，,。]", "", str(value or ""))
    return compact in {"法院", "另一个法院", "其他法院", "那个法院", "另一个", "其他地方", "另一个地方"}


def _is_context_dependent_answer(text: str) -> bool:
    compact = re.sub(r"[\s，,。.!！?？]", "", str(text or ""))
    return compact in {
        "是",
        "是的",
        "对",
        "对的",
        "确认",
        "就是这里",
        "就是这个",
        "去这里",
        "没错",
        "不是",
        "另一个",
        "另一个法院",
        "其他法院",
        "取消",
        "不用",
        "不用登记",
        "只记案件",
    }
