#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-unified-daily-20260818-5d23c6c"
previous="$releases/ai-review-system-unified-daily-20260818-8eab022"
current="$releases/current"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
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

restart_by_owner_signal() {
  local service pid
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
      echo "invalid MainPID for $service: $pid" >&2
      exit 1
    fi
    kill -TERM "$pid"
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

require_release "$candidate"
require_release "$previous"

if [[ "$action" == "deploy" ]]; then
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  switch_current "$candidate" unified-daily-5d23c6c-next
  restart_by_owner_signal
  if wait_healthy "$candidate"; then
    echo "deployed $candidate"
    exit 0
  fi
  echo "candidate health check failed; rolling back" >&2
  switch_current "$previous" unified-daily-5d23c6c-rollback
  restart_by_owner_signal
  wait_healthy "$previous"
  exit 1
elif [[ "$action" == "rollback" ]]; then
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not the candidate" >&2
    exit 1
  fi
  switch_current "$previous" unified-daily-5d23c6c-manual-rollback
  restart_by_owner_signal
  wait_healthy "$previous"
  echo "rolled back to $previous"
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
