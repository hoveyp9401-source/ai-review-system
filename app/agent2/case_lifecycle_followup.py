from __future__ import annotations

from dataclasses import dataclass, field
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo
from uuid import NAMESPACE_URL, uuid5


CadenceType = Literal[
    "daily",
    "weekly",
    "every_15_days",
    "monthly",
    "custom_interval",
    "event_only",
    "manual_only",
    "paused",
    "disabled",
]


@dataclass(frozen=True)
class CaseFollowupPolicySnapshot:
    policy_id: str
    tenant_id: str
    case_id: str
    assigned_user_id: str
    enabled: bool
    cadence_type: CadenceType
    timezone: str
    last_meaningful_progress_at: datetime | None
    next_due_at: datetime | None
    version: int
    policy_source: str = "tenant_default"
    event_triggers_enabled: bool = True
    hearing_reminders_enabled: bool = True
    stage_transition_enabled: bool = True
    node_transition_enabled: bool = True
    business_days_only: bool = False
    custom_interval_days: int | None = None
    snoozed_until: datetime | None = None


@dataclass(frozen=True)
class CaseFollowupSubject:
    tenant_id: str
    case_id: str
    assigned_user_id: str
    case_type: str
    stage: str
    node: str
    case_version: int
    case_name: str


@dataclass(frozen=True)
class CaseFollowupTaskSnapshot:
    followup_id: str
    tenant_id: str
    case_id: str
    assigned_user_id: str
    trigger_type: str
    question_type: str
    task_status: str


@dataclass(frozen=True)
class CaseFollowupTrigger:
    trigger_type: str
    trigger_event_id: str
    question_type: str
    due_at: datetime
    priority: int
    facts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommittedCaseLifecycleChange:
    tenant_id: str
    case_id: str
    case_type: str
    receipt_id: str
    receipt_status: str
    actual_write: bool
    occurred_at: datetime
    from_stage: str
    to_stage: str
    node: str
    case_version: int


@dataclass(frozen=True)
class FollowupEvaluation:
    eligible: bool
    reason_code: str
    trigger_type: str = ""
    question_type: str = ""
    due_at: datetime | None = None
    trigger_sources: tuple[CaseFollowupTrigger, ...] = ()


@dataclass(frozen=True)
class CaseFollowupTaskPlan:
    followup_id: str
    tenant_id: str
    case_id: str
    assigned_user_id: str
    case_name: str
    case_type: str
    stage: str
    node: str
    policy_id: str
    trigger_type: str
    trigger_event_ids: tuple[str, ...]
    trigger_sources: tuple[CaseFollowupTrigger, ...]
    question_type: str
    priority: int
    due_at: datetime
    expires_at: datetime
    case_version: int
    question_text: str
    idempotency_key: str


