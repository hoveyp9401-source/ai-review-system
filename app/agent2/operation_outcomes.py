from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
import re
from typing import Any, Literal, Protocol


BusinessStatus = Literal[
    "succeeded",
    "duplicate",
    "unchanged",
    "blocked",
    "failed",
    "conflict",
    "not_found",
    "partial",
    "registered",
    "matched",
    "queued",
    "sending",
    "accepted_by_provider",
    "delivery_confirmed",
    "waiting_for_reply",
    "accepted_by_one_party",
    "accepted_by_both",
    "declined",
    "cancelled",
    "changed",
    "expired",
]
MessageStatus = Literal[
    "not_applicable",
    "not_requested",
    "scheduled",
    "queued",
    "sending",
    "accepted_by_provider",
    "delivery_confirmed",
    "waiting_for_reply",
    "failed",
    "cancelled",
]

BUSINESS_STATUSES = frozenset(BusinessStatus.__args__)
MESSAGE_STATUSES = frozenset(MessageStatus.__args__)
MUTATION_OPERATIONS = frozenset(
    {
        "create",
        "update",
        "delete",
        "submit",
        "register",
        "respond",
        "create_task",
        "snooze",
    }
)
TRAVEL_RESPONSE_FACT_STATUSES = frozenset(
    {
        "accepted_by_one_party",
        "accepted_by_both",
        "declined",
        "cancelled",
        "expired",
    }
)


@dataclass(frozen=True)
class OutcomeObjectRef:
    object_type: str
    stable_id: str
    label: str
    version: int | None = None

    def __post_init__(self) -> None:
        if not self.object_type.strip() or not self.label.strip():
            raise ValueError("outcome object ref requires type and user-visible label")
        if self.version is not None and self.version < 0:
            raise ValueError("outcome object version must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "stable_id": self.stable_id,
            "label": self.label,
            "version": self.version,
        }


@dataclass(frozen=True)
class OutcomeReceiptRef:
    receipt_id: str
    receipt_type: str
    status: str
    actual_write: bool
    external_message_id: str = ""
    reliable_delivery_evidence: bool = False

    def __post_init__(self) -> None:
        if not self.receipt_id.strip() or not self.receipt_type.strip() or not self.status.strip():
            raise ValueError("outcome receipt ref requires id, type, and status")

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "receipt_type": self.receipt_type,
            "status": self.status,
            "actual_write": self.actual_write,
            "external_message_id": self.external_message_id,
            "reliable_delivery_evidence": self.reliable_delivery_evidence,
        }


@dataclass(frozen=True)
class OutcomeStateTransition:
    from_status: str
    to_status: str

    def as_dict(self) -> dict[str, str]:
        return {"from": self.from_status, "to": self.to_status}


@dataclass(frozen=True)
class OperationOutcome:
    domain: str
    operation: str
    object_ref: OutcomeObjectRef
    business_status: BusinessStatus
    message_status: MessageStatus
    changed_fields: tuple[str, ...]
    user_visible_snapshot: dict[str, Any]
    blocking_reason: str
    receipt_refs: tuple[OutcomeReceiptRef, ...]
    state_transition: OutcomeStateTransition
    actual_write: bool
    source_turn_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    outcome_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    conversation_id: str = ""
    would_write: bool = False
    audit_refs: tuple[str, ...] = ()
    idempotency_key: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.domain.strip() or not self.operation.strip():
            raise ValueError("operation outcome requires domain and operation")
        if self.business_status not in BUSINESS_STATUSES:
            raise ValueError("operation outcome has unknown business status")
        if self.message_status not in MESSAGE_STATUSES:
            raise ValueError("operation outcome has unknown message status")
        if self.message_status == "accepted_by_provider" and not any(
            receipt.external_message_id
            and receipt.status == "accepted_by_provider"
            for receipt in self.receipt_refs
        ):
            raise ValueError("provider acceptance requires external message evidence")
        if self.message_status == "delivery_confirmed" and not any(
            receipt.external_message_id and receipt.reliable_delivery_evidence
            for receipt in self.receipt_refs
        ):
            raise ValueError("delivery confirmation requires reliable callback evidence")
        if (
            self.domain == "travel"
            and self.business_status in TRAVEL_RESPONSE_FACT_STATUSES
        ):
            if not self.object_ref.stable_id.strip():
                raise ValueError("travel response fact requires a stable business object")
            if not any(
                receipt.receipt_type == "database"
                and receipt.status == "executed"
                and receipt.actual_write
                for receipt in self.receipt_refs
            ) or (self.operation == "respond" and not self.actual_write):
                raise ValueError(
                    "travel response fact requires a committed travel response receipt"
                )
        if (
            self.operation in MUTATION_OPERATIONS
            and self.business_status in {"succeeded", "registered"}
            and (
                not self.actual_write
                or not any(
                    receipt.receipt_type == "database"
                    and receipt.status == "executed"
                    and receipt.actual_write
                    for receipt in self.receipt_refs
                )
            )
        ):
            raise ValueError("successful mutation requires a committed write receipt")
        object.__setattr__(self, "user_visible_snapshot", deepcopy(self.user_visible_snapshot))
        object.__setattr__(self, "metadata", deepcopy(self.metadata))
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ValueError("operation outcome created_at must be timezone-aware")

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "operation": self.operation,
            "object_ref": self.object_ref.as_dict(),
            "business_status": self.business_status,
            "message_status": self.message_status,
            "changed_fields": list(self.changed_fields),
            "user_visible_snapshot": deepcopy(self.user_visible_snapshot),
            "blocking_reason": self.blocking_reason,
            "receipt_refs": [item.as_dict() for item in self.receipt_refs],
            "state_transition": self.state_transition.as_dict(),
            "actual_write": self.actual_write,
            "source_turn_id": self.source_turn_id,
            "metadata": deepcopy(self.metadata),
            "outcome_id": self.outcome_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "would_write": self.would_write,
            "audit_refs": list(self.audit_refs),
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat() if self.created_at is not None else "",
        }


