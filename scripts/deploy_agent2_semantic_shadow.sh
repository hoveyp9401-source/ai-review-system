#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-}"
DEPLOY_DIR="${2:-}"
EXPECTED_REPO="/home/ai_review_tunnel/ai-review-system"
EXPECTED_DEPLOY_ROOT="/home/ai_review_tunnel/deployments/"

if [[ "$(realpath -m "$REPO")" != "$EXPECTED_REPO" ]]; then
  printf 'ERROR: unexpected repository path\n' >&2
  exit 2
fi
case "$(realpath -m "$DEPLOY_DIR")/" in
  "$EXPECTED_DEPLOY_ROOT"*) ;;
  *) printf 'ERROR: deployment directory is outside the protected root\n' >&2; exit 2 ;;
esac

ARCHIVE="$DEPLOY_DIR/deploy-bundle-final-20260714.tar.gz"
MANIFEST="$DEPLOY_DIR/release-manifest-final-20260714.json"
FILE_LIST="$DEPLOY_DIR/deploy-files-final-20260714.txt"
BACKUP="$DEPLOY_DIR/backup"
PYTHON="$REPO/venv/bin/python"
for required in "$ARCHIVE" "$MANIFEST" "$FILE_LIST" "$PYTHON" "$REPO/.env"; do
  [[ -e "$required" ]] || { printf 'ERROR: missing deployment input\n' >&2; exit 2; }
done

umask 077
mkdir -p "$BACKUP"
chmod 700 "$DEPLOY_DIR" "$BACKUP"
chmod 600 "$REPO/.env"

