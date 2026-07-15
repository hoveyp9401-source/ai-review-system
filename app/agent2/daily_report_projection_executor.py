from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from app.agent2.operation_outcomes import OperationOutcome
from app.agent2.outcome_adapters import daily_execution_outcomes
from app.agent2.report_projection_policy import ReportProjectionDecision
from app.agent2.typed_daily_commands import TypedDailyCommand
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    build_typed_daily_snapshot,
    execute_typed_agent2_daily_commands,
)
from app.models import Agent2DailyCommandReceipt
from app.repositories import get_report


class DailyReportProjectionExecutor:
    """The Report Domain adapter for derived Case facts.

    It speaks only typed Report commands and returns the same OperationOutcome
    contract used by direct daily-report turns.
    """

    def __init__(self, *, session, user, settings, tenant_id: str):
        self._session = session
        self._user = user
        self._settings = settings
        self._tenant_id = tenant_id

    async def execute_report_projection(
        self,
        decision: ReportProjectionDecision,
        request_id: str,
    ) -> OperationOutcome:
        if decision.report_type != "daily" or decision.section not in {
            "today_work", "tomorrow_plan"
        }:
            raise ValueError("unsupported Report projection target")
        report = await get_report(
            self._session, self._user.id, decision.report_date
        )
        snapshot = build_typed_daily_snapshot(
            user=self._user, report_date=decision.report_date, report=report
        )
        decision_uuid = uuid5(NAMESPACE_URL, f"projection-decision:{decision.decision_id}")
        sub_decision_id = uuid5(NAMESPACE_URL, f"projection-sub:{request_id}")
        command = TypedDailyCommand(
            command_id=uuid5(NAMESPACE_URL, f"projection-command:{request_id}"),
            decision_id=decision_uuid,
            sub_decision_id=sub_decision_id,
            command_type="append_item",
            report_id=snapshot.report_id,
            report_version=snapshot.version,
            target_item_ids=(),
            patch={"field": decision.section, "items": [decision.normalized_fact]},
            idempotency_key=(
                f"{decision.source_turn_id}:daily:case-projection:{request_id}:"
                f"{decision.section}"
            ),
        )
        result = await execute_typed_agent2_daily_commands(
            self._session,
            user=self._user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=decision.report_date,
                source="case_followup_report_projection",
                source_text_hash=sha256(
                    decision.normalized_fact.encode("utf-8")
                ).hexdigest(),
                tenant_id=self._tenant_id,
            ),
            settings=self._settings,
            execution_authority="derived_committed_receipt",
        )
        outcome = daily_execution_outcomes(
            result, source_turn_id=decision.source_turn_id
        )[0]
        receipt = await self._session.scalar(
            select(Agent2DailyCommandReceipt).where(
                Agent2DailyCommandReceipt.tenant_id == self._tenant_id,
                Agent2DailyCommandReceipt.idempotency_key == command.idempotency_key,
            )
        )
        report_item_id = _projected_item_id(
            receipt, decision.section, decision.normalized_fact
        )
        return replace(
            outcome,
            tenant_id=self._tenant_id,
            user_id=str(self._user.id),
            idempotency_key=command.idempotency_key,
            user_visible_snapshot={
                **outcome.user_visible_snapshot,
                "report_item_id": report_item_id,
            },
        )


def _projected_item_id(receipt, section: str, value: str) -> str:
    if receipt is None:
        raise RuntimeError("Report projection receipt is missing")
    after = dict(receipt.after_json or {})
    before = dict(receipt.before_json or {})
    after_ids = tuple((after.get("item_ids") or {}).get(section) or ())
    before_ids = set((before.get("item_ids") or {}).get(section) or ())
    new_ids = tuple(item for item in after_ids if item not in before_ids)
    if len(new_ids) == 1:
        return str(new_ids[0])
    values = tuple(after.get(section) or ())
    matches = tuple(
        str(after_ids[index])
        for index, item in enumerate(values)
        if item == value and index < len(after_ids)
    )
    if len(matches) != 1:
        raise RuntimeError("Report projection item cannot be identified uniquely")
    return matches[0]
