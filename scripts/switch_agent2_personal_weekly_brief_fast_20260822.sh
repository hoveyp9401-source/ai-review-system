#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
releases=/home/ai_review_tunnel/releases
candidate="$releases/ai-review-system-agent2-weekly-brief-fast-20260822-v2"
previous="$releases/ai-review-system-agent2-daily-weekly-brief-20260822-v1"
current="$releases/current"
shared_env=/home/ai_review_tunnel/ai-review-system/.env
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
expected_commit=9e255b25713ee34cc68d0892dc35550864981707
daily_focus_output=/home/ai_review_tunnel/deploy_backups/daily-focus-weekly-brief-fast-v2-20260822.json
alignment_output=/home/ai_review_tunnel/deploy_backups/alignment-weekly-brief-fast-v2-20260822.json
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

verify_candidate() {
  [[ -d "$candidate" && ! -L "$candidate" ]]
  [[ -d "$previous" && ! -L "$previous" ]]
  [[ -f "$candidate/RELEASE_COMMIT" && ! -L "$candidate/RELEASE_COMMIT" ]]
  [[ "$(tr -d '\r\n' <"$candidate/RELEASE_COMMIT")" == "$expected_commit" ]]
  if [[ -n "$(find "$candidate" -perm /222 -print -quit)" ]]; then
    echo "candidate release contains writable paths" >&2
    return 1
  fi
}

verify_weekly_brief_off_and_empty() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    PYTHONPATH=. "$python" - <<'PY'
import asyncio
from datetime import date

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.legal_daily_roster import load_formal_legal_daily_roster


async def main() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    if settings.agent2_personal_weekly_brief_enabled:
        raise RuntimeError("personal weekly brief generation must remain disabled")
    if settings.agent2_personal_weekly_brief_send_enabled:
        raise RuntimeError("personal weekly brief sending must remain disabled")
    tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    async with AsyncSessionLocal() as session:
        exists = bool(
            await session.scalar(
                text(
                    "SELECT to_regclass('public.agent2_personal_weekly_briefs') "
                    "IS NOT NULL"
                )
            )
        )
        if not exists:
            raise RuntimeError("personal weekly brief table is missing")
        row_count = int(
            await session.scalar(
                text("SELECT count(*) FROM public.agent2_personal_weekly_briefs")
            )
            or 0
        )
        if row_count != 0:
            raise RuntimeError("personal weekly brief table is not empty")
        historical = await load_formal_legal_daily_roster(
            session,
            tenant_id=tenant_id,
            on_date=date(2026, 8, 21),
        )
        current = await load_formal_legal_daily_roster(
            session,
            tenant_id=tenant_id,
            on_date=date(2026, 8, 22),
        )
        if (len(historical.child_members), len(historical.center_members)) != (72, 2):
            raise RuntimeError("historical roster is not 72+2")
        if (len(current.child_members), len(current.center_members)) != (70, 4):
            raise RuntimeError("current roster is not 70+4")
        await session.rollback()
    await engine.dispose()


asyncio.run(main())
PY
  )
}

run_control_alignment() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    umask 077
    PYTHONPATH=. "$python" scripts/verify_agent2_runtime_control_alignment.py \
      --code-root "$candidate" \
      --expected-model deepseek-v4-flash \
      >"$alignment_output"
  )
}

run_daily_focus_smoke() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$shared_env"
    set +a
    cd "$candidate"
    umask 077
    unset SMOKE_CASE_NAMES
    PYTHONPATH=. "$python" \
      scripts/smoke_20260822_daily_focus_weekly_submit_rollback.py \
      >"$daily_focus_output"
    "$python" - "$daily_focus_output" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "bare",
    "natural",
    "followup",
    "daily_plan_followup",
    "explicit_weekly_switch",
}
actual = {str(item.get("name") or "") for item in payload.get("results", [])}
if payload.get("status") != "pass" or payload.get("passed") != 5:
    raise SystemExit("Daily focus smoke did not pass all five cases")
if payload.get("failed") != 0 or payload.get("failures"):
    raise SystemExit("Daily focus smoke contains a failed case")
if actual != expected:
    raise SystemExit("Daily focus smoke case set is incomplete")
if payload.get("dingtalk_send_calls") != 0 or payload.get("transport_enabled_cases") != 0:
    raise SystemExit("Daily focus smoke transport was not fully disabled")
if any(int(value) != 0 for value in payload.get("rollback_residue", {}).values()):
    raise SystemExit("Daily focus smoke left rollback residue")
if any(int(value) != 0 for value in payload.get("production_state_changes", {}).values()):
    raise SystemExit("Daily focus smoke changed production state")
PY
  )
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

frozen_pids=()
processes_frozen=0

resume_frozen() {
  local pid
  for pid in "${frozen_pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
  done
  frozen_pids=()
  processes_frozen=0
}

freeze_all_services() {
  local service pid state
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
      resume_frozen
      return 1
    fi
    frozen_pids+=("$pid")
  done
  sleep 0.2
  for pid in "${frozen_pids[@]}"; do
    state="$(awk '/^State:/{print $2}' "/proc/$pid/status" 2>/dev/null || true)"
    if [[ "$state" != "T" ]]; then
      echo "service process is not frozen: $pid:$state" >&2
      resume_frozen
      return 1
    fi
  done
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
    freeze_all_services || exit 1
  fi
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    switch_current "$previous" weekly-brief-fast-v2-rollback || exit 1
  fi
  terminate_frozen_services
  wait_healthy "$previous" || exit 1
  exit "$status"
}

if [[ ! -f "$shared_env" || -L "$shared_env" ]]; then
  echo "shared environment is missing or unsafe" >&2
  exit 1
fi
verify_candidate

if [[ "$action" == "deploy" ]]; then
  if [[ "$(readlink -f "$current")" != "$previous" ]]; then
    echo "current release changed before deploy" >&2
    exit 1
  fi
  if [[ -e "$daily_focus_output" || -L "$daily_focus_output" \
    || -e "$alignment_output" || -L "$alignment_output" ]]; then
    echo "deployment evidence path already exists" >&2
    exit 1
  fi
  verify_weekly_brief_off_and_empty
  trap 'rollback "$?"' ERR
  trap 'rollback 130' INT
  trap 'rollback 143' TERM
  freeze_all_services
  switch_current "$candidate" weekly-brief-fast-v2-next
  terminate_frozen_services
  wait_healthy "$candidate"
  verify_weekly_brief_off_and_empty
  run_control_alignment
  [[ "$(stat -c %a "$alignment_output")" == "600" ]]
  run_daily_focus_smoke
  [[ "$(stat -c %a "$daily_focus_output")" == "600" ]]
  wait_healthy "$candidate"
  verify_weekly_brief_off_and_empty
  trap - ERR INT TERM
  echo "deployed $candidate"
elif [[ "$action" == "rollback" ]]; then
  if [[ "$(readlink -f "$current")" != "$candidate" ]]; then
    echo "current release is not weekly brief fast v2" >&2
    exit 1
  fi
  freeze_all_services
  rollback 0
else
  echo "usage: $0 deploy|rollback" >&2
  exit 2
fi