cd "$REPO"
if [[ ! -f "$BACKUP/predeploy-files.tar.gz" ]]; then
  : > "$BACKUP/existing-files.txt"
  : > "$BACKUP/absent-files.txt"
  : > "$BACKUP/predeploy-hashes.tsv"
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    case "$path" in
      /*|*..*) printf 'ERROR: unsafe deploy path\n' >&2; exit 2 ;;
    esac
    if [[ -f "$path" ]]; then
      printf '%s\n' "$path" >> "$BACKUP/existing-files.txt"
      printf '%s\t%s\n' "$path" "$(sha256sum "$path" | cut -d' ' -f1)" \
        >> "$BACKUP/predeploy-hashes.tsv"
    else
      printf '%s\n' "$path" >> "$BACKUP/absent-files.txt"
      printf '%s\tABSENT\n' "$path" >> "$BACKUP/predeploy-hashes.tsv"
    fi
  done < "$FILE_LIST"
  tar -czf "$BACKUP/predeploy-files.tar.gz" -T "$BACKUP/existing-files.txt"
  cp -p "$REPO/.env" "$BACKUP/env.predeploy"
  chmod 600 "$BACKUP/env.predeploy"
  git rev-parse HEAD > "$BACKUP/git-head.txt"
  git status --porcelain=v1 > "$BACKUP/git-status.txt"
else
  printf 'reusing protected pre-overlay backup\n'
fi

tar -xzf "$ARCHIVE" -C "$REPO"

"$PYTHON" - "$REPO" "$MANIFEST" "$DEPLOY_DIR/runtime-hash-after-overlay.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
rows = []
for item in manifest["runtime_files"]:
    path = str(item["path"])
    digest = hashlib.sha256((repo / path).read_bytes()).hexdigest()
    if digest != item["sha256"]:
        raise SystemExit("runtime file hash mismatch")
    rows.append(f"{path}\t{digest}")
bundle = hashlib.sha256(("\n".join(rows) + "\n").encode()).hexdigest()
if bundle != manifest["runtime_bundle_sha256"]:
    raise SystemExit("runtime bundle hash mismatch")
receipt = {
    "status": "PASS",
    "runtime_file_count": len(rows),
    "runtime_bundle_sha256": bundle,
}
Path(sys.argv[3]).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt))
PY

"$PYTHON" "$REPO/scripts/apply_agent2_postgres_migration.py" \
  --migration "$REPO/scripts/create_agent2_case_lifecycle_followup.sql" \
  --env-file "$REPO/.env" \
  --expected-sha256 19b19a8653fdb4ba96bc2e300c886086b90a45982bb7d05b5cc7919718bdf34c \
  --receipt-output "$DEPLOY_DIR/migration-case-public.json"
"$PYTHON" "$REPO/scripts/apply_agent2_postgres_migration.py" \
  --migration "$REPO/scripts/create_agent2_semantic_admission.sql" \
  --env-file "$REPO/.env" \
  --expected-sha256 ac96c379ae93852d54896f60dc14213e9974c7bd760fe01490a9cea0d79c1aaa \
  --receipt-output "$DEPLOY_DIR/migration-semantic-public.json"

"$PYTHON" - "$REPO/.env" "$DEPLOY_DIR/env-update-receipt.json" <<'PY'
import json
import os
import sys
from pathlib import Path
from dotenv import dotenv_values

path = Path(sys.argv[1])
receipt_path = Path(sys.argv[2])
values = {key: str(value or "") for key, value in dotenv_values(path).items()}

def csv(name):
    return tuple(part.strip() for part in values.get(name, "").split(",") if part.strip())

agent_users = csv("AGENT2_CASE_FOLLOWUP_USER_IDS")
lifecycle_users = csv("CASE_FOLLOWUP_USER_ALLOWLIST")
agent_tenants = csv("AGENT2_CASE_FOLLOWUP_TENANT_IDS")
lifecycle_tenants = csv("CASE_FOLLOWUP_TENANT_ALLOWLIST")
if len(agent_users) != 2 or len(lifecycle_users) != 2 or set(agent_users) != set(lifecycle_users):
    raise SystemExit("protected two-user allowlists are missing or inconsistent")
if (
    len(agent_tenants) != 1
    or len(lifecycle_tenants) != 1
    or set(agent_tenants) != set(lifecycle_tenants)
    or agent_tenants[0] != "sandbox-agent2-phase2-20260711"
):
    raise SystemExit("protected Sandbox tenant allowlists are missing or inconsistent")

def is_false(name):
    return values.get(name, "").strip().lower() in {"0", "false", "no", "off"}

for flag in (
    "AGENT2_CASE_FOLLOWUP_SEND_ENABLED",
    "CASE_FOLLOWUP_SEND_ENABLED",
    "CASE_FOLLOWUP_REPORT_PROJECTION_ENABLED",
):
    if not is_false(flag):
        raise SystemExit("protected effect kill switch is not false")

updates = {
    "AGENT2_SEMANTIC_ADMISSION_ENABLED": "true",
    "AGENT2_SEMANTIC_ADMISSION_ENFORCE": "false",
    "AGENT2_SEMANTIC_ADMISSION_REVIEW_CAPTURE": "true",
    "AGENT2_SEMANTIC_ADMISSION_DEFERRED_CAPTURE": "false",
    "AGENT2_SEMANTIC_ADMISSION_SHADOW_REPLAY": "false",
    "AGENT2_SEMANTIC_ADMISSION_TENANT_ALLOWLIST": ",".join(agent_tenants),
    "AGENT2_SEMANTIC_ADMISSION_USER_ALLOWLIST": ",".join(agent_users),
}
lines = path.read_text(encoding="utf-8").splitlines()
written = set()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
    if key in updates:
        if key not in written:
            out.append(f"{key}={updates[key]}")
            written.add(key)
        continue
    out.append(line)
for key, value in updates.items():
    if key not in written:
        out.append(f"{key}={value}")
tmp = path.with_name(path.name + ".semantic-shadow.tmp")
tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
os.chmod(path, 0o600)
receipt = {
    "status": "PASS",
    "mode": "shadow",
    "tenant_count": len(agent_tenants),
    "user_count": len(agent_users),
    "semantic_enabled": True,
    "semantic_enforce": False,
    "review_capture": True,
    "deferred_capture": False,
    "shadow_replay": False,
    "followup_send": False,
    "report_projection": False,
    "env_mode": oct(path.stat().st_mode & 0o777),
}
receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt))
PY

cd "$REPO"
PYTHONPATH=. "$PYTHON" -m compileall -q app scripts
PYTHONPATH=. "$PYTHON" -c \
  'import app.config; import app.agent2.turn_runtime; import app.agent2.operation_outcome_store; import app.api.webhook; import app.stream_runner'

rollback_runtime() {
  tar -xzf "$BACKUP/predeploy-files.tar.gz" -C "$REPO"
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    case "$path" in /*|*..*) exit 2 ;; esac
    rm -f -- "$REPO/$path"
  done < "$BACKUP/absent-files.txt"
  cp -p "$BACKUP/env.predeploy" "$REPO/.env"
  chmod 600 "$REPO/.env"
  bash "$REPO/scripts/restart_production_user_processes.sh"
}

if ! bash "$REPO/scripts/restart_production_user_processes.sh"; then
  printf 'ERROR: restart failed; restoring runtime files and environment\n' >&2
  rollback_runtime
  printf '{"status":"ROLLED_BACK_AFTER_RESTART_FAILURE"}\n' \
    > "$DEPLOY_DIR/deploy-status.json"
  exit 1
fi

health="$(curl -fsS http://127.0.0.1:8000/health)"
[[ "$health" == *'"status":"ok"'* ]] || { printf 'ERROR: health check failed\n' >&2; exit 1; }
stream_count="$(ps -eo args= | grep -F "$REPO/venv/bin/python -m app.stream_runner" | grep -v grep | wc -l)"
scheduler_count="$(ps -eo args= | grep -F "$REPO/venv/bin/python -m app.scheduler.runner" | grep -v grep | wc -l)"
api_count="$(ps -eo args= | grep -F "$REPO/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000" | grep -v grep | wc -l)"
if [[ "$stream_count" != 1 || "$scheduler_count" != 1 || "$api_count" != 1 ]]; then
  printf 'ERROR: production process cardinality mismatch\n' >&2
  exit 1
fi

printf '{"status":"PASS","mode":"SHADOW","api_count":1,"stream_count":1,"scheduler_count":1,"health":"ok"}\n' \
  > "$DEPLOY_DIR/deploy-status.json"
printf 'semantic admission Shadow deployment completed\n'
