#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-daily-weekly-focus-20260822-v1"
previous="$releases/ai-review-system-agent2-daily-reliability-20260821-v2"
current="$releases/current"
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

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
  trap - ERR INT TERM
  set +e
  if [[ "$processes_frozen" -eq 0 ]]; then
    if ! freeze_running_services_for_rollback; then
      echo "could not freeze running services before rollback" >&2
      exit 1
    fi
  fi
  switch_current "$previous" agent2-daily-weekly-focus-rollback || exit 1
  terminate_frozen_services
  wait_healthy "$previous" || exit 1
  exit "$status"
}

if [[ ! -d "$candidate" || -L "$candidate" || ! -d "$previous" || -L "$previous" ]]; then
  echo "release directory is missing or unsafe" >&2
  exit 1
fi

if [[ "$action" == "deploy" ]]; then
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  trap 'rollback "$?"' ERR
  trap 'rollback 130' INT
  trap 'rollback 143' TERM
  freeze_all_services
  switch_current "$candidate" agent2-daily-weekly-focus-next
  terminate_frozen_services
  wait_healthy "$candidate"
  trap - ERR INT TERM
  echo "deployed $candidate"
elif [[ "$action" == "rollback" ]]; then
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not the Daily focus candidate" >&2
    exit 1
  fi
  rollback 0
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
