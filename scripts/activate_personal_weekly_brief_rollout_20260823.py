from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


ENABLED_KEY = "AGENT2_PERSONAL_WEEKLY_BRIEF_ENABLED"
SEND_KEY = "AGENT2_PERSONAL_WEEKLY_BRIEF_SEND_ENABLED"
TENANT_KEY = "AGENT2_PERSONAL_WEEKLY_BRIEF_TENANT_ID"
RUNTIME_TENANT_KEY = "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "verify", "rollback"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--backup-path", type=Path, required=True)
    return parser.parse_args()


def _values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in values:
            raise RuntimeError(f"duplicate environment key: {key}")
        values[key] = value.strip().strip('"').strip("'")
    return values


def _one_runtime_tenant(values: dict[str, str]) -> str:
    tenants = tuple(
        value.strip()
        for value in values.get(RUNTIME_TENANT_KEY, "").split(",")
        if value.strip()
    )
    if (
        len(tenants) != 1
        or not tenants[0].isascii()
        or any(character.isspace() for character in tenants[0])
    ):
        raise RuntimeError("Agent2 runtime tenant is not unique")
    return tenants[0]


def _is_false(value: str) -> bool:
    return value.strip().lower() in {"", "0", "false", "no", "off"}


def _render(text: str, replacements: dict[str, str]) -> str:
    remaining = dict(replacements)
    rendered: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in remaining:
            rendered.append(f"{key}={remaining.pop(key)}")
        else:
            rendered.append(raw_line)
    if remaining:
        if rendered and rendered[-1]:
            rendered.append("")
        rendered.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(rendered) + "\n"


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.personal-weekly-brief.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError("personal weekly brief environment temporary path exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _summary(action: str, content: bytes) -> dict[str, object]:
    values = _values(content.decode("utf-8"))
    runtime_tenant = _one_runtime_tenant(values)
    return {
        "status": "PASS",
        "action": action,
        "generation_enabled": values.get(ENABLED_KEY, "").lower() == "true",
        "send_enabled": values.get(SEND_KEY, "").lower() == "true",
        "tenant_aligned": values.get(TENANT_KEY, "") == runtime_tenant,
        "tenant_sha256": hashlib.sha256(runtime_tenant.encode()).hexdigest(),
    }


def main() -> None:
    args = _args()
    if not args.env_file.is_file() or args.env_file.is_symlink():
        raise RuntimeError("shared environment file is missing or unsafe")
    source_mode = args.env_file.stat().st_mode & 0o777
    current = args.env_file.read_bytes()
    values = _values(current.decode("utf-8"))
    runtime_tenant = _one_runtime_tenant(values)

    if args.action == "apply":
        if args.backup_path.exists() or args.backup_path.is_symlink():
            raise RuntimeError("personal weekly brief environment backup exists")
        if not (
            _is_false(values.get(ENABLED_KEY, ""))
            and _is_false(values.get(SEND_KEY, ""))
            and values.get(TENANT_KEY, "") in {"", runtime_tenant}
        ):
            raise RuntimeError("personal weekly brief environment is not closed")
        descriptor = os.open(
            args.backup_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(current)
            handle.flush()
            os.fsync(handle.fileno())
        updated = _render(
            current.decode("utf-8"),
            {
                ENABLED_KEY: "true",
                SEND_KEY: "true",
                TENANT_KEY: runtime_tenant,
            },
        ).encode("utf-8")
        _atomic_write(args.env_file, updated, mode=source_mode)
        payload = _summary("apply", updated)
        payload["backup_sha256"] = hashlib.sha256(current).hexdigest()
    elif args.action == "rollback":
        if not args.backup_path.is_file() or args.backup_path.is_symlink():
            raise RuntimeError("personal weekly brief environment backup is missing")
        backup = args.backup_path.read_bytes()
        _values(backup.decode("utf-8"))
        _atomic_write(args.env_file, backup, mode=source_mode)
        payload = _summary("rollback", backup)
    else:
        payload = _summary("verify", current)
        if not (
            payload["generation_enabled"] is True
            and payload["send_enabled"] is True
            and payload["tenant_aligned"] is True
        ):
            payload["status"] = "FAIL"
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
