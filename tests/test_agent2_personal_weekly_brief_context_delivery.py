from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.personal_weekly_brief_scope import PersonalWeeklyBriefTarget
from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord
from app.scheduler import runner


NOW = datetime(2026, 8, 22, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = "11111111-1111-4111-8111-111111111111"
TARGET = PersonalWeeklyBriefTarget(
    tenant_id="tenant-a",
    internal_user_id=USER_ID,
    dingtalk_user_id="ding-user-a",
    display_name="脱敏用户",
    conversation_id="conversation-a",
)


def _row(status: str) -> PersonalWeeklyBriefRecord:
    delivered = status == "delivered"
    return PersonalWeeklyBriefRecord(
        brief_id="22222222-2222-4222-8222-222222222222",
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        conversation_id="conversation-a",
        week_start=date(2026, 8, 17),
        week_end=date(2026, 8, 21),
        snapshot_at=NOW,
        source_snapshot={"sources": []},
        source_fingerprint="a" * 64,
        content_json={"trace": {}},
        message_text="脱敏个人本周工作简报",
        llm_model="deepseek-v4-flash",
        status=status,
        idempotency_key="personal-weekly:tenant-a:user-a:2026-08-17",
        created_at=NOW,
        updated_at=NOW,
        provider_message_id="provider-1",
        provider_accepted_at=NOW,
        delivered_at=NOW if delivered else None,
        delivery_receipt_json=(
            {
                "schema_version": "agent2.personal_weekly_brief.delivery.v1",
                "provider_reference": "provider-1",
                "delivery_verified": True,
                "delivery_status": "SUCCESS",
                "delivered_dingtalk_user_ids": ["ding-user-a"],
                "checked_at": NOW.isoformat(),
                "evidence_source": "delivery_query",
            }
            if delivered
            else {}
        ),
    )


@pytest.mark.asyncio
async def test_provider_acceptance_does_not_enter_conversation_context(monkeypatch) -> None:
    class _ForbiddenSessions:
        def __call__(self):
            raise AssertionError("delivery-pending row must not open context session")

    monkeypatch.setattr(runner, "AsyncSessionLocal", _ForbiddenSessions())

    await runner._record_personal_weekly_brief_context(
        tenant_id="tenant-a",
        row=_row("delivery_pending"),
        target=TARGET,
        changed_at=NOW,
    )


@pytest.mark.asyncio
async def test_only_verified_exact_recipient_delivery_enters_context(monkeypatch) -> None:
    observed: dict[str, object] = {}
    context_recorded_at = NOW.replace(minute=47)

    class _Session:
        async def scalar(self, _statement):
            return SimpleNamespace(
                id=UUID(USER_ID),
                active=True,
                dingtalk_user_id="ding-user-a",
                timezone="Asia/Shanghai",
            )

        async def commit(self):
            observed["committed"] = True

    class _Sessions:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

        def __call__(self):
            return self

    async def _record_outbound(_session, **kwargs):
        observed["outbound"] = kwargs

    class _Store:
        def __init__(self, _session):
            pass

        async def record_context(self, **kwargs):
            observed["context"] = kwargs

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())
    monkeypatch.setattr(
        runner,
        "record_verified_outbound_context_message",
        _record_outbound,
    )
    monkeypatch.setattr(runner, "SqlPersonalWeeklyBriefStore", _Store)
    monkeypatch.setattr(
        runner,
        "_personal_weekly_observed_now",
        lambda: context_recorded_at,
    )

    await runner._record_personal_weekly_brief_context(
        tenant_id="tenant-a",
        row=_row("delivered"),
        target=TARGET,
    )

    receipt = observed["outbound"]["delivery_receipt"]
    assert receipt["deliveryVerified"] is True
    assert receipt["deliveryRecipientUserIds"] == ["ding-user-a"]
    assert observed["outbound"]["conversation_id"] == "conversation-a"
    assert observed["context"]["brief_id"] == _row("delivered").brief_id
    assert observed["context"]["changed_at"] == context_recorded_at
    assert observed["committed"] is True


@pytest.mark.asyncio
async def test_mismatched_stored_delivery_receipt_never_enters_context(monkeypatch) -> None:
    row = _row("delivered")
    row = PersonalWeeklyBriefRecord(
        **{
            **row.__dict__,
            "delivery_receipt_json": {
                **row.delivery_receipt_json,
                "delivered_dingtalk_user_ids": ["ding-someone-else"],
            },
        }
    )

    class _Session:
        async def scalar(self, _statement):
            return SimpleNamespace(
                id=UUID(USER_ID),
                active=True,
                dingtalk_user_id="ding-user-a",
                timezone="Asia/Shanghai",
            )

    class _Sessions:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

        def __call__(self):
            return self

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())

    with pytest.raises(RuntimeError, match="stored delivery receipt is invalid"):
        await runner._record_personal_weekly_brief_context(
            tenant_id="tenant-a",
            row=row,
            target=TARGET,
            changed_at=NOW,
        )
