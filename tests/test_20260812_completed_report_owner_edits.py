from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import repositories
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.context import TrustedReportItem, TrustedReportSnapshot
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.validation import _locked_historical_report_date
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.typed_daily_executor import TYPED_AUDIT_KEY, _snapshot_json


def _completed_snapshot() -> DailyReportMutationSnapshot:
    owner_id = uuid4()
    return DailyReportMutationSnapshot(
        report_id=uuid4(),
        owner_user_id=owner_id,
        version=7,
        status="completed",
        today_work=("review contract", "prepare evidence"),
        problems=(),
        tomorrow_plan=("follow up with court",),
        item_ids={
            "today_work": ("tw-1", "tw-2"),
            "problems": (),
            "tomorrow_plan": ("tp-1",),
        },
    )


def _command(
    snapshot: DailyReportMutationSnapshot,
    *,
    command_type: str,
    target_item_ids: tuple[str, ...] = (),
    patch: dict,
) -> TypedDailyCommand:
    return TypedDailyCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type=command_type,
        report_id=snapshot.report_id,
        report_version=snapshot.version,
        target_item_ids=target_item_ids,
        patch=patch,
        idempotency_key=f"completed-owner:{command_type}:{uuid4()}",
    )


@pytest.mark.parametrize(
    ("command_type", "target_item_ids", "patch"),
    (
        ("append_item", (), {"field": "today_work", "items": ["send opinion"]}),
        ("acknowledge_empty_section", (), {"field": "problems"}),
        ("edit_item", ("tw-1",), {"replacement": "review final contract"}),
        ("delete_item", ("tw-2",), {}),
        (
            "move_item",
            ("tw-2",),
            {"source_field": "today_work", "target_field": "tomorrow_plan"},
        ),
    ),
)
def test_completed_owner_can_directly_mutate_content_and_status_stays_completed(
    command_type: str,
    target_item_ids: tuple[str, ...],
    patch: dict,
) -> None:
    snapshot = _completed_snapshot()
    result = execute_typed_daily_command(
        _command(
            snapshot,
            command_type=command_type,
            target_item_ids=target_item_ids,
            patch=patch,
        ),
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        allow_completed_content_mutation=True,
    )

    assert result.validation.status == "authorized"
    assert result.should_write_db is True
    assert result.after.status == "completed"
    assert result.after.version == snapshot.version + 1


@pytest.mark.parametrize(
    ("command_type", "target_item_ids", "patch"),
    (
        ("append_item", (), {"field": "today_work", "items": ["send opinion"]}),
        ("edit_item", ("tw-1",), {"replacement": "review final contract"}),
        ("delete_item", ("tw-2",), {}),
        (
            "move_item",
            ("tw-2",),
            {"source_field": "today_work", "target_field": "tomorrow_plan"},
        ),
    ),
)
def test_completed_content_authority_never_bypasses_owner_check(
    command_type: str,
    target_item_ids: tuple[str, ...],
    patch: dict,
) -> None:
    snapshot = _completed_snapshot()
    result = execute_typed_daily_command(
        _command(
            snapshot,
            command_type=command_type,
            target_item_ids=target_item_ids,
            patch=patch,
        ),
        snapshot=snapshot,
        actor_user_id=uuid4(),
        allow_completed_content_mutation=True,
    )

    assert result.validation.status == "blocked"
    assert result.validation.reason_code == "forbidden_payload"
    assert result.should_write_db is False
    assert result.after == snapshot


@pytest.mark.parametrize(
    ("command_type", "patch"),
    (
        ("clear_report", {"field": "all"}),
        (
            "copy_report",
            {"sections": {"today_work": ["copied content"]}},
        ),
        ("submit_report", {}),
    ),
)
def test_completed_content_authority_does_not_open_lifecycle_or_broad_operations(
    command_type: str,
    patch: dict,
) -> None:
    snapshot = _completed_snapshot()
    result = execute_typed_daily_command(
        _command(snapshot, command_type=command_type, patch=patch),
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        allow_completed_content_mutation=True,
    )

    assert result.validation.status == "blocked"
    assert result.validation.reason_code == "invalid_report_state"
    assert result.should_write_db is False


def _trusted_completed_report() -> TrustedReportSnapshot:
    owner_id = uuid4()
    report_id = uuid4()
    return TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-test",
        owner_user_id=owner_id,
        report_date=date(2026, 8, 11),
        version=3,
        status="completed",
        items=(
            TrustedReportItem(
                item_id="tw-1",
                field="today_work",
                content="review contract",
                report_id=report_id,
                report_version=3,
            ),
        ),
    )


