from __future__ import annotations

from pathlib import Path

import pytest

from scripts import manage_weekly_plan_full_rollout_20260821 as rollout


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
