#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-daily-recovery-20260820-3b9ad8b"
previous="$releases/ai-review-system-agent2-review-envelope-20260819-82b672a"
current="$releases/current"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
control_script="$candidate/scripts/manage_unified_daily_480722d_controls.py"
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

verify_controls() {
  (
    cd "$candidate"
    set -a
    . /home/ai_review_tunnel/ai-review-system/.env
    set +a
    PYTHONPATH=. "$python" "$control_script" verify
  )
}

rollback_code() {
  local status
  status="${1:-1}"
  trap - ERR INT TERM
  set +e
  if [[ "${code_restore_required:-0}" -eq 1 ]]; then
    switch_current "$previous" agent2-daily-recovery-3b9ad8b-rollback
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

if [[ "$action" == "deploy" ]]; then
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  code_restore_required=0
  trap 'rollback_code "$?"' ERR
  trap 'rollback_code 130' INT
  trap 'rollback_code 143' TERM
  freeze_all_services
  code_restore_required=1
  switch_current "$candidate" agent2-daily-recovery-3b9ad8b-next
  terminate_frozen_services
  wait_healthy "$candidate"
  verify_controls
  trap - ERR INT TERM
  code_restore_required=0
  echo "deployed $candidate"
  exit 0
elif [[ "$action" == "rollback" ]]; then
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not the candidate" >&2
    exit 1
  fi
  freeze_all_services
  code_restore_required=1
  rollback_code 0
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
