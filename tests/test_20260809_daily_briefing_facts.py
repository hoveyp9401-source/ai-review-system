from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from app.agent2.daily_briefing_fact_query import (
    DailyBriefingFactEvent,
    DailyBriefingFactQuery,
    DailyBriefingFactQueryRequest,
    DailyBriefingFactSource,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.services.management_daily_briefing import (
    BriefingRecipient,
    build_management_daily_briefings,
)
from app.scheduler.runner import _send_daily_briefings


REPORT_DATE = date(2026, 8, 7)
GENERATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)


class _Repository:
    def __init__(self, source: DailyBriefingFactSource) -> None:
        self.source = source
        self.calls: list[tuple[str, date]] = []

    async def load_source(
        self,
        *,
        tenant_id: str,
        report_date: date,
    ) -> DailyBriefingFactSource:
        self.calls.append((tenant_id, report_date))
        return self.source


def _member(
    ref: str,
    name: str,
    *,
    team_ref: str = "team-1",
    team_name: str = "综合管理部",
) -> MemberRecord:
    return MemberRecord(
        ref=ref,
        name=name,
        team_ref=team_ref,
        team_name=team_name,
        department_name="法务合约中心",
    )


def _report(
    *,
    member_ref: str,
    status: str = "completed",
    submitted_at: datetime | None = None,
) -> DailyReportRecord:
    return DailyReportRecord(
        ref=f"report-{member_ref}",
        member_ref=member_ref,
        team_ref="team-1",
        report_date=REPORT_DATE,
        status=status,
        confirmation_type="user_confirmed",
        confirmed_by_user=True,
        submitted_at=submitted_at,
    )


def test_management_briefing_carries_the_exact_generation_snapshot() -> None:
    members = (
        _member("member-submitted", "甲成员"),
        _member("member-missing", "乙成员"),
    )
    records = DashboardRecords(
        members=members,
        obligations=tuple(
            SubmissionObligation(
                member_ref=member.ref,
                team_ref=member.team_ref,
                report_date=REPORT_DATE,
                required=True,
                reason="正常工作日",
                deadline_at=GENERATED_AT,
                data_complete=True,
            )
            for member in members
        ),
        reports=(
            _report(
                member_ref="member-submitted",
                submitted_at=GENERATED_AT,
            ),
        ),
    )
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=GENERATED_AT,
        teams=(
            TeamRecord(
                ref="team-1",
                name="综合管理部",
                department_name="法务合约中心",
            ),
        ),
        records=records,
        recipients=(
            BriefingRecipient(
                id="lead-1",
                name="负责人",
                dingtalk_user_id="ding-lead-1",
                role="team_lead",
                team_ref="team-1",
            ),
        ),
    )

    snapshot = briefings["team_messages"][0]["briefing_snapshot"]
    assert snapshot["generated_at"] == GENERATED_AT.isoformat()
    assert snapshot["report_date"] == REPORT_DATE.isoformat()
    assert snapshot["scope"] == "team"
    assert {
        item["member_ref"]: item for item in snapshot["members"]
    } == {
        "member-submitted": {
            "member_ref": "member-submitted",
            "member_name": "甲成员",
            "team_ref": "team-1",
            "team_name": "综合管理部",
            "classification": "submitted",
            "report_status": "completed",
            "confirmation_type": "user_confirmed",
            "submitted_at": GENERATED_AT.isoformat(),
        },
        "member-missing": {
            "member_ref": "member-missing",
            "member_name": "乙成员",
            "team_ref": "team-1",
            "team_name": "综合管理部",
            "classification": "missing",
            "report_status": None,
            "confirmation_type": None,
            "submitted_at": None,
        },
    }


def test_legacy_briefing_query_returns_recorded_text_but_no_invented_cause() -> None:
    submitted_at = GENERATED_AT.replace(hour=9, minute=15)
    source = DailyBriefingFactSource(
        members=(
            _member("member-1", "目标成员"),
            _member("lead-1", "负责人"),
        ),
        reports=(
            _report(member_ref="member-1", submitted_at=submitted_at),
        ),
        events=(
            DailyBriefingFactEvent(
                recipient_ref="lead-1",
                recipient_name="负责人",
                report_date=REPORT_DATE,
                created_at=GENERATED_AT,
                scope="team",
                team_name="综合管理部",
                department_name="",
                message_status="delivered",
                delivery_verified=True,
                message_text="综合管理部晨报：目标成员显示未提交。",
                briefing_snapshot=None,
            ),
        ),
    )
    repository = _Repository(source)
    query = DailyBriefingFactQuery(repository)

    result = asyncio.run(
        query.execute(
            tenant_id="tenant-1",
            actor_user_id="actor-1",
            request=DailyBriefingFactQueryRequest(
                report_date=REPORT_DATE,
                member_name="目标成员",
            ),
            now=GENERATED_AT.replace(hour=16),
        )
    )

    assert repository.calls == [("tenant-1", REPORT_DATE)]
    assert result["target_member"]["name"] == "目标成员"
    assert result["current_submission"]["status"] == "completed"
    assert result["current_submission"]["submitted_at"] == submitted_at.isoformat()
    assert result["recorded_briefings"][0]["message_text"].endswith(
        "目标成员显示未提交。"
    )
    assert result["recorded_briefings"][0]["snapshot_available"] is False
    assert result["recorded_briefings"][0]["member_at_snapshot"] is None
    assert result["cause"] is None
    assert result["evidence_limits"] == [
        "该历史晨报没有保存生成时的结构化成员分类快照，不能仅凭当前日报状态反推当时原因。"
    ]


