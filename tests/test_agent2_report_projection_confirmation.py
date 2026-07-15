from datetime import UTC, datetime, timedelta

from app.agent2.report_projection_confirmation import (
    ProjectionConfirmationContext,
    ProjectionConfirmationPendingSnapshot,
    ProjectionConfirmationResolver,
    parse_projection_confirmation_answer,
)


NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


def _pending(**changes):
    values = dict(
        pending_id="pending-1", tenant_id="tenant-a", user_id="user-a",
        conversation_id="conversation-a", request_id="request-1",
        case_id="case-1", case_progress_id="progress-1", expected_state_version=3,
        expires_at=NOW + timedelta(minutes=10), status="awaiting_input", version=1,
    )
    values.update(changes)
    return ProjectionConfirmationPendingSnapshot(**values)


def _context(**changes):
    values = dict(
        tenant_id="tenant-a", user_id="user-a", conversation_id="conversation-a",
        conversation_state_version=3, now=NOW, source_message_already_processed=False,
    )
    values.update(changes)
    return ProjectionConfirmationContext(**values)


def test_confirmation_answer_forms_are_closed_and_deterministic():
    assert parse_projection_confirmation_answer("需要") == "confirm"
    assert parse_projection_confirmation_answer("确认") == "confirm"
    assert parse_projection_confirmation_answer("只记案件") == "decline"
    assert parse_projection_confirmation_answer("这条别放日报") == "decline"
    assert parse_projection_confirmation_answer("随便吧") == "unknown"


def test_unique_pending_binds_confirmation_but_multiple_pending_never_guess():
    resolver = ProjectionConfirmationResolver()

    bound = resolver.resolve((_pending(),), answer="需要", context=_context())
    ambiguous = resolver.resolve(
        (_pending(), _pending(pending_id="pending-2", request_id="request-2")),
        answer="确认", context=_context(),
    )

    assert bound.status == "bound"
    assert bound.action == "confirm"
    assert ambiguous.status == "clarification_required"
    assert ambiguous.actual_write is False


def test_expired_or_state_changed_confirmation_is_fail_closed():
    resolver = ProjectionConfirmationResolver()

    expired = resolver.resolve(
        (_pending(expires_at=NOW),), answer="需要", context=_context()
    )
    conflicted = resolver.resolve(
        (_pending(),), answer="需要",
        context=_context(conversation_state_version=4),
    )

    assert expired.status == "expired"
    assert conflicted.status == "conflicted"
    assert not expired.actual_write and not conflicted.actual_write