@pytest.mark.parametrize(
    "tool_name",
    ("edit_daily_items", "delete_daily_items", "move_daily_items"),
)
def test_completed_owner_content_edits_are_not_locked_after_historical_cutoff(
    tool_name: str,
) -> None:
    report = _trusted_completed_report()
    context = SimpleNamespace(
        now=datetime(2026, 8, 12, 2, 0, tzinfo=UTC),
        principal=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    definition = SimpleNamespace(
        tool_name=tool_name,
        read_or_write="write",
        permission_policy="authenticated_report_owner_write",
        object_binding_policy="trusted_report_version_and_item_ids",
    )

    assert (
        _locked_historical_report_date(
            context=context,
            definition=definition,
            report=report,
            date_facts={},
        )
        is None
    )


def test_historical_cutoff_still_blocks_non_completed_or_broad_operations() -> None:
    completed = _trusted_completed_report()
    collecting = completed.model_copy(update={"status": "collecting"})
    context = SimpleNamespace(
        now=datetime(2026, 8, 12, 2, 0, tzinfo=UTC),
        principal=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    edit_definition = SimpleNamespace(
        tool_name="edit_daily_items",
        read_or_write="write",
        permission_policy="authenticated_report_owner_write",
        object_binding_policy="trusted_report_version_and_item_ids",
    )
    clear_definition = SimpleNamespace(
        tool_name="request_clear_report",
        read_or_write="write",
        permission_policy="authenticated_report_owner_write",
        object_binding_policy="trusted_report_version_and_item_ids",
    )

    assert _locked_historical_report_date(
        context=context,
        definition=edit_definition,
        report=collecting,
        date_facts={},
    ) == date(2026, 8, 11)
    assert _locked_historical_report_date(
        context=context,
        definition=clear_definition,
        report=completed,
        date_facts={},
    ) == date(2026, 8, 11)


def test_add_tool_resolves_historical_completed_report_without_cutoff_lock() -> None:
    completed = _trusted_completed_report()
    context = SimpleNamespace(
        now=datetime(2026, 8, 12, 2, 0, tzinfo=UTC),
        principal=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    definition = SimpleNamespace(
        tool_name="add_daily_items",
        read_or_write="write",
        permission_policy="authenticated_report_owner_write",
        object_binding_policy="server_resolved_owner_report",
    )

    assert _locked_historical_report_date(
        context=context,
        definition=definition,
        report=completed,
        date_facts={},
    ) is None


def test_agent2_prompt_and_tools_state_completed_owner_edit_contract() -> None:
    prompt = canary_system_prompt()
    assert "status=completed remains directly mutable for content changes" in prompt
    assert "Do not require, suggest, or advertise reopening" in prompt
    for tool_name in (
        "add_daily_items",
        "edit_daily_items",
        "delete_daily_items",
        "move_daily_items",
    ):
        description = TOOL_REGISTRY[tool_name].description
        assert "trusted completed report directly" in description
        assert "preserves completed status" in description


@pytest.mark.asyncio
async def test_upsert_preserves_original_submission_metadata_for_completed_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_submitted_at = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    existing = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        team_id=uuid4(),
        report_date=date(2026, 8, 11),
        today_work=["review contract"],
        problems=[],
        tomorrow_plan=["follow up"],
        emotion="",
        raw_input="",
        input_fragments=[],
        section_status={},
        completeness_score=1,
        status="completed",
        confirmation_type="auto_submitted_timeout",
        confirmed_by_user=False,
        quality_warning=None,
        last_modified_by_user=False,
        last_modified_at=original_submitted_at,
        pending_confirmation_at=None,
        auto_submit_at=None,
        source="scheduler",
        llm_model="agent2",
        llm_payload={},
        submitted_at=original_submitted_at,
    )
    user = SimpleNamespace(id=existing.user_id, team_id=existing.team_id)

    class Session:
        async def flush(self) -> None:
            return None

    async def fake_get_report(session, user_id, report_date):
        del session, user_id, report_date
        return existing

    async def fake_interaction_event(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr(repositories, "get_report", fake_get_report)
    monkeypatch.setattr(
        repositories,
        "maybe_create_report_interaction_event",
        fake_interaction_event,
    )

    modified_at = datetime(2026, 8, 11, 15, 20, tzinfo=UTC)
    report = await repositories.upsert_daily_report(
        Session(),
        user=user,
        report_date=existing.report_date,
        raw_input="",
        source="agent2_tool_call_canary",
        today_work=["review final contract"],
        problems=[],
        tomorrow_plan=["follow up"],
        emotion="",
        completeness_score=1,
        status="completed",
        section_status={},
        llm_model="agent2_tool_call_core",
        llm_payload={"agent2": True},
        received_at=modified_at,
        confirmation_type="user_confirmed",
        confirmed_by_user=True,
        quality_warning=None,
        last_modified_by_user=True,
        last_modified_at=modified_at,
        pending_confirmation_at=None,
        auto_submit_at=None,
        replace_sections=True,
        preserve_existing_submission=True,
    )

    assert report.status == "completed"
    assert report.confirmation_type == "auto_submitted_timeout"
    assert report.confirmed_by_user is False
    assert report.submitted_at == original_submitted_at
    assert report.last_modified_by_user is True
    assert report.last_modified_at == modified_at
    assert report.today_work == ["review final contract"]


def test_completed_edit_audit_records_exact_before_and_after_content() -> None:
    snapshot = _completed_snapshot()
    command = _command(
        snapshot,
        command_type="edit_item",
        target_item_ids=("tw-1",),
        patch={"replacement": "review final contract"},
    )
    result = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        allow_completed_content_mutation=True,
    )

    before_json = _snapshot_json(result.before)
    after_json = _snapshot_json(result.after)
    audit_json = result.audit.as_dict()

    assert before_json["today_work"] == ["review contract", "prepare evidence"]
    assert after_json["today_work"] == [
        "review final contract",
        "prepare evidence",
    ]
    assert before_json["item_ids"]["today_work"] == ["tw-1", "tw-2"]
    assert after_json["item_ids"]["today_work"] == ["tw-1", "tw-2"]
    assert audit_json["command_type"] == "edit_item"
    assert audit_json["target_item_ids"] == ["tw-1"]
    assert audit_json["before_version"] == 7
    assert audit_json["after_version"] == 8
    assert audit_json["actual_write"] is True


@pytest.mark.parametrize(
    ("command_type", "target_item_ids", "patch"),
    (
        (
            "append_item",
            (),
            {"field": "today_work", "items": ["send final opinion"]},
        ),
        ("delete_item", ("tw-2",), {}),
        (
            "move_item",
            ("tw-2",),
            {"source_field": "today_work", "target_field": "tomorrow_plan"},
        ),
    ),
)
def test_completed_mutation_audit_can_reconstruct_each_change(
    command_type: str,
    target_item_ids: tuple[str, ...],
    patch: dict,
) -> None:
    snapshot = _completed_snapshot()
    result = execute_typed_daily_command(
        _command(
            snapshot,
            command_type=command_type,
            target_item_ids=target_item_ids,
            patch=patch,
        ),
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        allow_completed_content_mutation=True,
    )

    before_json = _snapshot_json(result.before)
    after_json = _snapshot_json(result.after)
    assert before_json["status"] == "completed"
    assert after_json["status"] == "completed"
    assert before_json != after_json
    if command_type == "append_item":
        assert after_json["today_work"][-1] == "send final opinion"
        assert len(after_json["item_ids"]["today_work"]) == 3
    elif command_type == "delete_item":
        assert "prepare evidence" not in after_json["today_work"]
        assert "tw-2" not in after_json["item_ids"]["today_work"]
    else:
        assert "prepare evidence" not in after_json["today_work"]
        assert after_json["tomorrow_plan"][-1] == "prepare evidence"
        assert "tw-2" in after_json["item_ids"]["tomorrow_plan"]


def test_completed_edit_keeps_append_only_typed_audit_history() -> None:
    snapshot = _completed_snapshot()
    command = _command(
        snapshot,
        command_type="delete_item",
        target_item_ids=("tw-2",),
        patch={},
    )
    result = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        allow_completed_content_mutation=True,
    )
    prior = {"command_type": "append_item", "after_version": 7}
    section_status = {TYPED_AUDIT_KEY: [prior]}
    section_status[TYPED_AUDIT_KEY] = [
        *section_status[TYPED_AUDIT_KEY],
        result.audit.as_dict(),
    ]

    assert section_status[TYPED_AUDIT_KEY][0] == prior
    assert section_status[TYPED_AUDIT_KEY][1]["command_type"] == "delete_item"
    assert section_status[TYPED_AUDIT_KEY][1]["after_version"] == 8
