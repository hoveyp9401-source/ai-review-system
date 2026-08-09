#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
previous="$releases/ai-review-system-daily-triage-20260804-v1"
candidate="$releases/ai-review-system-daily-triage-20260804-v2"
current="$releases/current"
pinned="$releases/ai-review-system-performance-grounded-20260730-3e32903"
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

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
  local temp="$releases/.team-effective-date-$(basename "$link")-next"
  if [[ -e "$temp" || -L "$temp" ]]; then
    echo "temporary switch path already exists: $temp" >&2
    exit 1
  fi
  ln -s "$target" "$temp"
  mv -Tf "$temp" "$link"
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

require_real_directory "$previous"
require_real_directory "$candidate"

if [[ "$action" == "deploy" ]]; then
  require_link_target "$current" "$previous"
  require_link_target "$pinned" "$previous"
  switch_link "$current" "$candidate"
  switch_link "$pinned" "$candidate"
  restart_by_owner_signal
  echo "team-effective-date v2 release paths switched; restart signals sent"
elif [[ "$action" == "rollback" ]]; then
  require_link_target "$current" "$candidate"
  require_link_target "$pinned" "$candidate"
  switch_link "$current" "$previous"
  switch_link "$pinned" "$previous"
  restart_by_owner_signal
  echo "team-effective-date v2 rolled back; restart signals sent"
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