class OutcomeReplyStrategy(Protocol):
    def compose(self, outcome: OperationOutcome) -> str: ...


class ReportReply:
    def compose(self, outcome: OperationOutcome) -> str:
        snapshot = outcome.user_visible_snapshot
        if outcome.operation == "confirm_projection":
            case_name = str(snapshot.get("case_name") or "该案件")
            content = str(snapshot.get("content") or "").strip()
            return (
                f"“{case_name}”的案件进展已经保留：{content}\n"
                "这句话是否也加入今天的日报？你可以回复“需要”或“只记案件”。"
            )
        if outcome.operation == "decline_projection":
            return "好的，这条只保留在案件进展里，没有加入日报。"
        report_type = str(snapshot.get("report_type") or "daily")
        label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}.get(
            report_type, "报告"
        )
        acknowledgement = {
            "create": "记下了。",
            "update": "已经改好了。",
            "delete": "这条已经删掉了。",
            "query": "现在的内容是：",
            "submit": "已经提交。",
        }.get(outcome.operation, "已经处理。")
        lines = [acknowledgement, f"当前{label}："]
        fields = (
            ("today_work", "今日工作"),
            ("accomplishments", "完成事项"),
            ("metrics", "关键指标"),
            ("problems", "问题与风险"),
            ("risks", "风险问题"),
            ("tomorrow_plan", "明日计划"),
            ("next_plan", "后续计划"),
        )
        for field_name, field_label in fields:
            if field_name not in snapshot:
                continue
            values = snapshot.get(field_name)
            values = values if isinstance(values, list) else []
            lines.append(field_label)
            lines.extend(f"{index}. {value}" for index, value in enumerate(values, start=1))
            if not values:
                lines.append("暂无")
        return "\n".join(lines)


class CaseProgressReply:
    def compose(self, outcome: OperationOutcome) -> str:
        snapshot = outcome.user_visible_snapshot
        case_name = str(snapshot.get("case_name") or outcome.object_ref.label)
        content = str(snapshot.get("content") or "")
        acknowledgement = {
            "create": "记好了，案件进展是：",
            "update": "已经改好了，案件进展现在是：",
            "delete": "这条案件进展已经删除：",
            "query": "查到的案件进展是：",
        }.get(outcome.operation, "案件进展已处理：")
        lines = [acknowledgement, f"案件：{case_name}", content]
        next_actions = snapshot.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            lines.append("下一步：")
            lines.extend(
                f"{index}. {value}"
                for index, value in enumerate(next_actions, start=1)
                if str(value or "").strip()
            )
        return "\n".join(lines).rstrip()