class FollowupPolicyEngine:
    """Evaluate one permission-scoped Case and its effective policy."""

    def evaluate(
        self,
        subject: CaseFollowupSubject,
        policy: CaseFollowupPolicySnapshot,
        *,
        now: datetime,
        existing_tasks: tuple[CaseFollowupTaskSnapshot, ...],
    ) -> FollowupEvaluation:
        if (
            subject.tenant_id != policy.tenant_id
            or subject.case_id != policy.case_id
            or subject.assigned_user_id != policy.assigned_user_id
        ):
            return FollowupEvaluation(False, "subject_policy_scope_mismatch")
        if not policy.enabled or policy.cadence_type in {"disabled", "paused"}:
            return FollowupEvaluation(False, "policy_disabled_or_paused")
        if policy.snoozed_until is not None and now < policy.snoozed_until:
            return FollowupEvaluation(False, "policy_snoozed")
        if any(
            task.tenant_id == subject.tenant_id
            and task.case_id == subject.case_id
            and task.assigned_user_id == subject.assigned_user_id
            and task.question_type == "meaningful_progress"
            and task.task_status in {
                "queued",
                "sending",
                "accepted_by_provider",
                "delivery_confirmed",
                "waiting_for_reply",
            }
            for task in existing_tasks
        ):
            return FollowupEvaluation(False, "waiting_for_reply_exists")
        if policy.cadence_type in {"event_only", "manual_only"}:
            return FollowupEvaluation(False, "fixed_cadence_not_enabled")
        if policy.next_due_at is None or now < policy.next_due_at:
            return FollowupEvaluation(False, "not_due")
        return FollowupEvaluation(
            True,
            "fixed_cadence_due",
            trigger_type="fixed_cadence",
            question_type="meaningful_progress",
            due_at=policy.next_due_at,
        )

    def evaluate_triggers(
        self,
        subject: CaseFollowupSubject,
        policy: CaseFollowupPolicySnapshot,
        *,
        triggers: tuple[CaseFollowupTrigger, ...],
        now: datetime,
        existing_tasks: tuple[CaseFollowupTaskSnapshot, ...],
    ) -> FollowupEvaluation:
        if any(
            item.tenant_id == subject.tenant_id
            and item.case_id == subject.case_id
            and item.assigned_user_id == subject.assigned_user_id
            and item.task_status in {
                "queued", "sending", "accepted_by_provider",
                "delivery_confirmed", "waiting_for_reply",
            }
            for item in existing_tasks
        ):
            return FollowupEvaluation(False, "waiting_for_reply_exists")
        candidates = [
            item for item in triggers
            if item.due_at <= now and self._trigger_enabled(item, policy)
        ]
        fixed = self.evaluate(subject, policy, now=now, existing_tasks=existing_tasks)
        if fixed.eligible and fixed.due_at is not None:
            candidates.append(
                CaseFollowupTrigger(
                    trigger_type="fixed_cadence",
                    trigger_event_id=(
                        f"cadence:{policy.policy_id}:{fixed.due_at.isoformat()}"
                    ),
                    question_type="meaningful_progress",
                    due_at=fixed.due_at,
                    priority=100,
                )
            )
        if not candidates:
            return fixed if not fixed.eligible else FollowupEvaluation(False, "no_due_trigger")
        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (-item.priority, item.due_at, item.trigger_event_id),
            )
        )
        primary = ordered[0]
        return FollowupEvaluation(
            eligible=True,
            reason_code="merged_trigger_due" if len(ordered) > 1 else "event_trigger_due",
            trigger_type=primary.trigger_type,
            question_type=primary.question_type,
            due_at=primary.due_at,
            trigger_sources=ordered,
        )

    @staticmethod
    def _trigger_enabled(
        trigger: CaseFollowupTrigger,
        policy: CaseFollowupPolicySnapshot,
    ) -> bool:
        if not policy.enabled or not policy.event_triggers_enabled:
            return False
        return {
            "hearing_proximity": policy.hearing_reminders_enabled,
            "hearing_result": policy.hearing_reminders_enabled,
            "stage_transition": policy.stage_transition_enabled,
            "node_transition": policy.node_transition_enabled,
            "manual": True,
        }.get(trigger.trigger_type, False)


