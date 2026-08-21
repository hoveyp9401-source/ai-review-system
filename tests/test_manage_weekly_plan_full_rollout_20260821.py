from __future__ import annotations

from pathlib import Path

import pytest

from app.agent2.weekly_plan_reminder_dispatch import (
    WEEKLY_PLAN_REMINDER_TEXT_TEMPLATE,
)
from scripts import manage_weekly_plan_full_rollout_20260821 as rollout

ROLLOUT_SWITCH = (
    Path(__file__).parents[1]
    / "scripts"
    / "switch_agent2_weekly_plan_full_20260821.sh"
)


def _env_text(**overrides: str) -> str:
    values = {
        "AGENT2_WEEKLY_PLAN_ENABLED": "false",
        "AGENT2_WEEKLY_PLAN_WRITE_ENABLED": "false",
        "AGENT2_WEEKLY_PLAN_SEND_ENABLED": "false",
        "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST": "tenant-a",
        "AGENT2_WEEKLY_PLAN_USER_ALLOWLIST": "user-a,user-b",
        "AGENT2_WEEKLY_PLAN_SEND_USER_ALLOWLIST": "",
        "WEEKLY_PLAN_COLLECTION_OPEN_HOUR": "16",
        "WEEKLY_PLAN_COLLECTION_OPEN_MINUTE": "0",
        "WEEKLY_PLAN_REMINDER_HOUR": "15",
        "WEEKLY_PLAN_REMINDER_MINUTE": "0",
    }
    values.update(overrides)
    return "UNCHANGED_SETTING=keep-me\n" + "".join(
        f"{key}={values[key]}\n" for key in rollout.ENV_KEYS
    )


def test_atomic_env_update_changes_only_the_weekly_plan_rollout_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(_env_text(), encoding="utf-8")
    replacements = {
        "AGENT2_WEEKLY_PLAN_ENABLED": "true",
        "AGENT2_WEEKLY_PLAN_WRITE_ENABLED": "true",
        "AGENT2_WEEKLY_PLAN_SEND_ENABLED": "true",
        "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST": "tenant-a",
        "AGENT2_WEEKLY_PLAN_USER_ALLOWLIST": "user-a,user-b,user-c",
        "AGENT2_WEEKLY_PLAN_SEND_USER_ALLOWLIST": "user-a,user-b,user-c",
        "WEEKLY_PLAN_COLLECTION_OPEN_HOUR": "15",
        "WEEKLY_PLAN_COLLECTION_OPEN_MINUTE": "0",
        "WEEKLY_PLAN_REMINDER_HOUR": "15",
        "WEEKLY_PLAN_REMINDER_MINUTE": "0",
    }

    result = rollout._write_env(path, replacements)
    _encoded, actual = rollout._read_env(path)

    assert actual == replacements
    assert path.read_text(encoding="utf-8").startswith(
        "UNCHANGED_SETTING=keep-me\n"
    )
    assert result["changed"] is True
    assert result["before_sha256"] != result["after_sha256"]


def test_env_reader_fails_closed_on_a_duplicate_rollout_key(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        _env_text() + "AGENT2_WEEKLY_PLAN_ENABLED=true\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate weekly-plan env key"):
        rollout._read_env(path)


def test_reminder_send_remains_blocked_until_copy_is_user_approved() -> None:
    with pytest.raises(RuntimeError, match="reminder copy is not user-approved"):
        rollout._require_user_approved_reminder_template(send_enabled=True)


def test_read_write_rollout_does_not_require_reminder_copy() -> None:
    assert rollout.ROLLOUT_SEND_ENABLED is False

    rollout._require_user_approved_reminder_template(send_enabled=False)


def test_approved_reminder_copy_must_still_match_exactly(monkeypatch) -> None:
    monkeypatch.setattr(
        rollout,
        "USER_APPROVED_REMINDER_TEMPLATE_SHA256",
        rollout._sha256(WEEKLY_PLAN_REMINDER_TEXT_TEMPLATE.encode("utf-8")),
    )

    rollout._require_user_approved_reminder_template(send_enabled=True)

    monkeypatch.setattr(
        rollout,
        "WEEKLY_PLAN_REMINDER_TEXT_TEMPLATE",
        WEEKLY_PLAN_REMINDER_TEXT_TEMPLATE + " changed",
    )
    with pytest.raises(RuntimeError, match="reminder copy changed after approval"):
        rollout._require_user_approved_reminder_template(send_enabled=True)


def test_switch_requires_config_restore_before_apply_can_partially_fail() -> None:
    source = ROLLOUT_SWITCH.read_text(encoding="utf-8")

    assert source.index("config_applied=1\n  run_config apply") < source.index(
        "run_config apply --backup-path"
    )


def test_switch_reports_an_incomplete_rollback_as_failure() -> None:
    source = ROLLOUT_SWITCH.read_text(encoding="utf-8")

    assert (
        'run_config restore --backup-path "$backup_path" || rollback_failed=1'
        in source
    )
    assert 'wait_healthy "$previous" || rollback_failed=1' in source
    assert 'if [[ "$rollback_failed" -ne 0 ]]' in source
    assert "status=1" in source


def test_switch_current_cannot_abort_the_rest_of_rollback() -> None:
    source = ROLLOUT_SWITCH.read_text(encoding="utf-8")
    switch_current = source.split("switch_current() {", 1)[1].split(
        "\n}\n\nrun_config()", 1
    )[0]

    assert "return 1" in switch_current
    assert "exit 1" not in switch_current


def test_partial_freeze_is_resumed_before_rollback_can_mutate_state() -> None:
    source = ROLLOUT_SWITCH.read_text(encoding="utf-8")
    freeze_all = source.split("freeze_all_services() {", 1)[1].split(
        "\n}\n\nterminate_frozen_services()", 1
    )[0]
    rollback_all = source.split("rollback_all() {", 1)[1].split(
        "\n}\n\nrequire_release", 1
    )[0]

    assert "service_pids=()" in freeze_all
    assert "resume_partial_freeze" in freeze_all
    assert "all_frozen_pids_are_stopped" in freeze_all
    assert freeze_all.index("processes_frozen=1") > freeze_all.index(
        "all_frozen_pids_are_stopped"
    )
    assert 'rollback_frozen=0' in rollback_all
    assert 'if [[ "$rollback_frozen" -eq 1 ]]' in rollback_all


def test_preserved_real_plans_make_restore_explicitly_incomplete() -> None:
    source = Path(rollout.__file__).read_text(encoding="utf-8")
    restore = source.split("async def restore(", 1)[1].split(
        "\n\nasync def verify(", 1
    )[0]

    assert "if fallback_disable:" in restore
    assert "manual intervention is required" in restore
