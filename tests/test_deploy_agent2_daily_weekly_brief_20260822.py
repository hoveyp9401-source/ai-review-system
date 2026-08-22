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


def test_successful_deploy_rechecks_roster_table_and_disabled_switches() -> None:
    deploy = _source().split('if [[ "$action" == "deploy" ]]', 1)[1].split(
        'elif [[ "$action" == "rollback" ]]', 1
    )[0]
    health = deploy.index('wait_healthy "$candidate"')

    assert deploy.index("run_roster verify-after", health) > health
    assert deploy.index("weekly_table_exists", health) > health
    assert deploy.index("verify_weekly_switches_off", health) > health


def test_manual_rollback_arms_both_data_restore_guards() -> None:
    manual = _source().split('elif [[ "$action" == "rollback" ]]', 1)[1]

    assert manual.index("roster_applied=1") < manual.index("rollback 0")
    assert manual.index("weekly_schema_applied=1") < manual.index("rollback 0")
