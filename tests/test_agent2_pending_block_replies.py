from __future__ import annotations

from types import SimpleNamespace

from app.agent2.turn_runtime import (
    InformationContinuationBlocked,
    SelectionContinuationBlocked,
    VerifiedTurnRejected,
    verified_turn_rejection_reply,
)


def _result(*, status: str, reason: str) -> SimpleNamespace:
    return SimpleNamespace(status=status, reason=reason)


def test_multiple_pending_contexts_get_a_domain_neutral_zero_write_reply() -> None:
    reply = verified_turn_rejection_reply(
        VerifiedTurnRejected("pending_context_not_unique")
    )

    assert reply.reply_kind == "pending_context_not_unique"
    assert "不止一个待处理" in reply.message
    assert "没有做任何写入" in reply.message
    assert "日报" not in reply.message


def test_duplicate_selection_reply_does_not_invite_a_retry_or_claim_a_write() -> None:
    reply = verified_turn_rejection_reply(
        SelectionContinuationBlocked(
            _result(
                status="duplicate_source_message",
                reason="source_message_already_processed",
            )
        )
    )

    assert reply.reply_kind == "selection_duplicate_source_message"
    assert "已经处理过" in reply.message
    assert "没有重复写入" in reply.message
    assert "稍后重试" not in reply.message


def test_expired_information_reply_explains_how_to_recover() -> None:
    reply = verified_turn_rejection_reply(
        InformationContinuationBlocked(
            _result(status="expired", reason="pending_expired")
        )
    )

    assert reply.reply_kind == "information_expired"
    assert "已经过期" in reply.message
    assert "对象和内容" in reply.message
    assert "没有写入" in reply.message


def test_permission_change_reply_is_fail_closed_without_internal_details() -> None:
    reply = verified_turn_rejection_reply(
        SelectionContinuationBlocked(
            _result(status="permission_revoked", reason="case_permission_revoked")
        )
    )

    assert reply.reply_kind == "selection_permission_revoked"
    assert "权限发生了变化" in reply.message
    assert "没有写入" in reply.message
    assert "case_permission_revoked" not in reply.message


def test_unknown_verified_rejection_uses_safe_domain_neutral_fallback() -> None:
    reply = verified_turn_rejection_reply(
        VerifiedTurnRejected("unexpected_internal_contract")
    )

    assert reply.reply_kind == "verified_turn_rejected"
    assert "当前上下文" in reply.message
    assert "没有写入任何业务数据" in reply.message
    assert "unexpected_internal_contract" not in reply.message
