#!/usr/bin/env bash
set -euo pipefail

releases=/home/ai_review_tunnel/releases
backups=/home/ai_review_tunnel/backups
candidate="$releases/ai-review-system-agent2-v22-completed-edit-8am-20260812-candidate"
release="$releases/ai-review-system-agent2-v22-completed-edit-8am-20260812-b88d74c"
old_release="$releases/ai-review-system-agent2-v22-briefing-detail-20260811-e8a60c5"
current="$releases/current"
next_link="$releases/.current-completed-edit-8am-20260812-next"
rollback_link="$releases/.current-completed-edit-8am-20260812-rollback"
shared_env=/home/ai_review_tunnel/ai-review-system/.env
env_backup="$backups/completed-edit-8am-20260812-env-before"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
health_url=http://127.0.0.1:8000/health
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

switched=0
env_changed=0

require_real_directory() {
  local path="$1"
  if [[ ! -d "$path" || -L "$path" ]]; then
    printf 'expected real directory: %s\n' "$path" >&2
    exit 1
  fi
}

restart_services() {
  local service pid
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
      printf 'invalid MainPID for %s: %s\n' "$service" "$pid" >&2
      return 1
    fi
    kill -TERM "$pid"
  done
}

wait_for_release() {
  local expected="$1"
  local attempt service pid cwd all_ready
  for attempt in $(seq 1 40); do
    all_ready=1
    for service in "${services[@]}"; do
      if [[ "$(systemctl is-active "$service")" != "active" ]]; then
        all_ready=0
        break
      fi
      pid="$(systemctl show "$service" -p MainPID --value)"
      if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
        all_ready=0
        break
      fi
      cwd="$(readlink -f "/proc/$pid/cwd" || true)"
      if [[ "$cwd" != "$expected" ]]; then
        all_ready=0
        break
      fi
    done
    if [[ "$all_ready" -eq 1 ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_health() {
  local attempt stable
  stable=0
  for attempt in $(seq 1 40); do
    if curl --fail --silent --show-error "$health_url" >/dev/null 2>&1; then
      stable=$((stable + 1))
      if [[ "$stable" -ge 3 ]]; then
        return 0
      fi
    else
      stable=0
    fi
    sleep 1
  done
  return 1
}

verify_effective_hour() {
  local expected="$1"
  local target_release="$2"
  (
    cd "$target_release"
    PYTHONPATH=. "$python" -c \
      "from app.config import get_settings; value=get_settings().auto_submit_cron_hour; assert value == $expected, value; print(value)"
  )
  local scheduler_pid
  scheduler_pid="$(systemctl show ai-review-scheduler.service -p MainPID --value)"
  tr '\0' '\n' <"/proc/$scheduler_pid/environ" \
    | grep -qx "AUTO_SUBMIT_CRON_HOUR=$expected"
}

restore_old_release() {
  local service pid
  if [[ -e "$rollback_link" || -L "$rollback_link" ]]; then
    rm -f -- "$rollback_link"
  fi
  ln -s "$old_release" "$rollback_link"
  mv -Tf "$rollback_link" "$current"
  if [[ "$env_changed" -eq 1 ]]; then
    cp -p -- "$env_backup" "$shared_env"
  fi
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]]; then
      kill -TERM "$pid"
    fi
  done
  wait_for_release "$old_release"
  wait_for_health
  verify_effective_hour 23 "$old_release"
}

rollback_after_failure() {
  local status
  status=$?
  trap - ERR
  set +e
  if [[ "$switched" -eq 1 || "$env_changed" -eq 1 ]]; then
    restore_old_release
    printf 'deployment failed; release and schedule restored\n' >&2
  fi
  exit "$status"
}

trap rollback_after_failure ERR

require_real_directory "$candidate"
require_real_directory "$old_release"
if [[ "$(readlink -f "$current")" != "$old_release" ]]; then
  printf 'current release changed before deployment\n' >&2
  exit 1
fi
if [[ "$(readlink -f "$candidate/.env")" != "$shared_env" ]]; then
  printf 'candidate environment link is invalid\n' >&2
  exit 1
fi
if [[ -e "$next_link" || -L "$next_link" || -e "$rollback_link" || -L "$rollback_link" ]]; then
  printf 'temporary deployment path already exists\n' >&2
  exit 1
fi
if [[ -e "$env_backup" || -L "$env_backup" ]]; then
  printf 'environment backup already exists\n' >&2
  exit 1
fi
if [[ "$(grep -c '^AUTO_SUBMIT_CRON_HOUR=23$' "$shared_env")" -ne 1 ]]; then
  printf 'expected one AUTO_SUBMIT_CRON_HOUR=23 line before deployment\n' >&2
  exit 1
fi

umask 077
cp -p -- "$shared_env" "$env_backup"
if [[ -e "$release" || -L "$release" ]]; then
  printf 'release path already exists\n' >&2
  exit 1
fi
cp -a -- "$candidate" "$release"
require_real_directory "$release"

sed -i 's/^AUTO_SUBMIT_CRON_HOUR=23$/AUTO_SUBMIT_CRON_HOUR=8/' "$shared_env"
env_changed=1
if [[ "$(grep -c '^AUTO_SUBMIT_CRON_HOUR=8$' "$shared_env")" -ne 1 ]]; then
  printf 'failed to set AUTO_SUBMIT_CRON_HOUR=8\n' >&2
  exit 1
fi

ln -s "$release" "$next_link"
mv -Tf "$next_link" "$current"
switched=1
restart_services
wait_for_release "$release"
wait_for_health
verify_effective_hour 8 "$release"

(
  cd "$release"
  PYTHONPATH=. "$python" -c \
    "from datetime import date; from app.scheduler.runner import _auto_submit_report_date; assert _auto_submit_report_date(date(2026, 8, 13)) == date(2026, 8, 12)"
  PYTHONPATH=. "$python" scripts/smoke_20260812_completed_edits_rollback.py \
    >/tmp/completed-edit-8am-postdeploy-smoke.json
)

switched=0
env_changed=0
trap - ERR
printf 'deployed release: %s\n' "$release"
printf 'environment backup: %s\n' "$env_backup"
