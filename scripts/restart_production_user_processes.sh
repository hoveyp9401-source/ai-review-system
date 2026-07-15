#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/home/ai_review_tunnel/ai-review-system}"
PORT="${PORT:-8000}"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
DRY_RUN=0

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

PY="$ROOT/venv/bin/python"
API_NEEDLE="$PY -m uvicorn app.main:app --host 0.0.0.0 --port $PORT"
STREAM_NEEDLE="$PY -m app.stream_runner"
SCHEDULER_NEEDLE="$PY -m app.scheduler.runner"

list_pids() {
  local needle="$1"
  ps -eo pid=,args= | while read -r pid args; do
    if [[ "$args" == *"$needle"* ]]; then
      printf '%s\n' "$pid"
    fi
  done
}

show_matches() {
  local label="$1"
  local needle="$2"
  local pids
  pids="$(list_pids "$needle" | tr '\n' ' ')"
  printf '%s: %s\n' "$label" "${pids:-none}"
}

kill_matches() {
  local label="$1"
  local needle="$2"
  mapfile -t pids < <(list_pids "$needle")
  if (( ${#pids[@]} == 0 )); then
    printf '%s: no running process\n' "$label"
    return 0
  fi
  printf '%s: stopping %s\n' "$label" "${pids[*]}"
  if (( DRY_RUN == 1 )); then
    return 0
  fi
  kill "${pids[@]}" 2>/dev/null || true
  for _ in {1..20}; do
    mapfile -t remaining < <(list_pids "$needle")
    if (( ${#remaining[@]} == 0 )); then
      return 0
    fi
    sleep 0.5
  done
  mapfile -t remaining < <(list_pids "$needle")
  if (( ${#remaining[@]} > 0 )); then
    printf '%s: force stopping %s\n' "$label" "${remaining[*]}"
    kill -9 "${remaining[@]}" 2>/dev/null || true
  fi
}

start_one() {
  local label="$1"
  local logfile="$2"
  shift 2
  printf '%s: starting %s\n' "$label" "$*"
  if (( DRY_RUN == 1 )); then
    return 0
  fi
  nohup env PYTHONPATH="$ROOT" "$@" > "$LOG_DIR/$logfile" 2>&1 < /dev/null &
}

require_single() {
  local label="$1"
  local needle="$2"
  mapfile -t pids < <(list_pids "$needle")
  if (( ${#pids[@]} != 1 )); then
    printf 'ERROR: expected exactly one %s process, got %s: %s\n' "$label" "${#pids[@]}" "${pids[*]:-none}" >&2
    return 1
  fi
  printf '%s: running pid %s\n' "$label" "${pids[0]}"
}

systemd_units_loaded() {
  local unit
  for unit in ai-review-api.service ai-review-stream.service ai-review-scheduler.service; do
    if [[ "$(systemctl show -p LoadState --value "$unit" 2>/dev/null || true)" != "loaded" ]]; then
      return 1
    fi
  done
}

restart_systemd_supervised_processes() {
  local units=(ai-review-scheduler.service ai-review-stream.service ai-review-api.service)
  local unit pid new_pid ready restarted=0
  declare -A old_pids=()
  printf 'systemd supervision detected; restarting service main processes without creating parallel nohup workers\n'
  for unit in "${units[@]}"; do
    pid="$(systemctl show -p MainPID --value "$unit")"
    if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
      printf 'ERROR: %s has no active MainPID\n' "$unit" >&2
      return 1
    fi
    old_pids["$unit"]="$pid"
    printf '%s: stopping supervised pid %s\n' "$unit" "$pid"
    if (( DRY_RUN == 0 )); then
      kill "$pid"
    fi
  done
  if (( DRY_RUN == 1 )); then
    printf 'dry-run complete; no process was changed\n'
    return 0
  fi
  for _ in {1..30}; do
    sleep 1
    ready=1
    for unit in "${units[@]}"; do
      new_pid="$(systemctl show -p MainPID --value "$unit")"
      if [[ ! "$new_pid" =~ ^[1-9][0-9]*$ ]] \
        || [[ "$new_pid" == "${old_pids[$unit]}" ]] \
        || ! kill -0 "$new_pid" 2>/dev/null; then
        ready=0
        break
      fi
    done
    if (( ready == 1 )) && systemctl is-active --quiet "${units[@]}"; then
      restarted=1
      break
    fi
  done
  if (( restarted == 0 )); then
    printf 'ERROR: one or more systemd services failed to recover\n' >&2
    systemctl --no-pager --full status "${units[@]}" || true
    return 1
  fi
  require_single "api" "$API_NEEDLE"
  require_single "stream" "$STREAM_NEEDLE"
  require_single "scheduler" "$SCHEDULER_NEEDLE"
  for _ in {1..30}; do
    if curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null; then
      printf '\n'
      break
    fi
    sleep 1
  done
  if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; then
    printf 'ERROR: API process restarted but health endpoint did not become ready\n' >&2
    return 1
  fi
  printf '\nsystemd-supervised restart complete\n'
}

main() {
  if [[ ! -d "$ROOT" ]]; then
    printf 'ERROR: ROOT does not exist: %s\n' "$ROOT" >&2
    return 1
  fi
  if [[ ! -x "$PY" ]]; then
    printf 'ERROR: python runtime does not exist or is not executable: %s\n' "$PY" >&2
    return 1
  fi
  cd "$ROOT"
  mkdir -p "$LOG_DIR"

  if systemd_units_loaded; then
    restart_systemd_supervised_processes
    return
  fi

  printf 'ROOT=%s\nPORT=%s\nDRY_RUN=%s\n' "$ROOT" "$PORT" "$DRY_RUN"
  show_matches "api-before" "$API_NEEDLE"
  show_matches "stream-before" "$STREAM_NEEDLE"
  show_matches "scheduler-before" "$SCHEDULER_NEEDLE"

  kill_matches "api" "$API_NEEDLE"
  kill_matches "stream" "$STREAM_NEEDLE"
  kill_matches "scheduler" "$SCHEDULER_NEEDLE"

  if (( DRY_RUN == 1 )); then
    printf 'dry-run complete; no process was changed\n'
    return 0
  fi

  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a

  start_one "scheduler" "scheduler.manual.log" "$PY" -m app.scheduler.runner
  start_one "stream" "stream.manual.log" "$PY" -m app.stream_runner
  start_one "api" "api.manual.log" "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"

  sleep 3
  require_single "api" "$API_NEEDLE"
  require_single "stream" "$STREAM_NEEDLE"
  require_single "scheduler" "$SCHEDULER_NEEDLE"
  curl -fsS "http://127.0.0.1:$PORT/health"
  printf '\nrestart complete\n'
}

main "$@"
