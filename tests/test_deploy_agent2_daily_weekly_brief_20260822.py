from pathlib import Path


SCRIPT = Path("scripts/switch_agent2_daily_weekly_focus_20260822.sh")


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_combined_release_uses_new_immutable_candidate_and_old_v2_rollback() -> None:
    source = _source()

    assert "ai-review-system-agent2-daily-weekly-brief-20260822-v1" in source
    assert "ai-review-system-agent2-daily-reliability-20260821-v2" in source


def test_weekly_brief_switches_and_pre_migration_state_are_checked_before_freeze() -> None:
    deploy = _source().split('if [[ "$action" == "deploy" ]]', 1)[1].split(
        'elif [[ "$action" == "rollback" ]]', 1
    )[0]

    assert deploy.index("verify_weekly_switches_off") < deploy.index(
        "freeze_all_services"
    )
    assert deploy.index("run_roster verify-before") < deploy.index(
        "freeze_all_services"
    )
    assert deploy.index("weekly_table_exists") < deploy.index("freeze_all_services")


def test_roster_schema_and_code_switch_happen_while_services_are_frozen() -> None:
    deploy = _source().split('if [[ "$action" == "deploy" ]]', 1)[1].split(
        'elif [[ "$action" == "rollback" ]]', 1
    )[0]

    freeze = deploy.index("freeze_all_services")
    roster = deploy.index("run_roster apply", freeze)
    schema = deploy.index("run_weekly_sql scripts/create_agent2", roster)
    switch = deploy.index('switch_current "$candidate"', schema)
    restart = deploy.index("terminate_frozen_services", switch)
    health = deploy.index('wait_healthy "$candidate"', restart)
    assert freeze < roster < schema < switch < restart < health


def test_schema_files_run_in_one_outer_transaction() -> None:
    source = _source()

    assert "--single-transaction" in source
    assert "--set=ON_ERROR_STOP=1" in source
    assert "public.agent2_personal_weekly_briefs" in source


def test_candidate_is_read_only_and_key_release_files_are_hash_pinned() -> None:
    source = _source()

    assert 'find "$candidate" -perm /222' in source
    assert "expected_roster_script_sha=" in source
    assert "expected_weekly_create_sha=" in source
    assert "expected_weekly_rollback_sha=" in source
    assert "expected_daily_focus_smoke_sha=" in source


def test_failed_deploy_restores_data_before_switching_to_old_code() -> None:
    rollback = _source().split("rollback() {", 1)[1].split(
        'if [[ ! -d "$candidate"', 1
    )[0]

    schema_restore = rollback.index(
        "run_weekly_sql scripts/rollback_agent2_personal_weekly_briefs.sql"
    )
    roster_restore = rollback.index("run_roster restore", schema_restore)
    old_switch = rollback.index('switch_current "$previous"', roster_restore)
    restart = rollback.index("terminate_frozen_services", old_switch)
    assert schema_restore < roster_restore < old_switch < restart


def test_failed_roster_restore_keeps_code_that_understands_70_plus_4() -> None:
    rollback = _source().split("rollback() {", 1)[1].split(
        'if [[ ! -d "$candidate"', 1
    )[0]

    assert 'if [[ "$roster_applied" -eq 0 ]]' in rollback
    assert 'target="$previous"' in rollback
    assert 'target="$candidate"' in rollback
    assert "safe-fallback" in rollback


def test_rollback_reloads_actual_database_state_instead_of_trusting_flags() -> None:
    rollback = _source().split("rollback() {", 1)[1].split(
        'if [[ ! -d "$candidate"', 1
    )[0]

    refresh_after_freeze = rollback.index("Re-read both migrations")
    verify_after = rollback.index("run_roster verify-after", refresh_after_freeze)
    verify_before = rollback.index("run_roster verify-before", verify_after)
    table_check = rollback.index("weekly_table_exists", verify_before)
    schema_restore = rollback.index("run_weekly_sql scripts/rollback", table_check)
    assert refresh_after_freeze < verify_after < verify_before < table_check < schema_restore


def test_final_private_backup_is_created_after_freeze_and_before_apply() -> None:
    deploy = _source().split('if [[ "$action" == "deploy" ]]', 1)[1].split(
        'elif [[ "$action" == "rollback" ]]', 1
    )[0]

    freeze = deploy.index("freeze_all_services")
    backup = deploy.index("run_roster backup", freeze)
    checksum = deploy.index("sha256sum", backup)
    apply = deploy.index("run_roster apply", checksum)
    assert freeze < backup < checksum < apply


def test_successful_deploy_rechecks_roster_table_and_disabled_switches() -> None:
    deploy = _source().split('if [[ "$action" == "deploy" ]]', 1)[1].split(
        'elif [[ "$action" == "rollback" ]]', 1
    )[0]
    health = deploy.index('wait_healthy "$candidate"')

    assert deploy.index("run_roster verify-after", health) > health
    assert deploy.index("weekly_table_exists", health) > health
    assert deploy.index("verify_weekly_switches_off", health) > health


def test_success_path_keeps_rollback_armed_through_online_business_gates() -> None:
    deploy = _source().split('if [[ "$action" == "deploy" ]]', 1)[1].split(
        'elif [[ "$action" == "rollback" ]]', 1
    )[0]

    history = deploy.index("verify_roster_history_and_leads")
    alignment = deploy.index("run_control_alignment", history)
    daily_smoke = deploy.index("run_daily_focus_postdeploy_smoke", alignment)
    final_health = deploy.index('wait_healthy "$candidate"', daily_smoke)
    disarm = deploy.index("trap - ERR INT TERM", final_health)
    assert history < alignment < daily_smoke < final_health < disarm


def test_daily_focus_gate_forces_and_parses_the_exact_five_case_matrix() -> None:
    source = _source()
    smoke = source.split("run_daily_focus_postdeploy_smoke() {", 1)[1].split(
        "switch_current() {", 1
    )[0]

    assert "unset SMOKE_CASE_NAMES" in smoke
    assert 'payload.get("passed") != 5' in smoke
    for case_name in (
        "bare",
        "natural",
        "followup",
        "daily_plan_followup",
        "explicit_weekly_switch",
    ):
        assert f'"{case_name}"' in smoke
    assert 'payload.get("transport_enabled_cases") != 0' in smoke
    assert 'payload.get("rollback_residue", {})' in smoke
    assert 'payload.get("production_state_changes", {})' in smoke


def test_manual_rollback_arms_both_data_restore_guards() -> None:
    manual = _source().split('elif [[ "$action" == "rollback" ]]', 1)[1]

    assert manual.index("roster_applied=1") < manual.index("rollback 0")
    assert manual.index("weekly_schema_applied=1") < manual.index("rollback 0")
