#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
backup_path="${2:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-daily-reliability-20260821-v1"
previous="$releases/ai-review-system-agent2-weekly-plan-full-20260821-v5"
current="$releases/current"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
control_script="$candidate/scripts/manage_agent2_daily_reliability_20260821_controls.py"
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
  local temporary="$releases/.current-$label"
  if [[ -e "$temporary" || -L "$temporary" ]]; then
    echo "temporary path already exists: $temporary" >&2
    return 1
  fi
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$current"
}

run_controls() {
  local mode="$1"
  shift
  (
    cd "$candidate"
    set -a
    . /home/ai_review_tunnel/ai-review-system/.env
    set +a
    PYTHONPATH=. "$python" "$control_script" "$mode" "$@"
  )
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
      echo "could not freeze service process: $pid" >&2
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

freeze_running_services_for_rollback() {
  if [[ "$processes_frozen" -eq 0 ]]; then
    freeze_all_services
  elif ! all_frozen_pids_are_stopped; then
    resume_partial_freeze
    return 1
  fi
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

controls_updated=0
code_switched=0

rollback_all() {
  local status="${1:-1}"
  local rollback_failed=0
  local rollback_frozen=1
  trap - ERR INT TERM
  set +e
  if ! freeze_running_services_for_rollback; then
    rollback_failed=1
    rollback_frozen=0
  fi
  if [[ "$rollback_frozen" -eq 1 ]]; then
    if [[ "$code_switched" -eq 1 ]]; then
      switch_current "$previous" agent2-daily-reliability-rollback \
        || rollback_failed=1
    fi
    if [[ "$controls_updated" -eq 1 ]]; then
      run_controls restore --backup-path "$backup_path" || rollback_failed=1
    fi
    terminate_frozen_services || rollback_failed=1
    wait_healthy "$previous" || rollback_failed=1
  else
    echo "services could not be fully frozen; rollback left code and controls unchanged" >&2
    if [[ "$code_switched" -eq 0 && "$controls_updated" -eq 0 ]]; then
      wait_healthy "$previous" || rollback_failed=1
    fi
  fi
  if [[ "$rollback_failed" -ne 0 ]]; then
    echo "daily reliability rollback did not fully recover; manual intervention required" >&2
    if [[ "$status" -eq 0 ]]; then
      status=1
    fi
  fi
  exit "$status"
}

require_release "$candidate"
require_release "$previous"

if [[ "$action" == "deploy" ]]; then
  if [[ -z "$backup_path" || ! -f "$backup_path" ]]; then
    echo "control backup is required" >&2
    exit 1
  fi
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  trap 'rollback_all "$?"' ERR
  trap 'rollback_all 130' INT
  trap 'rollback_all 143' TERM
  freeze_all_services
  controls_updated=1
  run_controls update --backup-path "$backup_path"
  switch_current "$candidate" agent2-daily-reliability-next
  code_switched=1
  terminate_frozen_services
  wait_healthy "$candidate"
  run_controls verify
  trap - ERR INT TERM
  controls_updated=0
  code_switched=0
  echo "deployed $candidate"
  exit 0
elif [[ "$action" == "rollback" ]]; then
  if [[ -z "$backup_path" || ! -f "$backup_path" ]]; then
    echo "control backup is required" >&2
    exit 1
  fi
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not the daily reliability candidate" >&2
    exit 1
  fi
  controls_updated=1
  code_switched=1
  rollback_all 0
else
  echo "usage: $0 deploy|rollback BACKUP_PATH" >&2
  exit 2
fi
