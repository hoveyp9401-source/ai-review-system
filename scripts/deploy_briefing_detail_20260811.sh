#!/usr/bin/env bash
set -euo pipefail

releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-v22-briefing-detail-20260811-candidate"
release="$releases/ai-review-system-agent2-v22-briefing-detail-20260811-e8a60c5"
old_release="$releases/ai-review-system-agent2-v22-overnight-20260811-3483ed9a"
current="$releases/current"
next_link="$releases/.current-briefing-detail-20260811-next"
rollback_link="$releases/.current-briefing-detail-20260811-rollback"
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
health_url=http://127.0.0.1:8000/health
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

switched=0

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

wait_for_services() {
  local attempt service pid cwd all_ready
  for attempt in $(seq 1 30); do
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
      if [[ "$cwd" != "$release" ]]; then
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
  for attempt in $(seq 1 30); do
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

rollback_after_failure() {
  local status service pid
  status=$?
  trap - ERR
  set +e
  if [[ "$switched" -eq 1 ]]; then
    if [[ ! -e "$rollback_link" && ! -L "$rollback_link" ]]; then
      ln -s "$old_release" "$rollback_link"
      mv -Tf "$rollback_link" "$current"
    fi
    for service in "${services[@]}"; do
      pid="$(systemctl show "$service" -p MainPID --value)"
      if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]]; then
        kill -TERM "$pid"
      fi
    done
    printf 'deployment failed; current release restored to %s\n' "$old_release" >&2
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
if [[ -e "$next_link" || -L "$next_link" || -e "$rollback_link" || -L "$rollback_link" ]]; then
  printf 'temporary deployment path already exists\n' >&2
  exit 1
fi
if [[ -e "$release" || -L "$release" ]]; then
  require_real_directory "$release"
else
  cp -a -- "$candidate" "$release"
fi

require_real_directory "$release"
if [[ "$(readlink -f "$release/.env")" != "/home/ai_review_tunnel/ai-review-system/.env" ]]; then
  printf 'release environment link is invalid\n' >&2
  exit 1
fi

ln -s "$release" "$next_link"
mv -Tf "$next_link" "$current"
switched=1
restart_services
wait_for_services
wait_for_health

cd "$release"
PYTHONPATH=. "$python" scripts/verify_20260811_briefing_detail_policy.py --report-date 2026-08-10 >/tmp/briefing-detail-policy-20260811.json
PYTHONPATH=. "$python" scripts/simulate_full_rollout_readonly.py >/tmp/full-rollout-readonly-20260811.json

switched=0
trap - ERR
printf 'deployed release: %s\n' "$release"
