from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    CANARY_THINKING_ENABLED,
)


ROOT = Path(__file__).resolve().parents[1]


def _bash() -> str:
    resolved = shutil.which("bash")
    if resolved:
        return resolved
    fallback = Path(r"C:\Program Files\Git\bin\bash.exe")
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("bash is required for release fault-injection tests")


def _bash_path(path: Path) -> str:
    return path.resolve().as_posix()


def _inject_post_change_failure(
    tmp_path: Path,
    *,
    current_points_to_target: bool,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    old_release = tmp_path / "old-release"
    target_release = tmp_path / "target-release"
    candidate_release = tmp_path / "candidate-release"
    for path in (old_release, target_release, candidate_release):
        path.mkdir()
    shared_env = tmp_path / "shared.env"
    env_backup = tmp_path / "env.backup"
    shared_env.write_text("current-env\n", encoding="utf-8")
    env_backup.write_text("old-env\n", encoding="utf-8")
    control_backup = tmp_path / "controls.json"
    control_backup.write_text("{}", encoding="utf-8")
    current_marker = tmp_path / "current-marker.txt"
    current_marker.write_text(
        str(target_release if current_points_to_target else old_release),
        encoding="utf-8",
    )
    fake_python = tmp_path / "fake-python.sh"
    call_log = tmp_path / "python-calls.log"
    fake_python.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$CALL_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    os.chmod(fake_python, 0o755)
    deploy_script = ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    rollback_link = tmp_path / "rollback-link"
    shell = f"""
export CANDIDATE_RELEASE='{_bash_path(candidate_release)}'
export TARGET_RELEASE='{_bash_path(target_release)}'
export EXPECTED_OLD_RELEASE='{_bash_path(old_release)}'
export RELEASE_KEY='fault-test'
export CALL_LOG='{_bash_path(call_log)}'
export DEPLOY_LIBRARY_ONLY=1
source '{_bash_path(deploy_script)}'
python='{_bash_path(fake_python)}'
control_backup='{_bash_path(control_backup)}'
env_backup='{_bash_path(env_backup)}'
shared_env='{_bash_path(shared_env)}'
current_marker='{_bash_path(current_marker)}'
rollback_link='{_bash_path(rollback_link)}'
services=(fake-api fake-stream fake-scheduler)
systemctl() {{ return 0; }}
wait_for_release() {{ return 0; }}
wait_for_health() {{ return 0; }}
switch_current_to() {{ printf "%s" "$1" > "$current_marker"; }}
controls_restore_required=1
current_restore_required={1 if current_points_to_target else 0}
set +e
restore_after_failure 77
"""
    result = subprocess.run(
        [_bash(), "-lc", shell],
        text=True,
        capture_output=True,
        check=False,
    )
    return result, current_marker, old_release, call_log


def test_v22_flash_release_is_one_fail_closed_bundle() -> None:
    script = (
        ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    ).read_text(encoding="utf-8")
    alignment = (
        ROOT / "scripts" / "verify_agent2_runtime_control_alignment.py"
    ).read_text(encoding="utf-8")

    assert CANARY_MODEL_NAME == "deepseek-v4-flash"
    assert CANARY_THINKING_ENABLED is True
    assert 'systemctl stop "${services[@]}"' in script
    assert 'systemctl start "${services[@]}"' in script
    assert "ai-review-api.service" in script
    assert "ai-review-stream.service" in script
    assert "ai-review-scheduler.service" in script
    assert "--mode activate" in script
    assert "--mode restore" in script
    assert "switch_current_to \"$TARGET_RELEASE\"" in script
    assert "switch_current_to \"$EXPECTED_OLD_RELEASE\"" in script
    assert "verify_agent2_runtime_control_alignment.py" in script
    assert "smoke_20260812_final_date_matrix_rollback.py" in script
    assert "smoke_20260812_continuous_real_context_rollback.py" in script
    assert "simulate_full_rollout_readonly.py" in script
    assert "send_message" not in script
    assert "send_to_user" not in script
    assert 'payload["dingtalk_send_calls"] == 0' in script
    assert 'assert len(payload["results"]) == 6' in script
    assert 'payload["current_agent2_active_user_limit"] == 74' in script
    assert "Agent2 active-user limit does not match" in alignment


def test_release_requires_74_control_changes_and_audits() -> None:
    script = (
        ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    ).read_text(encoding="utf-8")
    activation = (
        ROOT / "scripts" / "activate_agent2_v22_flash.py"
    ).read_text(encoding="utf-8")

    assert 'payload["changed_count"] == 74' in script
    assert 'payload["audit_count"] == 74' in script
    assert "with_for_update()" in activation
    assert "await session.commit()" in activation
    assert "ToolCallCanaryControlAudit(" in activation
    assert "controls_sha256" in activation
    assert "refusing to overwrite controls changed after activation" in activation


def test_control_scope_joins_dashboard_roster_to_separate_runtime_tenant() -> None:
    activation = (
        ROOT / "scripts" / "activate_agent2_v22_flash.py"
    ).read_text(encoding="utf-8")
    alignment = (
        ROOT / "scripts" / "verify_agent2_runtime_control_alignment.py"
    ).read_text(encoding="utf-8")

    for script in (activation, alignment):
        assert "ToolCallCanaryControl.user_id.in_(roster_user_ids)" in script
        assert "ToolCallCanaryControl.tenant_id == tenant_id" not in script
        assert "dashboard_tenant_id" in script
        assert "runtime_tenant_id" in script

    assert 'backup_payload.get("dashboard_tenant_id")' in activation
    assert 'backup_payload.get("runtime_tenant_id")' in activation


def test_release_marks_controls_for_restore_before_activation_can_commit() -> None:
    script = (
        ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    ).read_text(encoding="utf-8")

    restore_flag = script.index("controls_restore_required=1")
    activate = script.index("--mode activate", restore_flag)
    clear_flag = script.index("controls_restore_required=0", activate)

    assert restore_flag < activate < clear_flag
    assert 'if [[ "$controls_restore_required" -eq 1 ]]' in script


def test_release_marks_current_for_restore_before_atomic_link_switch() -> None:
    script = (
        ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    ).read_text(encoding="utf-8")

    restore_flag = script.index("current_restore_required=1")
    switch = script.index(
        'switch_current_to "$TARGET_RELEASE" "$next_link"',
        restore_flag,
    )
    clear_flag = script.index("current_restore_required=0", switch)

    assert restore_flag < switch < clear_flag
    assert 'if [[ "$current_restore_required" -eq 1 ]]' in script


def test_release_disarms_traps_before_clearing_either_restore_guard() -> None:
    script = (
        ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    ).read_text(encoding="utf-8")

    successful_deploy = script.index("printf 'deployed release:")
    disarm = script.rindex("trap - ERR INT TERM", 0, successful_deploy)
    clear_controls = script.rindex(
        "controls_restore_required=0", 0, successful_deploy
    )
    clear_current = script.rindex(
        "current_restore_required=0", 0, successful_deploy
    )

    assert disarm < clear_controls
    assert disarm < clear_current


def test_real_model_release_smoke_requires_flash_and_reasoning_each_turn() -> None:
    release = (
        ROOT / "scripts" / "deploy_agent2_v22_flash_20260812.sh"
    ).read_text(encoding="utf-8")
    smoke = (
        ROOT / "scripts" / "smoke_20260812_final_date_matrix_rollback.py"
    ).read_text(encoding="utf-8")
    capture = (
        ROOT / "scripts" / "smoke_20260811_overnight_daily_rollback.py"
    ).read_text(encoding="utf-8")

    assert 'result["served_models"] == ["deepseek-v4-flash"]' in release
    assert 'result["all_model_turns_have_reasoning"] is True' in release
    assert "reasoning_content_sha256" in smoke
    assert "reasoning_content_sha256" in capture


def test_failure_after_control_commit_still_restores_controls(tmp_path: Path) -> None:
    result, current_marker, old_release, call_log = _inject_post_change_failure(
        tmp_path,
        current_points_to_target=False,
    )

    assert result.returncode == 77
    assert Path(
        current_marker.read_text(encoding="utf-8")
    ).resolve() == old_release.resolve()
    assert "--mode restore" in call_log.read_text(encoding="utf-8")


def test_failure_after_current_switch_restores_code_and_controls(
    tmp_path: Path,
) -> None:
    result, current_marker, old_release, call_log = _inject_post_change_failure(
        tmp_path,
        current_points_to_target=True,
    )

    assert result.returncode == 77
    assert Path(
        current_marker.read_text(encoding="utf-8")
    ).resolve() == old_release.resolve()
    assert "--mode restore" in call_log.read_text(encoding="utf-8")
