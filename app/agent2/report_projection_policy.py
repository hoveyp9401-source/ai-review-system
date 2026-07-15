from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True)
class CaseFactExtraction:
    case_id: str
    actor_user_id: str
    raw_text: str
    normalized_fact: str
    factual_progress: tuple[str, ...]
    completed_actions: tuple[str, ...]
    next_actions: tuple[str, ...]
    action_time_scope: Literal["today", "future", "unknown"]
    report_preference: Literal["automatic", "case_only", "ask"]
    confidence: float
    evidence_spans: tuple[tuple[int, int], ...]
    current_status: str = ""
    time_anchors: tuple[str, ...] = ()
    hearing_readiness: str = ""
    blocking_issues: tuple[str, ...] = ()
    requested_snooze: str = ""
    normalization_version: str = "case-fact.v1"


@dataclass(frozen=True)
class ReportProjectionContext:
    tenant_id: str
    user_id: str
    source_turn_id: str
    report_date: date
    report_exists: bool
    report_writable: bool
    duplicate_projection_id: str
    automatic_projection_enabled: bool
    high_confidence_threshold: float


@dataclass(frozen=True)
class ReportProjectionDecision:
    decision_id: str
    eligible: bool
    projection_mode: str
    report_type: str
    section: str
    report_date: date
    normalized_fact: str
    source_case_id: str
    source_progress_id: str
    source_followup_id: str
    source_turn_id: str
    reason_code: str
    confidence: float
    duplicate_of: str
    policy_version: str


class ReportProjectionPolicy:
    POLICY_VERSION = "case-report-projection.v1"

    def decide(
        self,
        fact: CaseFactExtraction,
        context: ReportProjectionContext,
        *,
        source_progress_id: str = "",
        source_followup_id: str = "",
    ) -> ReportProjectionDecision:
        if fact.report_preference == "case_only":
            return self._decision(
                fact, context, eligible=False, projection_mode="none", section="",
                reason_code="user_opted_out", source_progress_id=source_progress_id,
                source_followup_id=source_followup_id,
            )
        if not fact.case_id:
            return self._blocked(fact, context, "ambiguous_case", source_progress_id, source_followup_id)
        if fact.actor_user_id != context.user_id:
            return self._blocked(fact, context, "ambiguous_actor", source_progress_id, source_followup_id)
        if fact.factual_progress and not fact.completed_actions and not fact.next_actions:
            return self._decision(
                fact,
                context,
                eligible=False,
                projection_mode="none",
                section="",
                reason_code="status_only",
                source_progress_id=source_progress_id,
                source_followup_id=source_followup_id,
            )
        if not fact.completed_actions and not fact.next_actions:
            return self._blocked(fact, context, "no_business_action", source_progress_id, source_followup_id)
        if fact.action_time_scope == "unknown":
            return self._blocked(fact, context, "insufficient_time_anchor", source_progress_id, source_followup_id)
        if context.duplicate_projection_id:
            return self._blocked(fact, context, "duplicate_projection", source_progress_id, source_followup_id)
        if not context.report_exists:
            return self._blocked(fact, context, "report_not_found", source_progress_id, source_followup_id)
        if not context.report_writable:
            return self._blocked(fact, context, "report_closed", source_progress_id, source_followup_id)
        if not context.automatic_projection_enabled:
            return self._blocked(fact, context, "policy_blocked", source_progress_id, source_followup_id)
        if (
            fact.report_preference == "ask"
            or fact.confidence < context.high_confidence_threshold
        ):
            confirmation_section = (
                "today_work"
                if fact.completed_actions and fact.action_time_scope == "today"
                else "tomorrow_plan"
                if fact.next_actions and fact.action_time_scope == "future"
                else ""
            )
            if not confirmation_section:
                return self._blocked(
                    fact, context, "policy_blocked",
                    source_progress_id, source_followup_id,
                )
            return self._decision(
                fact, context, eligible=False,
                projection_mode="confirmation_required", section=confirmation_section,
                reason_code="policy_blocked",
                source_progress_id=source_progress_id,
                source_followup_id=source_followup_id,
            )
        if (
            fact.case_id
            and fact.actor_user_id == context.user_id
            and fact.completed_actions
            and fact.action_time_scope == "today"
            and fact.report_preference == "automatic"
            and fact.confidence >= context.high_confidence_threshold
            and context.automatic_projection_enabled
            and context.report_exists
            and context.report_writable
            and not context.duplicate_projection_id
        ):
            return self._decision(
                fact,
                context,
                eligible=True,
                projection_mode="automatic",
                section="today_work",
                reason_code="completed_work_today",
                source_progress_id=source_progress_id,
                source_followup_id=source_followup_id,
            )
        if (
            fact.case_id
            and fact.actor_user_id == context.user_id
            and fact.next_actions
            and fact.action_time_scope == "future"
            and fact.report_preference == "automatic"
            and fact.confidence >= context.high_confidence_threshold
            and context.automatic_projection_enabled
            and context.report_exists
            and context.report_writable
            and not context.duplicate_projection_id
        ):
            return self._decision(
                fact,
                context,
                eligible=True,
                projection_mode="automatic",
                section="tomorrow_plan",
                reason_code="future_work_plan",
                source_progress_id=source_progress_id,
                source_followup_id=source_followup_id,
            )
        return self._decision(
            fact,
            context,
            eligible=False,
            projection_mode="none",
            section="",
            reason_code="policy_blocked",
            source_progress_id=source_progress_id,
            source_followup_id=source_followup_id,
        )

    def _blocked(
        self,
        fact: CaseFactExtraction,
        context: ReportProjectionContext,
        reason_code: str,
        source_progress_id: str,
        source_followup_id: str,
    ) -> ReportProjectionDecision:
        return self._decision(
            fact, context, eligible=False, projection_mode="none", section="",
            reason_code=reason_code, source_progress_id=source_progress_id,
            source_followup_id=source_followup_id,
        )

    def _decision(
        self,
        fact: CaseFactExtraction,
        context: ReportProjectionContext,
        *,
        eligible: bool,
        projection_mode: str,
        section: str,
        reason_code: str,
        source_progress_id: str,
        source_followup_id: str,
    ) -> ReportProjectionDecision:
        decision_id = str(
            uuid5(
                NAMESPACE_URL,
                f"report-projection:{context.tenant_id}:{context.user_id}:"
                f"{fact.case_id}:{context.source_turn_id}:{reason_code}:{section}",
            )
        )
        return ReportProjectionDecision(
            decision_id=decision_id,
            eligible=eligible,
            projection_mode=projection_mode,
            report_type="daily",
            section=section,
            report_date=context.report_date,
            normalized_fact=fact.normalized_fact,
            source_case_id=fact.case_id,
            source_progress_id=source_progress_id,
            source_followup_id=source_followup_id,
            source_turn_id=context.source_turn_id,
            reason_code=reason_code,
            confidence=fact.confidence,
            duplicate_of=context.duplicate_projection_id,
            policy_version=self.POLICY_VERSION,
        )
