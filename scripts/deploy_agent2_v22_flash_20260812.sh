#!/usr/bin/env bash
set -euo pipefail

# Required values are explicit so this release cannot silently deploy a stale
# candidate or overwrite an existing release directory.
: "${CANDIDATE_RELEASE:?set CANDIDATE_RELEASE to the real candidate directory}"
: "${TARGET_RELEASE:?set TARGET_RELEASE to a new real release directory}"
: "${EXPECTED_OLD_RELEASE:?set EXPECTED_OLD_RELEASE to the currently active release}"
: "${RELEASE_KEY:?set RELEASE_KEY to a unique release key}"

releases=/home/ai_review_tunnel/releases
backups=/home/ai_review_tunnel/backups
current="$releases/current"
shared_env=/home/ai_review_tunnel/ai-review-system/.env
python=/home/ai_review_tunnel/ai-review-system/venv/bin/python
health_url=http://127.0.0.1:8000/health
control_backup="$backups/${RELEASE_KEY}-agent2-controls-before.json"
env_backup="$backups/${RELEASE_KEY}-env-before"
next_link="$releases/.current-${RELEASE_KEY}-next"
rollback_link="$releases/.current-${RELEASE_KEY}-rollback"
activation_result="/tmp/${RELEASE_KEY}-control-activate.json"
alignment_result="/tmp/${RELEASE_KEY}-control-alignment.json"
rollout_result="/tmp/${RELEASE_KEY}-full-rollout-readonly.json"
smoke_result="/tmp/${RELEASE_KEY}-date-matrix-rollback.json"
continuous_smoke_result="/tmp/${RELEASE_KEY}-continuous-context-rollback.json"
services=(
  ai-review-api.service
  ai-review-stream.service
  ai-review-scheduler.service
)

services_stopped=0
controls_restore_required=0
current_restore_required=0

require_real_directory() {
  local path="$1"
  if [[ ! -d "$path" || -L "$path" ]]; then
    printf 'expected real directory: %s\n' "$path" >&2
    exit 1
  fi
}

require_release_child() {
  local path="$1"
  local parent base
  parent="$(readlink -f -- "$(dirname -- "$path")")"
  base="$(basename -- "$path")"
  if [[ "$parent" != "$releases" || -z "$base" \
    || "$base" == "." || "$base" == ".." || "$base" == "current" ]]; then
    printf 'unsafe release path: %s\n' "$path" >&2
    exit 1
  fi
}

stop_all_services() {
  local attempt service pid all_stopped
  systemctl stop "${services[@]}"
  for attempt in $(seq 1 30); do
    all_stopped=1
    for service in "${services[@]}"; do
      pid="$(systemctl show "$service" -p MainPID --value)"
      if [[ "$(systemctl is-active "$service" || true)" != "inactive" \
        || ! "$pid" =~ ^[0-9]+$ || "$pid" -ne 0 ]]; then
        all_stopped=0
        break
      fi
    done
    if [[ "$all_stopped" -eq 1 ]]; then
      services_stopped=1
      return 0
    fi
    sleep 1
  done
  printf 'the three services did not all stop\n' >&2
  return 1
}

start_all_services() {
  systemctl start "${services[@]}"
  services_stopped=0
}

