from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agent2.weekly_plan_history_pipeline import (
    HistorySuggestionRefreshRequest,
    HistorySuggestionResult,
)
from app.agent2.weekly_plan_history_sql_adapter import (
    SqlHistorySuggestionStore,
    SqlTrustedDailyHistorySource,
)
from app.agent2.weekly_plan_history_suggestions import ConfirmedRecordState
from app.agent2.weekly_plan_suggestions import (
    TrustedSourceKind,
    build_trusted_evidence,
    create_suggestion,
)

UTC = timezone.utc
OWNER = "11111111-1111-4111-8111-111111111111"


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def mappings(self):
        return self

    def one_or_none(self):
        if not self._rows:
            return None
        assert len(self._rows) == 1
        return self._rows[0]


class _RecordingSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarRows(self.rows)

    async def flush(self):
        return None


def _report(
    *,
    report_id: str,
    report_date: date,
    status: str,
    tomorrow_plan=(),
    today_work=(),
    problems=(),
    confirmed_by_user=False,
    confirmation_type="none",
    version=1,
):
    updated = datetime.combine(report_date, datetime.min.time(), tzinfo=UTC)
    return SimpleNamespace(
        id=UUID(report_id),
        user_id=UUID(OWNER),
        report_date=report_date,
        status=status,
        tomorrow_plan=list(tomorrow_plan),
        today_work=list(today_work),
        problems=list(problems),
        confirmed_by_user=confirmed_by_user,
        confirmation_type=confirmation_type,
        section_status={
            "_agent2_report_version": version,
            "_draft_item_ids": {
                "tomorrow_plan": [
                    f"tomorrow-item-{index}"
                    for index, _ in enumerate(tomorrow_plan, start=1)
                ]
            },
        },
        submitted_at=updated if status == "completed" else None,
        updated_at=updated,
    )


@pytest.mark.asyncio
async def test_sql_history_source_keeps_only_stable_owner_records_and_exact_item_refs() -> None:
    session = _RecordingSession(
        [
            _report(
                report_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                report_date=date(2026, 8, 11),
                status="completed",
                tomorrow_plan=("完善合同审查规则",),
                confirmed_by_user=True,
                confirmation_type="user_confirmed",
                version=3,
            ),
            _report(
                report_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                report_date=date(2026, 8, 12),
                status="completed",
                today_work=("整理项目资料",),
                confirmation_type="auto_submitted_timeout",
                version=2,
            ),
            _report(
                report_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                report_date=date(2026, 8, 13),
                status="collecting",
                tomorrow_plan=("不可信草稿",),
                version=8,
            ),
        ]
    )

    history = await SqlTrustedDailyHistorySource(session).load_history(
        HistorySuggestionRefreshRequest(
            tenant_id="tenant-a",
            owner_user_id=OWNER,
            target_week_start=date(2026, 8, 17),
            as_of=datetime(2026, 8, 14, 9, tzinfo=UTC),
        )
    )

    assert len(session.statements) == 1
    assert len(history.source_items) == 1
    source = history.source_items[0]
    assert source.item_ref == "tomorrow-item-1"
    assert source.source_version == "3"
    assert source.record_state is ConfirmedRecordState.CONFIRMED
    assert source.item_exact_text == "完善合同审查规则"
    assert [value.source_ref for value in history.later_records] == [
        "daily-report:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "daily-report:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ]

    compiled = str(session.statements[0].compile()).lower()
    # Daily reports themselves have no tenant column.  The authenticated
    # Agent2 binding is the tenant boundary, so the query must prove the owner
    # belongs to the requested tenant instead of trusting only a UUID supplied
    # by the caller.
    assert "agent2_identity_bindings" in compiled
    assert "tenant_id" in compiled


@pytest.mark.asyncio
async def test_sql_history_source_requires_the_requested_tenant_scope() -> None:
    session = _RecordingSession([])

    await SqlTrustedDailyHistorySource(session).load_history(
        HistorySuggestionRefreshRequest(
            tenant_id="tenant-b",
            owner_user_id=OWNER,
            target_week_start=date(2026, 8, 17),
            as_of=datetime(2026, 8, 14, 9, tzinfo=UTC),
        )
    )

    statement = session.statements[0]
    compiled = statement.compile()
    parameter_values = set(compiled.params.values())
    assert "tenant-b" in parameter_values
    assert UUID(OWNER) in parameter_values or OWNER in parameter_values


def _history_suggestion(*, version="1", text="完善合同审查规则"):
    evidence_text = '{"tomorrow_plan":["' + text + '"]}'
    return create_suggestion(
        owner_user_id=OWNER,
        target_week_start=date(2026, 8, 17),
        evidence=build_trusted_evidence(
            owner_user_id=OWNER,
            source_kind=TrustedSourceKind.CONFIRMED_RECORD,
            source_ref="daily-report:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa#item-1",
            source_version=version,
            evidence_text=evidence_text,
        ),
        matter_excerpt=text,
        created_at=datetime(2026, 8, 14, 9, tzinfo=UTC),
        expires_at=datetime(2026, 8, 24, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_sql_suggestion_store_loads_only_exact_owner_plan_and_week() -> None:
    plan_id = UUID("22222222-2222-4222-8222-222222222222")
    session = _RecordingSession(
        [
            {
                "plan_id": plan_id,
                "tenant_id": "tenant-a",
                "owner_user_id": OWNER,
                "target_week_start": date(2026, 8, 17),
            }
        ]
    )
    call_count = 0

    async def execute(statement):
        nonlocal call_count
        call_count += 1
        session.statements.append(statement)
        return _ScalarRows(
            [session.rows[0]] if call_count == 1 else []
        )

    session.execute = execute
    store = SqlHistorySuggestionStore(session)
    request = HistorySuggestionRefreshRequest(
        tenant_id="tenant-a",
        owner_user_id=OWNER,
        target_week_start=date(2026, 8, 17),
        as_of=datetime(2026, 8, 14, 9, tzinfo=UTC),
    )

    assert await store.load_available(request) == ()

    compiled = session.statements[1].compile()
    text = str(compiled).lower()
    assert "agent2_weekly_plan_suggestions" in text
    assert "tenant_id" in text and "owner_user_id" in text
    # The first query binds owner/week to one plan; the second query is then
    # additionally scoped by that exact plan_id.
    assert "agent2_weekly_plans" in str(session.statements[0]).lower()
    assert plan_id in tuple(compiled.params.values())
    assert date(2026, 8, 17) in tuple(compiled.params.values())


@pytest.mark.asyncio
async def test_sql_suggestion_store_has_no_write_when_owner_plan_is_missing() -> None:
    session = _RecordingSession([])
    request = HistorySuggestionRefreshRequest(
        tenant_id="tenant-a",
        owner_user_id=OWNER,
        target_week_start=date(2026, 8, 17),
        as_of=datetime(2026, 8, 14, 9, tzinfo=UTC),
    )

    result = await SqlHistorySuggestionStore(session).persist(
        request,
        HistorySuggestionResult(new_suggestions=(_history_suggestion(),)),
    )

    assert result.new_suggestions == ()
    assert len(session.statements) == 1
    assert str(session.statements[0]).lstrip().lower().startswith("select")
