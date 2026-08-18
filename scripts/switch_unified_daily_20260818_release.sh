#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-smart-daily-20260818-2350ec2"
previous="$releases/ai-review-system-unified-daily-20260818-480722d"
current="$releases/current"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
control_script="$candidate/scripts/manage_unified_daily_480722d_controls.py"
control_backup=/home/ai_review_tunnel/backups/agent2-smart-daily-2350ec2-controls-20260818T1850/controls.before.json
control_backup_sha256=7092f8a3921f02bfba46b595460871a480cca8ddfd0380eb4f77239a4d81838c
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

require_release() {
  local path="$1"
  if [[ ! -d "$path" || -L "$path" ]]; then
    echo "expected release directory: $path" >&2
    exit 1
  fi
}

switch_current() {
  local target="$1"
  local label="$2"
  local temp="$releases/.current-$label"
  if [[ -e "$temp" || -L "$temp" ]]; then
    echo "temporary path already exists: $temp" >&2
    exit 1
  fi
  ln -s "$target" "$temp"
  mv -Tf "$temp" "$current"
}

frozen_pids=()
processes_frozen=0

freeze_all_services() {
  local service pid state
  frozen_pids=()
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
      echo "invalid MainPID for $service: $pid" >&2
      return 1
    fi
    frozen_pids+=("$pid")
    processes_frozen=1
    kill -STOP "$pid"
  done
  sleep 0.2
  for pid in "${frozen_pids[@]}"; do
    state="$(awk '/^State:/{print $2}' "/proc/$pid/status")"
    if [[ "$state" != "T" ]]; then
      echo "service process did not freeze: $pid:$state" >&2
      return 1
    fi
  done
}

terminate_frozen_services() {
  local pid
  for pid in "${frozen_pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${frozen_pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
  done
  processes_frozen=0
  frozen_pids=()
}

restart_by_owner_signal() {
  local service pid
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]]; then
      kill -TERM "$pid"
    fi
  done
}

wait_healthy() {
  local expected="$1"
  local attempt service pid cwd all_ready
  for attempt in $(seq 1 35); do
    all_ready=true
    for service in "${services[@]}"; do
      if [[ "$(systemctl is-active "$service" 2>/dev/null || true)" != "active" ]]; then
        all_ready=false
        break
      fi
      pid="$(systemctl show "$service" -p MainPID --value)"
      if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
        all_ready=false
        break
      fi
      cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
      if [[ "$cwd" != "$expected" ]]; then
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

run_control() {
  local control_action="$1"
  (
    cd "$candidate"
    set -a
    . /home/ai_review_tunnel/ai-review-system/.env
    set +a
    if [[ "$control_action" == "verify" ]]; then
      PYTHONPATH=. "$python" "$control_script" verify
    else
      PYTHONPATH=. "$python" "$control_script" "$control_action" \
        --backup-path "$control_backup"
    fi
  )
}

rollback_code_and_controls() {
  local status
  status="${1:-1}"
  trap - ERR INT TERM
  set +e
  if [[ "${controls_restore_required:-0}" -eq 1 ]]; then
    run_control restore
  fi
  if [[ "${code_restore_required:-0}" -eq 1 ]]; then
    switch_current "$previous" agent2-smart-daily-2350ec2-rollback
  fi
  if [[ "$processes_frozen" -eq 1 ]]; then
    terminate_frozen_services
  else
    restart_by_owner_signal
  fi
  wait_healthy "$previous"
  exit "$status"
}

require_release "$candidate"
require_release "$previous"
if [[ ! -f "$control_backup" || -L "$control_backup" ]]; then
  echo "control backup is missing or not a regular file" >&2
  exit 1
fi
if [[ "$(sha256sum "$control_backup" | cut -d' ' -f1)" != "$control_backup_sha256" ]]; then
  echo "control backup hash mismatch" >&2
  exit 1
fi

if [[ "$action" == "deploy" ]]; then
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  controls_restore_required=0
  code_restore_required=0
  trap 'rollback_code_and_controls "$?"' ERR
  trap 'rollback_code_and_controls 130' INT
  trap 'rollback_code_and_controls 143' TERM
  freeze_all_services
  code_restore_required=1
  switch_current "$candidate" agent2-smart-daily-2350ec2-next
  controls_restore_required=1
  run_control update
  terminate_frozen_services
  wait_healthy "$candidate"
  run_control verify
  trap - ERR INT TERM
  controls_restore_required=0
  code_restore_required=0
  echo "deployed $candidate"
  exit 0
elif [[ "$action" == "rollback" ]]; then
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not the candidate" >&2
    exit 1
  fi
  freeze_all_services
  controls_restore_required=1
  code_restore_required=1
  rollback_code_and_controls 0
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