class TravelReply:
    def compose(self, outcome: OperationOutcome) -> str:
        snapshot = outcome.user_visible_snapshot
        destination = str(snapshot.get("destination") or outcome.object_ref.label)
        date_label = str(snapshot.get("date_label") or "")
        purpose = str(snapshot.get("purpose") or "")
        counterparty = str(snapshot.get("counterparty") or "")
        if outcome.operation == "update" and outcome.business_status == "cancelled":
            return "\n".join(
                value
                for value in (
                    "已取消这次出差安排。",
                    f"地点：{destination}" if destination else "",
                )
                if value
            )
        if outcome.operation == "update" and outcome.business_status == "changed":
            return "\n".join(
                value
                for value in (
                    "已更新这次出差安排。",
                    f"地点：{destination}" if destination else "",
                    f"时间：{date_label}" if date_label else "",
                )
                if value
            )
        details = [f"地点：{destination}"]
        if date_label:
            details.append(f"时间：{date_label}")
        if purpose:
            details.append(f"事由：{purpose}")
        status_text = {
            "registered": "出差安排已经登记。",
            "matched": f"找到了同期同地的同事{f'：{counterparty}' if counterparty else ''}。",
            "queued": "协同通知已经排队，尚未发送。",
            "sending": "协同通知正在发送，暂时还不能确认平台是否受理。",
            "accepted_by_provider": "协同通知请求已提交，平台已经受理；这不代表对方已经收到或同意。",
            "delivery_confirmed": "协同通知已有可靠的送达确认。",
            "waiting_for_reply": "协同通知已发出，目前正在等待回复。",
            "accepted_by_one_party": "已有一方接受协同，还在等待另一方回复。",
            "accepted_by_both": "双方都已明确接受这次出差协同。",
            "declined": "这次出差协同已被拒绝。",
            "failed": "协同通知发送失败，没有宣称送达。",
            "cancelled": "这次出差协同已经取消。",
            "expired": "这次出差协同已经过期。",
        }.get(outcome.business_status, "出差协同状态已更新。")
        return "\n".join([status_text, *details])


class KnowledgeReply:
    def compose(self, outcome: OperationOutcome) -> str:
        return str(outcome.user_visible_snapshot.get("answer") or "没有找到可靠答案。")


class ChatReply:
    def compose(self, outcome: OperationOutcome) -> str:
        return str(outcome.user_visible_snapshot.get("text") or "我看到了。")


class FollowupReply:
    def compose(self, outcome: OperationOutcome) -> str:
        snapshot = outcome.user_visible_snapshot
        case_name = str(snapshot.get("case_name") or outcome.object_ref.label)
        if outcome.operation == "snooze":
            until = str(snapshot.get("snoozed_until") or "稍后")
            return f"好，我会暂停“{case_name}”的本次追问，下次在 {until} 之后再问你。"
        if outcome.operation == "update" and "cadence_type" in snapshot:
            cadence = {
                "daily": "每天一次", "weekly": "每周一次",
                "every_15_days": "每 15 天一次", "monthly": "每月一次",
                "custom_interval": "自定义周期", "event_only": "仅关键节点",
                "manual_only": "仅人工追问", "paused": "暂停",
                "disabled": "关闭主动追问",
            }.get(str(snapshot.get("cadence_type") or ""), "已更新")
            next_due = str(snapshot.get("next_due_at") or "")
            event_labels = [
                label for field, label in (
                    ("hearing_reminders_enabled", "开庭提醒"),
                    ("stage_transition_enabled", "阶段变化提醒"),
                    ("node_transition_enabled", "关键节点提醒"),
                )
                if snapshot.get(field)
            ]
            reply = f"“{case_name}”的主动追问已调整为{cadence}。"
            if next_due:
                reply += f"下次计划追问时间是 {next_due}。"
            reply += (
                f"保留：{'、'.join(event_labels)}。"
                if event_labels else "事件提醒均未开启。"
            )
            return reply
        due_at = str(snapshot.get("due_at_label") or "")
        question = str(snapshot.get("question_summary") or "案件进展")
        prefix = f"我已经为“{case_name}”创建了追问任务"
        if due_at:
            prefix += f"，计划在{due_at}了解{question}"
        status = {
            "scheduled": "。任务还没进入发送队列。",
            "queued": "。消息已进入发送队列，目前还没有发送结果。",
            "sending": "。消息正在发送，暂时还不能确认平台是否受理。",
            "accepted_by_provider": "。钉钉接口已经受理，但这不代表对方已经收到或回复。",
            "delivery_confirmed": "。钉钉已有可靠的送达确认，目前等待用户回复。",
            "waiting_for_reply": "。消息已发出，目前正在等待用户回复。",
            "failed": "，但消息发送失败，没有宣称送达。",
            "cancelled": "，但任务已经取消，不会继续发送。",
        }.get(outcome.message_status, "。")
        return prefix + status


