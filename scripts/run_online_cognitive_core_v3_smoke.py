from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select

from app.agent2.conversation_state import (
    BoundPending,
    ConversationEntity,
    ConversationGoal,
    ConversationState,
    RecentContextFrame,
)
from app.agent2.conversation_state_store import SQLAlchemyConversationStateStore
from app.db import AsyncSessionLocal
from app.models import (
    Agent2ConversationState,
    DailyReport,
    ReportInteractionEvent,
    Team,
    User,
    WebhookEvent,
)


TZ = ZoneInfo("Asia/Shanghai")
REPORT_DATE = date(2026, 7, 10)


class OnlineV3Smoke:
    def __init__(self, *, base_url: str):
        self.run_id = uuid4().hex[:10]
        self.base_url = base_url
        self.team_code = f"__cognitive_v3_online_team__{self.run_id}"
        self.user_prefix = f"__cognitive_v3_online_user__{self.run_id}"
        self.user_ids: list[str] = []

    async def ensure_user(self, suffix: str) -> User:
        async with AsyncSessionLocal() as session:
            team = (
                await session.execute(select(Team).where(Team.code == self.team_code))
            ).scalar_one_or_none()
            if team is None:
                team = Team(
                    code=self.team_code,
                    name="Cognitive v3 Online Smoke",
                    department_name="Legal",
                )
                session.add(team)
                await session.flush()
            dingtalk_user_id = f"{self.user_prefix}_{suffix}"
            user = User(
                dingtalk_user_id=dingtalk_user_id,
                name=f"V3 Smoke {suffix}",
                team_id=team.id,
                role="member",
                timezone="Asia/Shanghai",
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            self.user_ids.append(str(user.id))
            return user

    async def seed_report(
        self,
        user: User,
        *,
        today_work: list[str],
        problems: list[str] | None = None,
        tomorrow_plan: list[str] | None = None,
    ) -> None:
        problems = list(problems or [])
        tomorrow_plan = list(tomorrow_plan or [])
        async with AsyncSessionLocal() as session:
            managed = await session.merge(user)
            session.add(
                DailyReport(
                    user_id=managed.id,
                    team_id=managed.team_id,
                    report_date=REPORT_DATE,
                    today_work=list(today_work),
                    problems=problems,
                    tomorrow_plan=tomorrow_plan,
                    emotion="",
                    raw_input="v3 online smoke seed",
                    input_fragments=[],
                    section_status={
                        "_draft_item_ids": {
                            "today_work": [f"tw-{index}" for index, _ in enumerate(today_work, start=1)],
                            "problems": [f"pb-{index}" for index, _ in enumerate(problems, start=1)],
                            "tomorrow_plan": [f"tp-{index}" for index, _ in enumerate(tomorrow_plan, start=1)],
                        },
                        "_agent2_report_version": 0,
                    },
                    completeness_score=Decimal("1.0"),
                    status="collecting",
                    confirmation_type="none",
                    confirmed_by_user=False,
                    last_modified_by_user=False,
                    source="cognitive_v3_online_smoke_seed",
                    llm_model="smoke",
                    llm_payload={},
                )
            )
            await session.commit()

    async def post(self, client: httpx.AsyncClient, user: User, text: str, key: str, conversation_id: str) -> dict:
        response = await client.post(
            "/reports/manual",
            json={
                "dingtalk_user_id": user.dingtalk_user_id,
                "raw_input": text,
                "source": "agent2_cognitive_v3_online_smoke",
                "report_date": REPORT_DATE.isoformat(),
                "idempotency_key": f"{self.run_id}:{key}",
                "conversation_id": conversation_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def report(self, user: User) -> DailyReport | None:
        async with AsyncSessionLocal() as session:
            return (
                await session.execute(
                    select(DailyReport).where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date == REPORT_DATE,
                    )
                )
            ).scalar_one_or_none()

    async def state(self, user: User, conversation_id: str) -> ConversationState:
        async with AsyncSessionLocal() as session:
            return await SQLAlchemyConversationStateStore(session).load(
                user_id=str(user.id),
                conversation_id=conversation_id,
            )

    async def latest_v3_audit(self, user: User) -> dict[str, Any]:
        async with AsyncSessionLocal() as session:
            event = (
                await session.execute(
                    select(ReportInteractionEvent)
                    .where(
                        ReportInteractionEvent.user_id == user.id,
                        ReportInteractionEvent.backend_action == "agent2_cognitive_v3_plan",
                    )
                    .order_by(ReportInteractionEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            return dict(event.llm_decision_json or {})

    async def seed_case_context(self, user: User, conversation_id: str) -> None:
        now = datetime.now(TZ)
        state = ConversationState(
            user_id=str(user.id),
            conversation_id=conversation_id,
            version=1,
            current_goal=ConversationGoal(intent="case_discussion", source_context_id="seed-case-context"),
            recent_context=(
                RecentContextFrame(
                    context_id="seed-case-context",
                    message_id="seed-case-message",
                    intents=("case_discussion",),
                    entity_ids=("seed-case-fact",),
                    summary="法院认为证据链完整",
                    occurred_at=now - timedelta(minutes=1),
                ),
            ),
        )
        async with AsyncSessionLocal() as session:
            await SQLAlchemyConversationStateStore(session).save(state, expected_version=0)
            await session.commit()

    async def seed_monthly_pending(self, user: User, conversation_id: str) -> None:
        now = datetime.now(TZ)
        entity = ConversationEntity(
            entity_id="online-monthly-report",
            entity_type="monthly_report",
            value="7月月报",
            confidence=1.0,
        )
        state = ConversationState(
            user_id=str(user.id),
            conversation_id=conversation_id,
            version=1,
            current_entities=(entity,),
            pending=(
                BoundPending(
                    pending_id="online-monthly-pending",
                    user_id=str(user.id),
                    conversation_id=conversation_id,
                    intent="monthly_submit",
                    action="confirm_monthly_report",
                    entity_ids=(entity.entity_id,),
                    context_id="online-monthly-context",
                    created_at=now - timedelta(minutes=1),
                    expires_at=now + timedelta(minutes=10),
                ),
            ),
        )
        async with AsyncSessionLocal() as session:
            await SQLAlchemyConversationStateStore(session).save(state, expected_version=0)
            await session.commit()

    async def cleanup(self) -> None:
        async with AsyncSessionLocal() as session:
            users = (
                await session.execute(select(User).where(User.dingtalk_user_id.like(f"{self.user_prefix}%")))
            ).scalars().all()
            user_ids = [user.id for user in users]
            if user_ids:
                await session.execute(delete(DailyReport).where(DailyReport.user_id.in_(user_ids)))
                await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id.in_(user_ids)))
            if self.user_ids:
                await session.execute(
                    delete(Agent2ConversationState).where(Agent2ConversationState.user_key.in_(self.user_ids))
                )
            await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id.like(f"{self.user_prefix}%")))
            for user in users:
                await session.delete(user)
            team = (await session.execute(select(Team).where(Team.code == self.team_code))).scalar_one_or_none()
            if team is not None:
                await session.delete(team)
            await session.commit()

    async def run(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            chat_user = await self.ensure_user("chat")
            chat_id = f"conversation-chat-{self.run_id}"
            await self.post(client, chat_user, "最近有点累，随便聊聊", "chat-1", chat_id)
            await self.post(client, chat_user, "今天去了法院", "chat-2", chat_id)
            await self.post(client, chat_user, "继续聊刚才的话题", "chat-3", chat_id)
            chat_report = await self.report(chat_user)
            chat_state = await self.state(chat_user, chat_id)
            assert chat_report is not None and sum("法院" in item for item in chat_report.today_work) == 1
            assert chat_state.current_goal is not None and chat_state.current_goal.intent == "chat"
            results.append({"name": "chat_with_daily", "status": "PASS"})

            mixed_user = await self.ensure_user("mixed")
            mixed_id = f"conversation-mixed-{self.run_id}"
            await self.post(client, mixed_user, "今天完成合同审核，另外王总那个案件风险怎么看", "mixed", mixed_id)
            mixed_report = await self.report(mixed_user)
            mixed_audit = await self.latest_v3_audit(mixed_user)
            assert mixed_report is not None and any("合同审核" in item for item in mixed_report.today_work)
            assert {"daily_append", "case_query"} <= set(mixed_audit["cognitive_decision"]["intents"])
            assert [item["command_type"] for item in mixed_audit["command_plan"]["business_commands"]] == ["query_case_risk"]
            results.append({"name": "daily_and_case_query", "status": "PASS"})

            context_user = await self.ensure_user("context")
            context_id = f"conversation-context-{self.run_id}"
            await self.seed_case_context(context_user, context_id)
            await self.post(client, context_user, "刚才那个补充到今天日报", "context", context_id)
            context_report = await self.report(context_user)
            assert context_report is not None and "法院认为证据链完整" in context_report.today_work
            results.append({"name": "case_context_to_daily", "status": "PASS"})

            travel_user = await self.ensure_user("travel")
            travel_id = f"conversation-travel-{self.run_id}"
            await self.post(client, travel_user, "明天上海开庭，帮我记一下，然后看看这个案件有没有风险", "travel", travel_id)
            travel_report = await self.report(travel_user)
            travel_audit = await self.latest_v3_audit(travel_user)
            assert travel_report is not None and any("上海" in item for item in travel_report.tomorrow_plan)
            assert {"travel_event", "daily_append", "case_query"} <= set(travel_audit["cognitive_decision"]["intents"])
            business_types = {item["command_type"] for item in travel_audit["command_plan"]["business_commands"]}
            assert business_types == {"record_travel_candidate", "query_case_risk"}
            results.append({"name": "travel_daily_case", "status": "PASS"})

            pending_user = await self.ensure_user("pending")
            pending_id = f"conversation-pending-{self.run_id}"
            await self.seed_report(
                pending_user,
                today_work=["完成合同审核"],
                problems=["暂无"],
                tomorrow_plan=["继续跟进"],
            )
            await self.seed_monthly_pending(pending_user, pending_id)
            pending_response = await self.post(client, pending_user, "提交日报", "pending", pending_id)
            pending_report = await self.report(pending_user)
            pending_audit = await self.latest_v3_audit(pending_user)
            print(
                "ONLINE_COGNITIVE_V3_PENDING "
                + json.dumps(
                    {
                        "response": pending_response,
                        "report_status": pending_report.status if pending_report is not None else "missing",
                        "decision": pending_audit.get("cognitive_decision"),
                        "command_plan": pending_audit.get("command_plan"),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
            assert pending_report is not None and pending_report.status == "completed"
            assert [item["command_type"] for item in pending_audit["command_plan"]["daily_commands"]] == ["submit_report"]
            assert pending_audit["command_plan"]["business_commands"] == []
            results.append({"name": "daily_submit_with_monthly_pending", "status": "PASS"})
        return results


async def main_async(*, base_url: str, output: Path) -> int:
    smoke = OnlineV3Smoke(base_url=base_url)
    try:
        results = await smoke.run()
        payload = {"summary": {"pass": len(results), "fail": 0, "total": len(results)}, "results": results}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload["summary"], ensure_ascii=False))
        print(f"OUTPUT {output}")
        return 0
    finally:
        await smoke.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="outputs/online_cognitive_core_v3_smoke.json")
    args = parser.parse_args()
    return asyncio.run(main_async(base_url=args.base_url, output=Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
