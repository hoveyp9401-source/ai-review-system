#!/usr/bin/env bash
set -euo pipefail

releases=/home/ai_review_tunnel/releases
previous="$releases/ai-review-system-agent2-model-query-20260806-v4"
candidate="$releases/ai-review-system-agent2-model-query-20260806-v5"
current="$releases/current"
pinned="$releases/ai-review-system-performance-grounded-20260730-3e32903"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
backup_dir=/home/ai_review_tunnel/backups/agent2-model-query-20260806-v5-owner-signal
control_backup="$backup_dir/canary-controls-before.json"
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

links_switched=0
controls_updated=0
services_paused=0
deploy_succeeded=0
pids=()

require_real_directory() {
  local path="$1"
  if [[ ! -d "$path" || -L "$path" ]]; then
    echo "expected real release directory: $path" >&2
    exit 1
  fi
}

require_link_target() {
  local link="$1"
  local expected="$2"
  if [[ ! -L "$link" || "$(readlink -f "$link")" != "$expected" ]]; then
    echo "unexpected release link target: $link" >&2
    exit 1
  fi
}

switch_link() {
  local link="$1"
  local target="$2"
  local temp="$releases/.agent2-model-query-$(basename "$link")-next"
  if [[ -e "$temp" || -L "$temp" ]]; then
    echo "temporary switch path already exists: $temp" >&2
    exit 1
  fi
  ln -s "$target" "$temp"
  mv -Tf "$temp" "$link"
}

wait_for_health() {
  local attempt service all_active
  for attempt in $(seq 1 30); do
    all_active=true
    for service in "${services[@]}"; do
      if ! systemctl is-active --quiet "$service"; then
        all_active=false
      fi
    done
    if $all_active && curl -fsS http://127.0.0.1:8000/health >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

pause_services() {
  local service pid
  pids=()
  for service in "${services[@]}"; do
    pid="$(systemctl show "$service" -p MainPID --value)"
    if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
      echo "invalid MainPID for $service: $pid" >&2
      exit 1
    fi
    pids+=("$pid")
  done
  for pid in "${pids[@]}"; do
    kill -STOP "$pid"
  done
  services_paused=1
}

restart_by_owner_signal() {
  local pid
  for pid in "${pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
  done
  services_paused=0
}

rollback() {
  if [[ "$deploy_succeeded" == "1" ]]; then
    return
  fi
  set +e
  if [[ "$links_switched" == "1" ]]; then
    switch_link "$current" "$previous"
    switch_link "$pinned" "$previous"
  fi
  if [[ "$controls_updated" == "1" ]]; then
    cd "$candidate"
    PYTHONPATH="$candidate" "$python" \
      scripts/manage_canary_controls_daily_triage.py restore \
      --backup-path "$control_backup"
  fi
  if [[ "$services_paused" == "1" ]]; then
    restart_by_owner_signal
  fi
  wait_for_health
}

trap rollback EXIT

require_real_directory "$previous"
require_real_directory "$candidate"
require_link_target "$current" "$previous"
require_link_target "$pinned" "$previous"
test -f "$candidate/app/agent2/report_insight_query.py"
test -f "$candidate/scripts/smoke_agent2_report_insight_tool_rollback.py"

mkdir -p "$backup_dir"
test ! -e "$control_backup"
cd "$previous"
PYTHONPATH="$previous" "$python" \
  scripts/manage_canary_controls_daily_triage.py backup \
  --backup-path "$control_backup"

pause_services

switch_link "$current" "$candidate"
switch_link "$pinned" "$candidate"
links_switched=1

cd "$candidate"
PYTHONPATH="$candidate" "$python" \
  scripts/manage_canary_controls_daily_triage.py update \
  --backup-path "$control_backup"
controls_updated=1
PYTHONPATH="$candidate" "$python" \
  scripts/manage_canary_controls_daily_triage.py verify

restart_by_owner_signal
wait_for_health

require_link_target "$current" "$candidate"
require_link_target "$pinned" "$candidate"
deploy_succeeded=1
trap - EXIT

readlink -f "$current"
curl -fsS http://127.0.0.1:8000/health
systemctl show "${services[@]}" -p Id -p ActiveState -p NRestarts --no-pager

