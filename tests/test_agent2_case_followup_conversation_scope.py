from app.scheduler.runner import resolve_followup_conversation_id


def test_followup_conversation_scope_requires_explicit_or_unique_observed_id():
    assert resolve_followup_conversation_id(
        {}, tenant_id="tenant-a", user_id="user-a",
        observed_conversation_ids=("conversation-1",),
    ) == "conversation-1"
    assert resolve_followup_conversation_id(
        {}, tenant_id="tenant-a", user_id="user-a",
        observed_conversation_ids=("conversation-1", "conversation-2"),
    ) == ""


def test_explicit_tenant_user_conversation_mapping_wins_without_cross_scope_fallback():
    configured = {
        "tenant-a:user-a": "conversation-explicit",
        "tenant-b:user-a": "conversation-other-tenant",
    }

    assert resolve_followup_conversation_id(
        configured, tenant_id="tenant-a", user_id="user-a",
        observed_conversation_ids=("conversation-1", "conversation-2"),
    ) == "conversation-explicit"
    assert resolve_followup_conversation_id(
        configured, tenant_id="tenant-c", user_id="user-a",
        observed_conversation_ids=(),
    ) == ""
