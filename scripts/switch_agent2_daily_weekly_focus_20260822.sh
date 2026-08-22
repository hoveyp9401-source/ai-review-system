#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-daily-weekly-brief-20260822-v1"
previous="$releases/ai-review-system-agent2-daily-reliability-20260821-v2"
current="$releases/current"
shared_env=/home/ai_review_tunnel/ai-review-system/.env
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
roster_backup=/home/ai_review_tunnel/deploy_backups/formal-roster-70-4-20260822-deploy.json
roster_backup_sha="$roster_backup.sha256"
daily_focus_output=/home/ai_review_tunnel/deploy_backups/daily-focus-postdeploy-20260822.json
alignment_output=/home/ai_review_tunnel/deploy_backups/agent2-alignment-postdeploy-20260822.json
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

roster_applied=0
weekly_schema_applied=0
expected_roster_script_sha=f5c6808b5a09ffdba029c50444eea7301fffc89dcfaf8715926f23bb63940a81
expected_weekly_create_sha=81f13205826e8d11a8deb6371a9e477ba5aeb13de79402c0474f71463b08945c
expected_weekly_rollback_sha=2a16ae92342b72e8ba1ecf91f23f2c6a93a3f54b308bde5c224d8ec614b2ee04
expected_daily_focus_smoke_sha=4caa73e926ce66645e9f49d06c733778e127a1cd6367e5edfbb611afafc959dd

run_roster() {
  local roster_action="$1"
  shift
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    PYTHONPATH=. "$python" scripts/manage_formal_roster_70_4_20260822.py \
      "$roster_action" "$@"
  )
}

run_weekly_sql() {
  local sql_file="$1"
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    local dburl="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
    psql "$dburl" -X --set=ON_ERROR_STOP=1 --single-transaction \
      --file "$candidate/$sql_file"
  )
}

weekly_table_exists() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    local dburl="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
    [[ "$(psql "$dburl" -X -At --set=ON_ERROR_STOP=1 \
      --command "SELECT to_regclass('public.agent2_personal_weekly_briefs') IS NOT NULL")" == "t" ]]
  )
}

verify_weekly_switches_off() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    PYTHONPATH=. "$python" - <<'PY'
from app.config import get_settings

get_settings.cache_clear()
settings = get_settings()
if settings.agent2_personal_weekly_brief_enabled:
    raise SystemExit("personal weekly brief generation must remain disabled")
if settings.agent2_personal_weekly_brief_send_enabled:
    raise SystemExit("personal weekly brief sending must remain disabled")
PY
  )
}

verify_candidate_files() {
  local actual
  if [[ -n "$(find "$candidate" -perm /222 -print -quit)" ]]; then
    echo "candidate release contains writable paths" >&2
    return 1
  fi
  actual="$(sha256sum "$candidate/scripts/manage_formal_roster_70_4_20260822.py" | awk '{print $1}')"
  [[ "$actual" == "$expected_roster_script_sha" ]]
  actual="$(sha256sum "$candidate/scripts/create_agent2_personal_weekly_briefs.sql" | awk '{print $1}')"
  [[ "$actual" == "$expected_weekly_create_sha" ]]
  actual="$(sha256sum "$candidate/scripts/rollback_agent2_personal_weekly_briefs.sql" | awk '{print $1}')"
  [[ "$actual" == "$expected_weekly_rollback_sha" ]]
  actual="$(sha256sum "$candidate/scripts/smoke_20260822_daily_focus_weekly_submit_rollback.py" | awk '{print $1}')"
  [[ "$actual" == "$expected_daily_focus_smoke_sha" ]]
}

verify_roster_history_and_leads() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    PYTHONPATH=. "$python" - <<'PY'
import asyncio
from datetime import date

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.legal_daily_roster import load_formal_legal_daily_roster


