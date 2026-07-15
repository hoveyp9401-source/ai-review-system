from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from app.agent2.operation_outcomes import OperationOutcome
from app.agent2.outcome_adapters import daily_execution_outcomes
from app.agent2.report_projection_corrections import ProjectionCorrectionPlan
from app.agent2.typed_daily_commands import (
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    build_typed_daily_snapshot,
    execute_typed_agent2_daily_commands,
)
from app.models import DailyReport


@dataclass(frozen=True)
class DailyProjectionCorrectionExecution:
    outcome: OperationOutcome
    report_item_id_after: str


class DailyReportProjectionCorrectionExecutor:
    def __init__(self, *, session, user, settings, tenant_id: str):
        self._session = session
        self._user = user
        self._settings = settings
        self._tenant_id = tenant_id

    async def execute(
        self,
        plan: ProjectionCorrectionPlan,
        *,
        source_message_id: str,
        idempotency_key: str,
    ) -> DailyProjectionCorrectionExecution:
        if not plan.allowed or plan.report_id == "" or plan.report_item_id == "":
            raise ValueError("projection correction requires an allowed exact plan")
        report = await self._session.get(DailyReport, _uuid(plan.report_id))
        if report is None or report.user_id != self._user.id:
            raise ValueError("projection report is not visible to the user")
        snapshot = build_typed_daily_snapshot(
            user=self._user, report_date=report.report_date, report=report
        )
        locations = [
            (field, index)
            for field, item_ids in snapshot.item_ids.items()
            for index, item_id in enumerate(item_ids)
            if item_id == plan.report_item_id
        ]
        if len(locations) != 1:
            raise ValueError("projection report item is no longer unique")
        source_field, source_index = locations[0]
        source_text = getattr(snapshot, source_field)[source_index]
        base = uuid5(NAMESPACE_URL, f"projection-correction:{idempotency_key}")
        if plan.operation == "remove":
            commands = (
                TypedDailyCommand(
                    command_id=uuid5(base, "remove"), decision_id=base,
                    sub_decision_id=uuid5(base, "remove-sub"),
                    command_type="delete_item", report_id=snapshot.report_id,
                    report_version=snapshot.version,
                    target_item_ids=(plan.report_item_id,), patch={},
                    idempotency_key=f"{idempotency_key}:remove",
                ),
            )
            report_item_after = ""
        elif plan.operation == "replace_text":
            commands = (
                TypedDailyCommand(
                    command_id=uuid5(base, "replace"), decision_id=base,
                    sub_decision_id=uuid5(base, "replace-sub"),
                    command_type="edit_item", report_id=snapshot.report_id,
                    report_version=snapshot.version,
                    target_item_ids=(plan.report_item_id,),
                    patch={"replacement": plan.replacement_text},
                    idempotency_key=f"{idempotency_key}:replace",
                ),
            )
            report_item_after = plan.report_item_id
        elif plan.operation == "move_section":
            if plan.target_section == source_field:
                raise ValueError("projection is already in the requested report section")
            delete = TypedDailyCommand(
                command_id=uuid5(base, "move-delete"), decision_id=base,
                sub_decision_id=uuid5(base, "move-delete-sub"),
                command_type="delete_item", report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(plan.report_item_id,), patch={},
                idempotency_key=f"{idempotency_key}:move-delete",
            )
            after_delete = execute_typed_daily_command(
                delete, snapshot=snapshot, actor_user_id=self._user.id
            ).after
            append = TypedDailyCommand(
                command_id=uuid5(base, "move-append"), decision_id=base,
                sub_decision_id=uuid5(base, "move-append-sub"),
                command_type="append_item", report_id=snapshot.report_id,
                report_version=after_delete.version, target_item_ids=(),
                patch={"field": plan.target_section, "items": [source_text]},
                idempotency_key=f"{idempotency_key}:move-append",
            )
            predicted = execute_typed_daily_command(
                append, snapshot=after_delete, actor_user_id=self._user.id
            ).after
            matches = tuple(
                item_id
                for index, value in enumerate(getattr(predicted, plan.target_section))
                if value == source_text
                for item_id in (predicted.item_ids[plan.target_section][index],)
            )
            if len(matches) != 1:
                raise ValueError("moved projection target is not unique")
            commands = (delete, append)
            report_item_after = matches[0]
        else:
            raise ValueError("unsupported projection correction operation")
        result = await execute_typed_agent2_daily_commands(
            self._session, user=self._user, commands=commands,
            execution_context=TypedDailyExecutionContext(
                report_date=report.report_date,
                source="case_report_projection_correction",
                source_text_hash=sha256(source_message_id.encode("utf-8")).hexdigest(),
                tenant_id=self._tenant_id,
            ), settings=self._settings,
            execution_authority="derived_committed_receipt",
        )
        outcome = daily_execution_outcomes(
            result, source_turn_id=source_message_id
        )[0]
        outcome = replace(
            outcome,
            operation="delete" if plan.operation == "remove" else "update",
        )
        return DailyProjectionCorrectionExecution(
            replace(
                outcome, tenant_id=self._tenant_id,
                user_id=str(self._user.id), idempotency_key=idempotency_key,
            ),
            report_item_after,
        )


def _uuid(value: str):
    from uuid import UUID

    return UUID(value)