def test_structured_briefing_query_preserves_snapshot_and_current_timeline() -> None:
    submitted_at = GENERATED_AT.replace(hour=9, minute=15)
    snapshot = {
        "generated_at": GENERATED_AT.isoformat(),
        "report_date": REPORT_DATE.isoformat(),
        "scope": "team",
        "members": [
            {
                "member_ref": "member-1",
                "member_name": "目标成员",
                "team_ref": "team-1",
                "team_name": "综合管理部",
                "classification": "missing",
                "report_status": None,
                "confirmation_type": None,
                "submitted_at": None,
            }
        ],
    }
    source = DailyBriefingFactSource(
        members=(
            _member("member-1", "目标成员"),
            _member("lead-1", "负责人"),
        ),
        reports=(
            _report(member_ref="member-1", submitted_at=submitted_at),
        ),
        events=(
            DailyBriefingFactEvent(
                recipient_ref="lead-1",
                recipient_name="负责人",
                report_date=REPORT_DATE,
                created_at=GENERATED_AT,
                scope="team",
                team_name="综合管理部",
                department_name="",
                message_status="delivered",
                delivery_verified=True,
                message_text="综合管理部晨报：目标成员显示未提交。",
                briefing_snapshot=snapshot,
            ),
        ),
    )

    result = asyncio.run(
        DailyBriefingFactQuery(_Repository(source)).execute(
            tenant_id="tenant-1",
            actor_user_id="actor-1",
            request=DailyBriefingFactQueryRequest(
                report_date=REPORT_DATE,
                member_name="目标成员",
            ),
            now=GENERATED_AT.replace(hour=16),
        )
    )

    event = result["recorded_briefings"][0]
    assert event["snapshot_available"] is True
    assert event["snapshot_generated_at"] == GENERATED_AT.isoformat()
    assert event["member_at_snapshot"]["classification"] == "missing"
    assert result["current_submission"]["submitted_at"] == submitted_at.isoformat()
    assert result["cause"] is None
    assert result["evidence_limits"] == []


def test_agent2_has_a_dedicated_read_only_briefing_fact_tool() -> None:
    definition = TOOL_REGISTRY["query_daily_briefing_facts"]

    assert definition.read_or_write == "read"
    assert definition.transaction_target_policy == "read_only"
    assert "server" in definition.object_binding_policy
    prompt = canary_system_prompt()
    assert "query_daily_briefing_facts" in prompt
    assert "不能仅凭当前日报状态反推" in prompt
    assert "为什么说没交" not in prompt


def test_briefing_dispatch_persists_the_generation_snapshot_unchanged() -> None:
    snapshot = {
        "generated_at": GENERATED_AT.isoformat(),
        "report_date": REPORT_DATE.isoformat(),
        "scope": "team",
        "members": [
            {
                "member_ref": "member-1",
                "member_name": "目标成员",
                "team_ref": "team-1",
                "team_name": "综合管理部",
                "classification": "missing",
                "report_status": None,
                "confirmation_type": None,
                "submitted_at": None,
            }
        ],
    }

    class Robot:
        async def send_robot_direct_markdown(self, **kwargs):
            return {
                "processQueryKey": "provider-ref",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": kwargs["user_ids"],
            }

    class Session:
        def __init__(self) -> None:
            self.events = []

        def add(self, event) -> None:
            self.events.append(event)

    session = Session()
    sent = asyncio.run(
        _send_daily_briefings(
            Robot(),
            {
                "date": REPORT_DATE.isoformat(),
                "team_messages": [
                    {
                        "scope": "team",
                        "team_id": "team-1",
                        "team_name": "综合管理部",
                        "recipients": [
                            {
                                "id": "11111111-1111-1111-1111-111111111111",
                                "name": "负责人",
                                "dingtalk_user_id": "ding-lead-1",
                            }
                        ],
                        "briefing_snapshot": snapshot,
                        "text": "综合管理部晨报",
                    }
                ],
            },
            session=session,
            report_date=REPORT_DATE,
        )
    )

    assert sent == 1
    assert len(session.events) == 1
    assert session.events[0].llm_decision_json["briefing_snapshot"] == snapshot