class OutcomeFactGuard:
    """Fail closed when expression text contradicts receipt-backed facts."""

    _message_sent_receipt_statuses = frozenset(
        {"accepted_by_provider", "delivery_confirmed", "waiting_for_reply"}
    )

    _write_success_patterns = tuple(
        re.compile(re.escape(claim))
        for claim in (
            "已记录",
            "已经记录",
            "已保存",
            "已经保存",
            "已写入",
            "已经写入",
            "已添加",
            "已经添加",
            "已加入",
            "已经加入",
            "已登记",
            "已经登记",
            "已修改",
            "已经修改",
            "已删除",
            "已经删除",
            "已提交",
            "已经提交",
            "记下了",
            "已记下",
            "已经记下",
            "记好了",
            "已记好",
            "已经记好",
            "记住了",
            "已记住",
            "已经记住",
            "我会记下",
            "将记下",
            "我会记录",
            "将记录",
            "我会登记",
            "将登记",
            "我会保存",
            "将保存",
            "收录了",
            "存下了",
        )
    )
    _message_send_patterns = tuple(
        re.compile(re.escape(claim))
        for claim in (
            "通知已发出",
            "通知已经发出",
            "消息已发出",
            "消息已经发出",
            "通知已发送",
            "通知已经发送",
            "消息已发送",
            "消息已经发送",
            "发送成功",
        )
    )
    _delivery_patterns = tuple(
        re.compile(re.escape(claim))
        for claim in ("已送达", "已经送达", "对方已收到", "对方已经收到")
    )
    _agreement_patterns = (
        re.compile(r"(?:对方|双方|一方).{0,6}(?:已经|已)(?:明确)?(?:同意|接受)"),
        re.compile(r"(?:对方|双方|一方).{0,6}(?:同意|接受)了"),
    )
    _bounded_write_success_pattern = re.compile(
        r"(?:已经|已).{0,12}(?:加入|添加|写入|保存|记录|登记)"
        r"(?:到|进|至)?(?:日报|周报|月报|报告|系统|数据库)"
    )
    _clause_boundary_pattern = re.compile(
        r"[，,。；;！？!?\n]+|(?:但是|不过|而是)"
    )
    _explicit_denial_pattern = re.compile(
        r"(?:不能说|不可说|不要说|别说|无法确认|不能确认|尚不能确认|"
        r"没有证据(?:证明|表明|说明)?|不代表|并不代表|并非|不是).{0,16}$"
    )

    def violations(
        self, outcomes: tuple[OperationOutcome, ...], reply: str
    ) -> tuple[str, ...]:
        violations: list[str] = []
        write_patterns = (
            *self._write_success_patterns,
            self._bounded_write_success_pattern,
        )
        has_write_claim = self._has_positive_assertion(reply, write_patterns)
        if any(
            outcome.operation in MUTATION_OPERATIONS
            and not outcome.actual_write
            for outcome in outcomes
        ) and has_write_claim:
            violations.append("write_success_without_actual_write")
        if has_write_claim and any(
            outcome.domain in {"chat", "knowledge"} for outcome in outcomes
        ):
            violations.append("read_only_reply_claims_business_write")
        read_only_expression = any(
            outcome.domain in {"chat", "knowledge"} for outcome in outcomes
        )
        has_message_send_claim = self._has_positive_assertion(
            reply, self._message_send_patterns
        )
        if read_only_expression and has_message_send_claim:
            violations.append("read_only_reply_claims_message_sent")
        if has_message_send_claim and any(
            outcome.domain in {"travel", "followup"}
            and not self._has_external_message_evidence(outcome)
            for outcome in outcomes
        ):
            violations.append("message_sent_claim_without_external_evidence")
        has_agreement_claim = self._has_positive_assertion(
            reply, self._agreement_patterns
        )
        if read_only_expression and has_agreement_claim:
            violations.append("read_only_reply_claims_user_agreement")
        if has_agreement_claim and any(
            outcome.domain == "travel"
            and not self._has_committed_travel_response_receipt(outcome)
            for outcome in outcomes
        ):
            violations.append(
                "agreement_claim_without_committed_response_receipt"
            )
        delivery_evidence = any(
            outcome.message_status == "delivery_confirmed"
            and any(receipt.reliable_delivery_evidence for receipt in outcome.receipt_refs)
            for outcome in outcomes
        )
        if not delivery_evidence and self._has_positive_assertion(
            reply, self._delivery_patterns
        ):
            violations.append("delivery_claim_without_reliable_evidence")
        return tuple(violations)

    @classmethod
    def _has_external_message_evidence(cls, outcome: OperationOutcome) -> bool:
        return any(
            receipt.external_message_id.strip()
            and receipt.status in cls._message_sent_receipt_statuses
            for receipt in outcome.receipt_refs
        )

    @staticmethod
    def _has_committed_travel_response_receipt(
        outcome: OperationOutcome,
    ) -> bool:
        return any(
            receipt.receipt_type == "database"
            and receipt.status == "executed"
            and receipt.actual_write
            for receipt in outcome.receipt_refs
        )

    @classmethod
    def _has_positive_assertion(
        cls,
        reply: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> bool:
        """Recognize only non-negated success assertions in bounded clauses."""

        for clause in cls._clause_boundary_pattern.split(reply):
            if not clause:
                continue
            for pattern in patterns:
                for match in pattern.finditer(clause):
                    prefix = clause[: match.start()].rstrip()
                    if not cls._explicit_denial_pattern.search(prefix):
                        return True
        return False


class OutcomeReplyComposer:
    """Compose user-visible text from outcomes without re-deciding their facts."""

    def __init__(self) -> None:
        self._strategies: dict[str, OutcomeReplyStrategy] = {
            "report": ReportReply(),
            "case_progress": CaseProgressReply(),
            "travel": TravelReply(),
            "knowledge": KnowledgeReply(),
            "chat": ChatReply(),
            "followup": FollowupReply(),
        }
        self._fact_guard = OutcomeFactGuard()

    def compose(self, outcomes: tuple[OperationOutcome, ...]) -> str:
        if not outcomes:
            return "这次没有形成可执行的业务结果，我没有写入任何内容。"
        try:
            replies: list[str] = []
            for outcome in outcomes:
                reply = self._compose_one(outcome)
                if self._fact_guard.violations((outcome,), reply):
                    raise ValueError("reply contradicts operation outcome")
                replies.append(reply)
            return "\n\n".join(replies)
        except (KeyError, TypeError, ValueError):
            if not any(outcome.actual_write for outcome in outcomes):
                return "这次没有写入任何业务内容；回复暂时无法生成，我不会改变或补充执行事实。"
            return "业务操作结果已按实际回执保留，但回复暂时无法生成；我不会改变或补充执行事实。"

    def _compose_one(self, outcome: OperationOutcome) -> str:
        if outcome.business_status in {"blocked", "failed", "conflict", "not_found"}:
            if outcome.blocking_reason == "travel_time_needs_clarification":
                destination = str(
                    outcome.user_visible_snapshot.get("destination") or ""
                ).strip()
                prefix = f"去{destination}的出差" if destination else "这次出差"
                return (
                    f"{prefix}我已经识别到了，还差出发日期。"
                    "哪天去？本次还没有登记。"
                )
            if outcome.blocking_reason == "travel_location_needs_clarification":
                return (
                    "这次出差的城市还不能确定。请补充具体城市；"
                    "本次还没有登记。"
                )
            if outcome.blocking_reason in {
                "case_target_not_found",
                "case_reference_not_uniquely_authorized",
                "case_reference_not_grounded_in_segment",
            }:
                case_name = str(
                    outcome.user_visible_snapshot.get("case_name")
                    or outcome.object_ref.label
                    or ""
                ).strip()
                target = f"“{case_name}”" if case_name else "这个案件编号或名称"
                return (
                    f"我没在你当前分配的案件中找到{target}。"
                    "请核对案件编号或名称后再发一次；本次没有登记。"
                )
            label = outcome.object_ref.label
            return f"「{label}」这次暂时没能记录，我没有改动现有内容。"
        strategy = self._strategies.get(outcome.domain)
        if strategy is None:
            return f"「{outcome.object_ref.label}」已处理。"
        if outcome.business_status in {"duplicate", "unchanged"}:
            view = replace(outcome, operation="query")
            return "这项此前已经处理，本次没有重复写入。\n" + strategy.compose(view)
        return strategy.compose(outcome)
