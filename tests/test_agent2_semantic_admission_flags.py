from types import SimpleNamespace

from app.agent2.cognitive_runtime_v3 import semantic_admission_mode


TENANT = "sandbox-agent2-phase2-20260711"
PANG = "pang-user-id"


def _settings(**overrides):
    values = {
        "agent2_semantic_admission_enabled": False,
        "agent2_semantic_admission_enforce": False,
        "agent2_semantic_admission_tenant_allowlist": "",
        "agent2_semantic_admission_user_allowlist": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_semantic_admission_is_disabled_by_default_and_with_empty_allowlists():
    assert semantic_admission_mode(_settings(), tenant_id=TENANT, user_id=PANG) == "disabled"
    assert (
        semantic_admission_mode(
            _settings(agent2_semantic_admission_enabled=True),
            tenant_id=TENANT,
            user_id=PANG,
        )
        == "disabled"
    )


def test_semantic_admission_shadow_requires_both_trusted_scope_allowlists():
    settings = _settings(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_tenant_allowlist=TENANT,
        agent2_semantic_admission_user_allowlist=PANG,
    )

    assert semantic_admission_mode(settings, tenant_id=TENANT, user_id=PANG) == "shadow"
    assert semantic_admission_mode(settings, tenant_id="other", user_id=PANG) == "disabled"
    assert semantic_admission_mode(settings, tenant_id=TENANT, user_id="other") == "disabled"


def test_semantic_admission_enforce_cannot_expand_beyond_allowlists():
    settings = _settings(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_enforce=True,
        agent2_semantic_admission_tenant_allowlist=f"{TENANT},other-tenant",
        agent2_semantic_admission_user_allowlist=PANG,
    )

    assert semantic_admission_mode(settings, tenant_id=TENANT, user_id=PANG) == "enforced"
    assert semantic_admission_mode(settings, tenant_id=TENANT, user_id="liu") == "disabled"
