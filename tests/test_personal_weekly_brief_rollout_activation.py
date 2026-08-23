from __future__ import annotations

import sys

from scripts.activate_personal_weekly_brief_rollout_20260823 import main


def test_activation_applies_and_restores_exact_environment(
    tmp_path,
    monkeypatch,
) -> None:
    env_path = tmp_path / ".env"
    backup_path = tmp_path / "before.env"
    original = (
        "UNRELATED=value\n"
        "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST=tenant-a\n"
        "AGENT2_PERSONAL_WEEKLY_BRIEF_ENABLED=false\n"
        "AGENT2_PERSONAL_WEEKLY_BRIEF_SEND_ENABLED=false\n"
        "AGENT2_PERSONAL_WEEKLY_BRIEF_TENANT_ID=\n"
    )
    env_path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "activate",
            "apply",
            "--env-file",
            str(env_path),
            "--backup-path",
            str(backup_path),
        ],
    )
    main()

    activated = env_path.read_text(encoding="utf-8")
    assert "UNRELATED=value" in activated
    assert "AGENT2_PERSONAL_WEEKLY_BRIEF_ENABLED=true" in activated
    assert "AGENT2_PERSONAL_WEEKLY_BRIEF_SEND_ENABLED=true" in activated
    assert "AGENT2_PERSONAL_WEEKLY_BRIEF_TENANT_ID=tenant-a" in activated
    assert backup_path.read_text(encoding="utf-8") == original

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "activate",
            "rollback",
            "--env-file",
            str(env_path),
            "--backup-path",
            str(backup_path),
        ],
    )
    main()

    assert env_path.read_text(encoding="utf-8") == original