class TriggerPolicyMatrix:
    PLAINTIFF_TRANSITIONS = frozenset({
        ("拟诉", "诉讼中"),
        ("诉讼中", "执行中"),
        ("拟诉", "已结案"),
        ("诉讼中", "已结案"),
        ("执行中", "已结案"),
    })
    DEFENDANT_TRANSITIONS = frozenset({
        ("受理", "开庭"),
        ("开庭", "审结"),
        ("审结", "履行"),
        ("履行", "已结案"),
    })
    NODE_ALLOWLIST = frozenset({
        "已立案", "确定开庭日期", "开庭结束", "收到裁判文书", "进入上诉期",
        "提交执行申请", "执行立案", "财产查控", "执行和解", "部分履行",
        "履行完成", "终本", "恢复执行", "结案",
    })

    def from_committed_change(
        self,
        change: CommittedCaseLifecycleChange,
    ) -> tuple[CaseFollowupTrigger, ...]:
        if (
            change.receipt_status != "executed"
            or not change.actual_write
            or not change.receipt_id
        ):
            return ()
        triggers: list[CaseFollowupTrigger] = []
        allowed = (
            self.PLAINTIFF_TRANSITIONS
            if change.case_type == "plaintiff"
            else self.DEFENDANT_TRANSITIONS
            if change.case_type == "defendant"
            else frozenset()
        )
        if (change.from_stage, change.to_stage) in allowed:
            triggers.append(
                CaseFollowupTrigger(
                    trigger_type="stage_transition",
                    trigger_event_id=f"{change.receipt_id}:stage",
                    question_type="stage_transition_followup",
                    due_at=change.occurred_at,
                    priority=400,
                    facts={
                        "receipt_id": change.receipt_id,
                        "from_stage": change.from_stage,
                        "to_stage": change.to_stage,
                    },
                )
            )
        if change.node in self.NODE_ALLOWLIST:
            triggers.append(
                CaseFollowupTrigger(
                    trigger_type="node_transition",
                    trigger_event_id=f"{change.receipt_id}:node:{change.node}",
                    question_type="node_transition_followup",
                    due_at=change.occurred_at,
                    priority=300,
                    facts={"receipt_id": change.receipt_id, "node": change.node},
                )
            )
        return tuple(triggers)


def build_hearing_triggers(
    *,
    hearing_event_id: str,
    hearing_at: datetime,
    post_hearing_delay_hours: int = 4,
) -> tuple[CaseFollowupTrigger, ...]:
    if not hearing_event_id.strip():
        raise ValueError("hearing event id is required")
    if hearing_at.tzinfo is None:
        raise ValueError("hearing timestamp must be timezone-aware")
    if post_hearing_delay_hours <= 0:
        raise ValueError("post-hearing delay must be positive")
    hearing_date = hearing_at.date().isoformat()
    reminders = tuple(
        CaseFollowupTrigger(
            trigger_type="hearing_proximity",
            trigger_event_id=f"{hearing_event_id}:before-{days}d",
            question_type="hearing_readiness",
            due_at=hearing_at - timedelta(days=days),
            priority=700 + days,
            facts={
                "hearing_event_id": hearing_event_id,
                "hearing_date": hearing_date,
                "reminder_slot": f"before-{days}d",
            },
        )
        for days in (7, 3, 1)
    )
    post = CaseFollowupTrigger(
        trigger_type="hearing_result",
        trigger_event_id=f"{hearing_event_id}:after-{post_hearing_delay_hours}h",
        question_type="hearing_result",
        due_at=hearing_at + timedelta(hours=post_hearing_delay_hours),
        priority=750,
        facts={
            "hearing_event_id": hearing_event_id,
            "hearing_date": hearing_date,
            "reminder_slot": f"after-{post_hearing_delay_hours}h",
        },
    )
    return (*reminders, post)


class FollowupQuestionComposer:
    """Compose a question from evaluated facts without adding Case facts."""

    def compose(
        self,
        subject: CaseFollowupSubject,
        evaluation: FollowupEvaluation,
    ) -> str:
        if not evaluation.eligible or not evaluation.trigger_sources:
            raise ValueError("follow-up question requires an eligible evaluated trigger")
        primary = evaluation.trigger_sources[0]
        if primary.question_type == "hearing_readiness":
            hearing_date = str(primary.facts.get("hearing_date") or "").strip()
            if not hearing_date:
                raise ValueError("hearing follow-up requires a structured hearing date")
            return (
                f"“{subject.case_name}”将在 {hearing_date} 开庭。"
                "证据材料、授权手续、出庭人员以及与法院的沟通情况准备得怎么样？"
                "有需要协调的事项吗？"
            )
        if primary.question_type == "hearing_result":
            hearing_date = str(primary.facts.get("hearing_date") or "").strip()
            if not hearing_date:
                raise ValueError("hearing result follow-up requires a structured hearing date")
            return (
                f"“{subject.case_name}”在 {hearing_date} 的庭审已经结束。"
                "庭审结果、法院意见和下一步安排是什么？"
            )
        if primary.question_type == "meaningful_progress":
            return (
                f"“{subject.case_name}”已经到追问时间了。"
                "目前有没有新进展？下一步准备怎么推进？"
            )
        if primary.question_type == "stage_transition_followup":
            to_stage = str(primary.facts.get("to_stage") or subject.stage)
            return (
                f"“{subject.case_name}”已进入{to_stage}阶段。"
                "目前已经完成了哪些工作，下一步准备怎么推进？"
            )
        if primary.question_type == "node_transition_followup":
            node = str(primary.facts.get("node") or subject.node)
            return (
                f"“{subject.case_name}”的关键节点已更新为“{node}”。"
                "目前有什么需要记录的进展或下一步计划？"
            )
        raise ValueError("unsupported follow-up question type")


