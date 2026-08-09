from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.canary_config import canary_prompt_sha256
from app.agent2.tool_calling.canary_service import (
    process_tool_call_canary_ingress,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.legal_daily_dashboard.chat_query import (
    ManagedDailyQuery,
    ManagedDailyQueryRequest,
)
from app.legal_daily_dashboard.domain import DashboardActor
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository
from app.llm.client import LLMClient
from app.models import Agent2ConversationState, DailyReport, User, WebhookEvent
from app.services.management_daily_briefing import (
    ManagementDailyBriefingService,
)


REPORT_DATE = date(2026, 8, 3)
PANG_USER_ID = "222b1eeb-4faa-40cf-a193-e1892c9377b0"
FORMAL_TEAM_ID = "783ac5ac-527a-40b0-9a3c-fb53d8d4f951"
LEGACY_TEAM_CODE = "monthly-admin"
FORMAL_TEAM_CODE = "team-01"
UTTERANCES = (
    "查看昨天综合部的人的日报",
    "看一下昨天综合管理部所有人的日报",
    "昨天综合部成员的日报发我看看",
)


async def _counts(session) -> dict[str, int]:
    return {
        "receipts": int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id))
            )
            or 0
        ),
        "conversation_states": int(
            await session.scalar(
                select(func.count(Agent2ConversationState.id))
            )
            or 0
        ),
        "webhook_events": int(
            await session.scalar(select(func.count(WebhookEvent.id))) or 0
        ),
        "daily_reports": int(
            await session.scalar(select(func.count(DailyReport.id))) or 0
        ),
    }


async def _receipts(session, source_message_id: str):
    return list(
        (
            await session.scalars(
                select(ToolCallCanaryReceipt)
                .where(
                    ToolCallCanaryReceipt.source_message_id
                    == source_message_id
                )
                .order_by(ToolCallCanaryReceipt.created_at)
            )
        ).all()
    )


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id).strip()
    now = datetime(
        2026,
        8,
        4,
        10,
        43,
        tzinfo=ZoneInfo(settings.timezone),
    )
    llm_client = LLMClient(settings)

    async with AsyncSessionLocal() as baseline_session:
        baseline = await _counts(baseline_session)

    output: dict[str, object] = {
        "report_date": REPORT_DATE.isoformat(),
        "baseline": baseline,
    }
    async with AsyncSessionLocal() as session:
        repository = SqlDashboardRepository(session)
        member_teams = await repository.list_member_teams(
            tenant_id=tenant_id,
            on_date=REPORT_DATE,
        )
        matching_teams = [
            team for team in member_teams if team.name == "综合管理部"
        ]
        if len(matching_teams) != 1:
            raise AssertionError(
                [(team.code, team.name) for team in matching_teams]
            )
        if matching_teams[0].code != FORMAL_TEAM_CODE:
            raise AssertionError(matching_teams[0])
        if any(team.code == LEGACY_TEAM_CODE for team in member_teams):
            raise AssertionError("expired legacy team remains query-visible")

        briefing_teams = await repository.list_teams(
            tenant_id=tenant_id,
            on_date=REPORT_DATE,
        )
        if len(briefing_teams) != 7:
            raise AssertionError(
                [(team.code, team.name) for team in briefing_teams]
            )
        if any(team.code == LEGACY_TEAM_CODE for team in briefing_teams):
            raise AssertionError("expired legacy team remains briefing-visible")

        direct_result = await ManagedDailyQuery(repository).execute(
            actor=DashboardActor(
                tenant_id=tenant_id,
                user_id=PANG_USER_ID,
            ),
            request=ManagedDailyQueryRequest(
                view="team_reports",
                report_date=REPORT_DATE,
                team_name="综合管理部",
            ),
            now=now,
        )
        direct_text = json.dumps(direct_result, ensure_ascii=False)
        if LEGACY_TEAM_CODE in direct_text or FORMAL_TEAM_CODE in direct_text:
            raise AssertionError(direct_result)
        if len(direct_result.get("members") or []) != 11:
            raise AssertionError(direct_result)

        briefing = await ManagementDailyBriefingService(settings).build(
            session,
            REPORT_DATE,
        )
        briefing_text = json.dumps(briefing, ensure_ascii=False, default=str)
        if LEGACY_TEAM_CODE in briefing_text:
            raise AssertionError("legacy team leaked into management briefing")
        team_message_ids = {
            str(item.get("team_id") or "")
            for item in briefing.get("team_messages") or []
        }
        if FORMAL_TEAM_ID not in team_message_ids:
            raise AssertionError(sorted(team_message_ids))

        pang = await session.scalar(
            select(User).where(User.id == PANG_USER_ID)
        )
        if pang is None:
            raise AssertionError("Pang user not found")
        control = await session.scalar(
            select(ToolCallCanaryControl).where(
                ToolCallCanaryControl.user_id == PANG_USER_ID
            )
        )
        if control is None or not control.enabled:
            raise AssertionError("Pang canary control is not enabled")
        control.registry_digest = runtime_registry_contract_digest(settings)
        control.prompt_sha256 = canary_prompt_sha256()
        await session.flush()

        variants = []
        for utterance in UTTERANCES:
            source_message_id = f"rollback-team-date-{uuid4()}"
            outcome = await process_tool_call_canary_ingress(
                session,
                user=pang,
                dingtalk_user_id=pang.dingtalk_user_id,
                user_text=utterance,
                source_channel="rollback_smoke",
                conversation_id=f"rollback-team-date-conv-{uuid4()}",
                source_message_id=source_message_id,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            await session.flush()
            receipts = await _receipts(session, source_message_id)
            query_receipts = [
                receipt
                for receipt in receipts
                if receipt.tool_name == "query_managed_daily_reports"
            ]
            if not query_receipts:
                raise AssertionError((utterance, outcome.message, receipts))
            if any(receipt.status != "success" for receipt in query_receipts):
                raise AssertionError(
                    [
                        (receipt.status, receipt.error_code)
                        for receipt in query_receipts
                    ]
                )
            if outcome.actual_write:
                raise AssertionError((utterance, "unexpected write"))
            serialized = json.dumps(
                [receipt.safe_user_facts for receipt in query_receipts],
                ensure_ascii=False,
                default=str,
            )
            combined = f"{outcome.message}\n{serialized}"
            forbidden = (
                LEGACY_TEAM_CODE,
                FORMAL_TEAM_CODE,
                "二选一",
                "要查看哪个",
                "请选择哪个",
            )
            if any(marker in combined for marker in forbidden):
                raise AssertionError((utterance, combined))
            if "综合管理部" not in combined:
                raise AssertionError((utterance, combined))
            variants.append(
                {
                    "utterance": utterance,
                    "message_preview": outcome.message[:160],
                    "message_length": len(outcome.message),
                    "receipt_statuses": [
                        receipt.status for receipt in query_receipts
                    ],
                }
            )

        output.update(
            {
                "query_visible_team_codes": [
                    team.code for team in member_teams
                ],
                "briefing_visible_team_codes": [
                    team.code for team in briefing_teams
                ],
                "direct_team_member_count": len(
                    direct_result.get("members") or []
                ),
                "variants": variants,
            }
        )
        await session.rollback()

    async with AsyncSessionLocal() as final_session:
        final = await _counts(final_session)
    if final != baseline:
        raise AssertionError({"baseline": baseline, "final": final})
    output["final"] = final
    output["rolled_back"] = True
    print(json.dumps(output, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