wait_for_release() {
  local expected="$1"
  local attempt service pid cwd all_ready
  for attempt in $(seq 1 50); do
    all_ready=1
    for service in "${services[@]}"; do
      if [[ "$(systemctl is-active "$service" || true)" != "active" ]]; then
        all_ready=0
        break
      fi
      pid="$(systemctl show "$service" -p MainPID --value)"
      if [[ ! "$pid" =~ ^[0-9]+$ || "$pid" -le 1 ]]; then
        all_ready=0
        break
      fi
      cwd="$(readlink -f "/proc/$pid/cwd" || true)"
      if [[ "$cwd" != "$expected" ]]; then
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
  for attempt in $(seq 1 50); do
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

switch_current_to() {
  local target="$1"
  local temporary="$2"
  if [[ -e "$temporary" || -L "$temporary" ]]; then
    printf 'temporary current link already exists: %s\n' "$temporary" >&2
    return 1
  fi
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$current"
}

verify_env_unchanged() {
  cmp --silent "$env_backup" "$shared_env"
}

verify_smoke_output() {
  "$python" - "$smoke_result" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["status"] == "pass", payload
assert payload["passed"] == 12, payload
assert payload["failed"] == 0, payload
assert payload["dingtalk_send_calls"] == 0, payload
assert not any(payload["rollback_residue"].values()), payload
for result in payload["results"]:
    assert result["served_models"] == ["deepseek-v4-flash"], result
    assert result["all_model_turns_have_reasoning"] is True, result
    assert result["reasoning_tokens_present"] is True, result
    assert result["second_date_review_count"] == 0, result
print("date matrix verified")
PY
}

verify_continuous_smoke_output() {
  "$python" - "$continuous_smoke_result" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["status"] == "pass", payload
assert len(payload["results"]) == 6, payload
assert not payload["failures"], payload
assert payload["dingtalk_send_calls"] == 0, payload
assert not any(payload["rollback_residue"].values()), payload
for result in payload["results"]:
    assert result["typed_dates"] == ["2026-08-12"], result
    assert result["served_models"] == ["deepseek-v4-flash"], result
    assert result["all_model_turns_have_reasoning"] is True, result
print("continuous context verified")
PY
}

verify_rollout_output() {
  "$python" - "$rollout_result" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["simulation_only"] is True, payload
assert payload["database_writes"] == 0, payload
assert payload["dingtalk_send_calls"] == 0, payload
assert payload["roster_count"] == 74, payload
assert payload["currently_active_users"] == 74, payload
assert payload["currently_enabled_agent2_controls"] == 74, payload
assert payload["currently_message_ready_agent2_controls"] == 74, payload
assert payload["current_agent2_active_user_limit"] == 74, payload
assert payload["required_agent2_active_user_limit_at_real_rollout"] == 74, payload
assert payload["team_count"] == 7, payload
assert payload["center_level_member_count"] == 2, payload
print("full rollout verified")
PY
}

restore_after_failure() {
  local original_status restore_status
  original_status="${1:-1}"
  trap - ERR INT TERM
  set +e
  restore_status=0

  systemctl stop "${services[@]}" || restore_status=1
  services_stopped=1

  if [[ "$controls_restore_required" -eq 1 ]]; then
    (
      cd "$TARGET_RELEASE"
      PYTHONPATH=. "$python" scripts/activate_agent2_v22_flash.py \
        --mode restore \
        --backup-path "$control_backup" \
        --release-key "$RELEASE_KEY" \
        >/tmp/${RELEASE_KEY}-control-restore.json
    ) || restore_status=1
  fi

  if [[ "$current_restore_required" -eq 1 ]]; then
    switch_current_to "$EXPECTED_OLD_RELEASE" "$rollback_link" \
      || restore_status=1
  fi
  cp -p -- "$env_backup" "$shared_env" || restore_status=1
  systemctl start "${services[@]}" || restore_status=1
  services_stopped=0
  wait_for_release "$EXPECTED_OLD_RELEASE" || restore_status=1
  wait_for_health || restore_status=1
  "$python" "$TARGET_RELEASE/scripts/verify_agent2_runtime_control_alignment.py" \
    --code-root "$EXPECTED_OLD_RELEASE" \
    >/tmp/${RELEASE_KEY}-rollback-alignment.json || restore_status=1

  if [[ "$restore_status" -ne 0 ]]; then
    printf 'deployment failed and automatic double-restore needs manual attention\n' >&2
  else
    printf 'deployment failed; old release and all 74 controls were restored\n' >&2
  fi
  exit "$original_status"
}

if [[ "${DEPLOY_LIBRARY_ONLY:-0}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

umask 077
if [[ ! "$RELEASE_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  printf 'unsafe release key: %s\n' "$RELEASE_KEY" >&2
  exit 1
fi
require_release_child "$CANDIDATE_RELEASE"
require_release_child "$TARGET_RELEASE"
require_release_child "$EXPECTED_OLD_RELEASE"
require_real_directory "$CANDIDATE_RELEASE"
require_real_directory "$EXPECTED_OLD_RELEASE"
if [[ "$(readlink -f "$current")" != "$EXPECTED_OLD_RELEASE" ]]; then
  printf 'current release changed before deployment\n' >&2
  exit 1
fi
if [[ -e "$TARGET_RELEASE" || -L "$TARGET_RELEASE" ]]; then
  printf 'target release already exists: %s\n' "$TARGET_RELEASE" >&2
  exit 1
fi
if [[ -e "$control_backup" || -L "$control_backup" \
  || -e "$env_backup" || -L "$env_backup" \
  || -e "$next_link" || -L "$next_link" \
  || -e "$rollback_link" || -L "$rollback_link" ]]; then
  printf 'release backup or temporary path already exists\n' >&2
  exit 1
fi
if [[ "$(readlink -f "$CANDIDATE_RELEASE/.env")" != "$shared_env" ]]; then
  printf 'candidate environment link is invalid\n' >&2
  exit 1
fi

# Build the immutable target and complete all read-only checks before the
# maintenance window starts.
cp -a -- "$CANDIDATE_RELEASE" "$TARGET_RELEASE"
require_real_directory "$TARGET_RELEASE"
if [[ "$(readlink -f "$TARGET_RELEASE/.env")" != "$shared_env" ]]; then
  printf 'target environment link is invalid\n' >&2
  exit 1
fi
cp -p -- "$shared_env" "$env_backup"
(
  cd "$TARGET_RELEASE"
  PYTHONPATH=. "$python" -c \
    "from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME, CANARY_THINKING_ENABLED; assert CANARY_MODEL_NAME == 'deepseek-v4-flash'; assert CANARY_THINKING_ENABLED is True"
  PYTHONPATH=. "$python" scripts/activate_agent2_v22_flash.py \
    --mode preflight \
    --backup-path "$control_backup" \
    --release-key "$RELEASE_KEY" \
    >/tmp/${RELEASE_KEY}-control-preflight.json
)
verify_env_unchanged

# No old process can observe the new controls, and no new process can observe
# the old controls: the entire mismatch window is contained while all three
# services are stopped.
trap 'restore_after_failure "$?"' ERR
trap 'restore_after_failure 130' INT
trap 'restore_after_failure 143' TERM
stop_all_services
# Set this before activation starts.  The activation transaction can commit
# all 74 controls and then fail during its verification/output phase; rollback
# must still restore the backup in that case.
controls_restore_required=1
(
  cd "$TARGET_RELEASE"
  PYTHONPATH=. "$python" scripts/activate_agent2_v22_flash.py \
    --mode activate \
    --backup-path "$control_backup" \
    --release-key "$RELEASE_KEY" \
    >"$activation_result"
)
"$python" - "$activation_result" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["target_verified"] is True, payload
assert payload["roster_count"] == 74, payload
assert payload["control_count"] == 74, payload
assert payload["changed_count"] == 74, payload
assert payload["audit_count"] == 74, payload
assert payload["candidate"]["model_name"] == "deepseek-v4-flash", payload
assert payload["candidate"]["thinking_enabled"] is True, payload
PY
# Mark the old link for restoration before the atomic switch.  A signal can
# arrive after mv changes current but before the next shell statement; the
# rollback must still put the old code back.
current_restore_required=1
switch_current_to "$TARGET_RELEASE" "$next_link"
verify_env_unchanged
start_all_services
wait_for_release "$TARGET_RELEASE"
wait_for_health

# Verify imports through the exact current path, the 74 control contract, the
# formal roster/briefing topology, and then real Flash Agent2 behavior without
# sending DingTalk messages or retaining smoke data.
"$python" "$TARGET_RELEASE/scripts/verify_agent2_runtime_control_alignment.py" \
  --code-root "$(readlink -f "$current")" \
  --expected-model deepseek-v4-flash \
  >"$alignment_result"
(
  cd "$current"
  PYTHONPATH=. "$python" scripts/activate_agent2_v22_flash.py \
    --mode check \
    --backup-path "$control_backup" \
    --release-key "$RELEASE_KEY" \
    >/tmp/${RELEASE_KEY}-control-postcheck.json
  PYTHONPATH=. "$python" scripts/simulate_full_rollout_readonly.py \
    >"$rollout_result"
  PYTHONPATH=. "$python" scripts/smoke_20260812_final_date_matrix_rollback.py \
    >"$smoke_result"
  PYTHONPATH=. "$python" scripts/smoke_20260812_continuous_real_context_rollback.py \
    >"$continuous_smoke_result"
)
verify_smoke_output
verify_continuous_smoke_output
verify_rollout_output
verify_env_unchanged

# All release checks have passed while both restore guards are still armed.
# Disarm the signal/error handlers first so an interruption can never observe
# only one of the two guards cleared and create a code/control mismatch.
trap - ERR INT TERM
controls_restore_required=0
current_restore_required=0
services_stopped=0
printf 'deployed release: %s\n' "$TARGET_RELEASE"
printf 'control backup: %s\n' "$control_backup"
printf 'environment backup: %s\n' "$env_backup"