def build_followup_task_plan(
    subject: CaseFollowupSubject,
    evaluation: FollowupEvaluation,
    *,
    expires_at: datetime,
    policy_id: str = "",
) -> CaseFollowupTaskPlan:
    if not evaluation.eligible or evaluation.due_at is None or not evaluation.trigger_sources:
        raise ValueError("task plan requires one eligible follow-up evaluation")
    if expires_at <= evaluation.due_at:
        raise ValueError("follow-up task expiry must be after due time")
    trigger_ids = tuple(item.trigger_event_id for item in evaluation.trigger_sources)
    scope = (
        f"{subject.tenant_id}:{subject.case_id}:{subject.assigned_user_id}:"
        f"{evaluation.question_type}:{'|'.join(sorted(trigger_ids))}"
    )
    followup_id = str(uuid5(NAMESPACE_URL, f"case-followup:{scope}"))
    return CaseFollowupTaskPlan(
        followup_id=followup_id,
        tenant_id=subject.tenant_id,
        case_id=subject.case_id,
        assigned_user_id=subject.assigned_user_id,
        case_name=subject.case_name,
        case_type=subject.case_type,
        stage=subject.stage,
        node=subject.node,
        policy_id=policy_id,
        trigger_type=evaluation.trigger_type,
        trigger_event_ids=trigger_ids,
        trigger_sources=evaluation.trigger_sources,
        question_type=evaluation.question_type,
        priority=max(item.priority for item in evaluation.trigger_sources),
        due_at=evaluation.due_at,
        expires_at=expires_at,
        case_version=subject.case_version,
        question_text=FollowupQuestionComposer().compose(subject, evaluation),
        idempotency_key=f"case-followup:{followup_id}",
    )


def calculate_next_due_at(
    last_meaningful_progress_at: datetime,
    *,
    cadence_type: CadenceType,
    timezone_name: str,
    custom_interval_days: int | None = None,
    business_days_only: bool = False,
) -> datetime:
    if last_meaningful_progress_at.tzinfo is None:
        raise ValueError("meaningful progress timestamp must be timezone-aware")
    local = last_meaningful_progress_at.astimezone(ZoneInfo(timezone_name))
    if cadence_type == "daily":
        result = local + timedelta(days=1)
    elif cadence_type == "weekly":
        result = local + timedelta(days=7)
    elif cadence_type == "every_15_days":
        result = local + timedelta(days=15)
    elif cadence_type == "custom_interval":
        if custom_interval_days is None or custom_interval_days <= 0:
            raise ValueError("custom cadence requires positive interval days")
        result = local + timedelta(days=custom_interval_days)
    elif cadence_type == "monthly":
        year = local.year + (1 if local.month == 12 else 0)
        month = 1 if local.month == 12 else local.month + 1
        day = min(local.day, monthrange(year, month)[1])
        result = local.replace(year=year, month=month, day=day)
    else:
        raise ValueError("cadence does not define a fixed next due time")
    if business_days_only:
        while result.weekday() >= 5:
            result += timedelta(days=1)
    return result
