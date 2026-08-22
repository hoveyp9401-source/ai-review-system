#!/usr/bin/env bash
set -euo pipefail

root=/home/ai_review_tunnel
current="$root/releases/current"
python="$root/ai-review-system/venv/bin/python"
migration="$root/manage_formal_roster_70_4_20260822.py.tmp"
backup="$root/deploy_backups/formal-roster-70-4-20260822-v3.json"
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

frozen_pids=()
roster_applied=0

run_roster() {
  local action="$1"
  shift
  (
    set -a
    # shellcheck disable=SC1091
    . "$root/ai-review-system/.env"
    set +a
    cd "$current"
    PYTHONPATH=. "$python" "$migration" "$action" "$@"
  )
}

resume_services() {
  local pid
  for pid in "${frozen_pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
  done
  frozen_pids=()
}

cleanup() {
  local original_status="$1"
  local cleanup_failed=0
  trap - ERR INT TERM EXIT
  set +e
  if [[ "$roster_applied" -eq 1 ]]; then
    run_roster restore --backup-path "$backup" >/dev/null || cleanup_failed=1
  fi
  resume_services
  rm -f -- "$migration"
  if [[ "$cleanup_failed" -ne 0 ]]; then
    echo "roster restore smoke cleanup requires manual attention" >&2
    exit 1
  fi
  exit "$original_status"
}

trap 'cleanup "$?"' ERR
trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup "$?"' EXIT

[[ -f "$migration" && ! -L "$migration" ]]
[[ -f "$backup" && ! -L "$backup" ]]
[[ "$(stat -c %a "$backup")" == "600" ]]

run_roster verify-before >/dev/null

for service in "${services[@]}"; do
  pid="$(systemctl show "$service" -p MainPID --value)"
  [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]]
  [[ "$(stat -c %U "/proc/$pid")" == "ai_review_tunnel" ]]
  kill -STOP "$pid"
  frozen_pids+=("$pid")
done

sleep 0.2
for pid in "${frozen_pids[@]}"; do
  [[ "$(awk '/^State:/{print $2}' "/proc/$pid/status")" == "T" ]]
done

run_roster apply --backup-path "$backup" >/dev/null
roster_applied=1
run_roster verify-after >/dev/null
run_roster restore --backup-path "$backup" >/dev/null
roster_applied=0
run_roster verify-before >/dev/null

resume_services

for service in "${services[@]}"; do
  [[ "$(systemctl is-active "$service")" == "active" ]]
done
curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null

rm -f -- "$migration"
trap - ERR INT TERM EXIT
printf '%s\n' '{"action":"formal_roster_restore_smoke","apply":"pass","restore":"pass","final_state":"72+2","services":"active","health":"ok","real_message_sent":false}'