async def main() -> None:
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    async with AsyncSessionLocal() as session:
        historical = await load_formal_legal_daily_roster(
            session,
            tenant_id=tenant_id,
            on_date=date(2026, 8, 21),
        )
        current = await load_formal_legal_daily_roster(
            session,
            tenant_id=tenant_id,
            on_date=date(2026, 8, 22),
        )
        if (len(historical.child_members), len(historical.center_members)) != (72, 2):
            raise RuntimeError("historical roster is not 72+2")
        if (len(current.child_members), len(current.center_members)) != (70, 4):
            raise RuntimeError("current roster is not 70+4")
        lead_count = int(
            await session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM legal_daily_access_assignments assignments
                    JOIN users ON users.id::text = assignments.principal_user_id
                    JOIN teams ON teams.id = assignments.team_id
                    WHERE assignments.active IS TRUE
                      AND assignments.effective_from <= DATE '2026-08-22'
                      AND (
                          assignments.effective_to IS NULL
                          OR assignments.effective_to >= DATE '2026-08-22'
                      )
                      AND (
                          (users.name = '丁益明' AND teams.code = 'monthly-law-2')
                          OR (users.name = '薛旭' AND teams.code = 'monthly-law-4')
                      )
                      AND assignments.dashboard_role = 'team_lead'
                    """
                )
            )
            or 0
        )
        if lead_count != 2:
            raise RuntimeError("team lead responsibilities changed")
        await session.rollback()
    await engine.dispose()


asyncio.run(main())
PY
  )
}

run_control_alignment() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    umask 077
    PYTHONPATH=. "$python" scripts/verify_agent2_runtime_control_alignment.py \
      --code-root "$candidate" \
      --expected-model deepseek-v4-flash \
      >"$alignment_output"
  )
}

run_daily_focus_postdeploy_smoke() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    umask 077
    unset SMOKE_CASE_NAMES
    PYTHONPATH=. "$python" \
      scripts/smoke_20260822_daily_focus_weekly_submit_rollback.py \
      >"$daily_focus_output"
    "$python" - "$daily_focus_output" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "bare",
    "natural",
    "followup",
    "daily_plan_followup",
    "explicit_weekly_switch",
}
actual = {str(item.get("name") or "") for item in payload.get("results", [])}
if payload.get("status") != "pass" or payload.get("passed") != 5:
    raise SystemExit("Daily focus smoke did not pass all five cases")
if payload.get("failed") != 0 or payload.get("failures"):
    raise SystemExit("Daily focus smoke contains a failed case")
if actual != expected:
    raise SystemExit("Daily focus smoke case set is incomplete")
if payload.get("dingtalk_send_calls") != 0 or payload.get("transport_enabled_cases") != 0:
    raise SystemExit("Daily focus smoke transport was not fully disabled")
if any(int(value) != 0 for value in payload.get("rollback_residue", {}).values()):
    raise SystemExit("Daily focus smoke left rollback residue")
if any(int(value) != 0 for value in payload.get("production_state_changes", {}).values()):
    raise SystemExit("Daily focus smoke changed production state")
PY
  )
}

switch_current() {
  local target="$1"
  local label="$2"
  local temporary="$releases/.current-$label"
  if [[ -e "$temporary" || -L "$temporary" ]]; then
    echo "temporary path already exists: $temporary" >&2
    return 1
  fi
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$current"
}

frozen_pids=()
processes_frozen=0

resume_partial_freeze() {
  local pid
  for pid in "${frozen_pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
  done
  frozen_pids=()
  processes_frozen=0
}

all_frozen_pids_are_stopped() {
  local pid state
  for pid in "${frozen_pids[@]}"; do
    state="$(awk '/^State:/{print $2}' "/proc/$pid/status" 2>/dev/null || true)"
    if [[ "$state" != "T" ]]; then
      echo "service process is not frozen: $pid:$state" >&2
      return 1
    fi
  done
}

all_rollback_pids_are_stopped_or_exited() {
  local pid state
  for pid in "${frozen_pids[@]}"; do
    state="$(awk '/^State:/{print $2}' "/proc/$pid/status" 2>/dev/null || true)"
    if [[ -z "$state" && ! -e "/proc/$pid/status" ]]; then
      continue
    fi
    if [[ "$state" != "T" ]]; then
      echo "rollback service process is not frozen: $pid:$state" >&2
      return 1
    fi
  done
}

freeze_all_services() {
  local service pid
  local service_pids=()
  frozen_pids=()
  processes_frozen=0
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
      echo "invalid MainPID for $service: $pid" >&2
      return 1
    fi
    service_pids+=("$pid")
  done
  for pid in "${service_pids[@]}"; do
    if ! kill -STOP "$pid"; then
      resume_partial_freeze
      return 1
    fi
    frozen_pids+=("$pid")
  done
  sleep 0.2
  if ! all_frozen_pids_are_stopped; then
    resume_partial_freeze
    return 1
  fi
  processes_frozen=1
}

freeze_running_services_for_rollback() {
  local service pid
  frozen_pids=()
  processes_frozen=0
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value 2>/dev/null || true)"
    if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
      echo "rollback found no running process for $service; restoring code pointer" >&2
      continue
    fi
    if ! kill -STOP "$pid" 2>/dev/null; then
      if [[ ! -e "/proc/$pid/status" ]]; then
        echo "rollback process exited before freeze: $service:$pid" >&2
        continue
      fi
      resume_partial_freeze
      return 1
    fi
    frozen_pids+=("$pid")
  done
  sleep 0.2
  if ! all_rollback_pids_are_stopped_or_exited; then
    resume_partial_freeze
    return 1
  fi
  processes_frozen=1
}

terminate_frozen_services() {
  local pid
  for pid in "${frozen_pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${frozen_pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
  done
  frozen_pids=()
  processes_frozen=0
}

wait_healthy() {
  local expected="$1"
  local attempt service pid cwd all_ready
  for attempt in $(seq 1 50); do
    all_ready=true
    for service in "${services[@]}"; do
      if [[ "$(systemctl is-active "$service" 2>/dev/null || true)" != "active" ]]; then
        all_ready=false
        break
      fi
      pid="$(systemctl show "$service" -p MainPID --value)"
      cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
      if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 || "$cwd" != "$expected" ]]; then
        all_ready=false
        break
      fi
    done
    if [[ "$all_ready" == true ]] && curl -fsS --max-time 2 \
      http://127.0.0.1:8000/health >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback() {
  local status="${1:-1}"
  local rollback_failed=0
  local target
  trap - ERR INT TERM
  set +e
  if [[ "$processes_frozen" -eq 0 ]]; then
    if ! freeze_running_services_for_rollback; then
      echo "could not freeze running services before rollback" >&2
      exit 1
    fi
  fi

  # Signals and post-commit read failures can make in-memory flags stale.
  # Re-read both migrations while traffic is frozen before deciding recovery.
  if run_roster verify-after >/dev/null 2>&1; then
    roster_applied=1
  elif run_roster verify-before >/dev/null 2>&1; then
    roster_applied=0
  else
    roster_applied=1
    rollback_failed=1
  fi
  if weekly_table_exists; then
    weekly_schema_applied=1
  else
    weekly_schema_applied=0
  fi

  if [[ "$weekly_schema_applied" -eq 1 ]]; then
    run_weekly_sql scripts/rollback_agent2_personal_weekly_briefs.sql || true
    if ! weekly_table_exists; then
      weekly_schema_applied=0
    else
      rollback_failed=1
    fi
  fi
  if [[ "$roster_applied" -eq 1 ]]; then
    if [[ -f "$roster_backup_sha" ]] \
      && (cd "$(dirname "$roster_backup")" && sha256sum -c "$(basename "$roster_backup_sha")" >/dev/null) \
      && run_roster restore --backup-path "$roster_backup"; then
      true
    fi
    if run_roster verify-before >/dev/null 2>&1; then
      roster_applied=0
    else
      rollback_failed=1
    fi
  fi

  if [[ "$roster_applied" -eq 0 ]]; then
    target="$previous"
    if [[ "$(readlink -f "$current")" != "$previous" ]]; then
      switch_current "$previous" agent2-daily-weekly-brief-rollback \
        || rollback_failed=1
    fi
  else
    # A failed roster restore must stay on the code that understands 70+4.
    target="$candidate"
    if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
      switch_current "$candidate" agent2-daily-weekly-brief-safe-fallback \
        || rollback_failed=1
    fi
  fi

  terminate_frozen_services
  wait_healthy "$target" || rollback_failed=1
  if [[ "$rollback_failed" -ne 0 ]]; then
    echo "rollback needs manual attention; services kept on the safest available release" >&2
    exit 1
  fi
  exit "$status"
}

if [[ ! -d "$candidate" || -L "$candidate" || ! -d "$previous" || -L "$previous" ]]; then
  echo "release directory is missing or unsafe" >&2
  exit 1
fi
if [[ ! -f "$shared_env" || -L "$shared_env" ]]; then
  echo "shared environment is missing or unsafe" >&2
  exit 1
fi
for required_file in \
  scripts/manage_formal_roster_70_4_20260822.py \
  scripts/create_agent2_personal_weekly_briefs.sql \
  scripts/rollback_agent2_personal_weekly_briefs.sql \
  scripts/smoke_20260822_daily_focus_weekly_submit_rollback.py; do
  if [[ ! -f "$candidate/$required_file" || -L "$candidate/$required_file" ]]; then
    echo "candidate release file is missing or unsafe: $required_file" >&2
    exit 1
  fi
done
verify_candidate_files

if [[ "$action" == "deploy" ]]; then
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  if [[ -e "$roster_backup" || -L "$roster_backup" \
    || -e "$roster_backup_sha" || -L "$roster_backup_sha" \
    || -e "$daily_focus_output" || -L "$daily_focus_output" \
    || -e "$alignment_output" || -L "$alignment_output" ]]; then
    echo "final deployment evidence path already exists" >&2
    exit 1
  fi
  verify_weekly_switches_off
  run_roster verify-before
  if weekly_table_exists; then
    echo "personal weekly brief table already exists before deploy" >&2
    exit 1
  fi
  trap 'rollback "$?"' ERR
  trap 'rollback 130' INT
  trap 'rollback 143' TERM
  freeze_all_services
  run_roster backup --backup-path "$roster_backup"
  [[ "$(stat -c %a "$roster_backup")" == "600" ]]
  (
    cd "$(dirname "$roster_backup")"
    umask 077
    sha256sum "$(basename "$roster_backup")" \
      >"$(basename "$roster_backup_sha")"
    sha256sum -c "$(basename "$roster_backup_sha")" >/dev/null
  )
  run_roster apply --backup-path "$roster_backup"
  roster_applied=1
  run_weekly_sql scripts/create_agent2_personal_weekly_briefs.sql
  weekly_schema_applied=1
  switch_current "$candidate" agent2-daily-weekly-brief-next
  terminate_frozen_services
  wait_healthy "$candidate"
  run_roster verify-after
  weekly_table_exists
  verify_weekly_switches_off
  verify_roster_history_and_leads
  run_control_alignment
  [[ "$(stat -c %a "$alignment_output")" == "600" ]]
  run_daily_focus_postdeploy_smoke
  [[ "$(stat -c %a "$daily_focus_output")" == "600" ]]
  wait_healthy "$candidate"
  run_roster verify-after
  weekly_table_exists
  verify_weekly_switches_off
  trap - ERR INT TERM
  echo "deployed $candidate"
elif [[ "$action" == "rollback" ]]; then
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not the Daily and weekly brief candidate" >&2
    exit 1
  fi
  if [[ ! -f "$roster_backup" || -L "$roster_backup" \
    || "$(stat -c %a "$roster_backup")" != "600" \
    || ! -f "$roster_backup_sha" || -L "$roster_backup_sha" ]]; then
    echo "final roster deployment backup is missing or unsafe" >&2
    exit 1
  fi
  roster_applied=1
  weekly_schema_applied=1
  rollback 0
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
