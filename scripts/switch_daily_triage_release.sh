#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-daily-triage-20260804-v1"
pinned="$releases/ai-review-system-performance-grounded-20260730-3e32903"
pinned_backup="$releases/ai-review-system-performance-grounded-20260730-3e32903.before-daily-triage-20260804-v1"
current="$releases/current"
old_api="$releases/ai-review-system-performance-report-format-v2-20260803-28e4ae3c"
control_backup=/home/ai_review_tunnel/codex_backups/daily_triage_20260804_before_v1/agent2-canary-controls.before.json
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
control_script="$candidate/scripts/manage_canary_controls_daily_triage.py"
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

require_directory() {
  local path="$1"
  if [[ ! -d "$path" || -L "$path" ]]; then
    echo "expected real directory: $path" >&2
    exit 1
  fi
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

restore_paths_after_failed_update() {
  local temp="$releases/.current-daily-triage-v1-rollback"
  if [[ -e "$temp" || -L "$temp" ]]; then
    echo "rollback temp path already exists: $temp" >&2
    exit 1
  fi
  ln -s "$old_api" "$temp"
  mv -Tf "$temp" "$current"
  if [[ -L "$pinned" && "$(readlink -f "$pinned")" == "$candidate" ]]; then
    unlink "$pinned"
  fi
  if [[ ! -e "$pinned" && -d "$pinned_backup" && ! -L "$pinned_backup" ]]; then
    mv "$pinned_backup" "$pinned"
  fi
}

if [[ "$action" == "deploy" ]]; then
  require_directory "$candidate"
  require_directory "$pinned"
  require_directory "$old_api"
  if [[ -e "$pinned_backup" || -L "$pinned_backup" ]]; then
    echo "pinned backup already exists: $pinned_backup" >&2
    exit 1
  fi
  if [[ "$(readlink -f "$current")" != "$old_api" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  temp="$releases/.current-daily-triage-v1-next"
  if [[ -e "$temp" || -L "$temp" ]]; then
    echo "deploy temp path already exists: $temp" >&2
    exit 1
  fi

  mv "$pinned" "$pinned_backup"
  ln -s "$candidate" "$pinned"
  ln -s "$candidate" "$temp"
  mv -Tf "$temp" "$current"
  if ! (
    cd "$candidate"
    PYTHONPATH=. "$python" "$control_script" update \
      --backup-path "$control_backup"
  ); then
    restore_paths_after_failed_update
    exit 1
  fi
  restart_by_owner_signal
  echo "daily-triage-v1 release paths switched; restart signals sent"
elif [[ "$action" == "rollback" ]]; then
  require_directory "$candidate"
  if [[ ! -L "$pinned" || "$(readlink -f "$pinned")" != "$candidate" ]]; then
    echo "pinned release is not the daily-triage candidate" >&2
    exit 1
  fi
  require_directory "$pinned_backup"
  (
    cd "$candidate"
    PYTHONPATH=. "$python" "$control_script" restore \
      --backup-path "$control_backup"
  )
  restore_paths_after_failed_update
  restart_by_owner_signal
  echo "daily-triage-v1 rolled back; restart signals sent"
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
