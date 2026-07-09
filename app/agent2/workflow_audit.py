from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.time import now_in_timezone
from app.workflows.intake import IncomingMessageEnvelope


async def create_agent2_workflow_audit_event(
    *,
    session: AsyncSession,
    user: Any,
    incoming: Any,
    settings: Any,
    envelope: IncomingMessageEnvelope,
    shadow: Any,
    mode: str,
    observe_only_log: bool,
) -> None:
    if not bool(getattr(settings, "shadow_memory_enabled", False)):
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        return

    route_observation = shadow.route_observation(envelope)
    gate_observation = shadow.gate_observation(envelope)
    confidence = _decimal_confidence(getattr(getattr(shadow, "route", None), "confidence", None))
    backend_action = "agent2_workflow_audit_observe" if observe_only_log else "agent2_workflow_audit_gate"
    try:
        from app.models import ReportInteractionEvent

        async with session.begin_nested():
            event = ReportInteractionEvent(
                user_id=user_id,
                report_id=None,
                dingtalk_user_id=str(getattr(incoming, "dingtalk_user_id", "") or ""),
                report_date=now_in_timezone(getattr(settings, "timezone", "Asia/Shanghai")).date(),
                message_text=str(getattr(incoming, "text", "") or ""),
                llm_decision_json={
                    "agent2": True,
                    "audit_stage": "observe_only" if observe_only_log else "gate",
                    "mode": mode,
                    "route": route_observation,
                    "gate": gate_observation,
                    "cognitive_decision": shadow.cognitive_decision.as_dict()
                    if getattr(shadow, "cognitive_decision", None)
                    else {},
                },
                backend_action=backend_action,
                before_snapshot_json={},
                after_snapshot_json={},
                correction_type="",
                correction_from="",
                correction_to="",
                confidence=confidence,
                asr_suspect_json={},
            )
            session.add(event)
            await session.flush()
    except Exception as exc:
        print(f"Agent2 workflow audit skipped: {exc}", flush=True)


def _decimal_confidence(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 4)))
    except (TypeError, ValueError):
        return None
