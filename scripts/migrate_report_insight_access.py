from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import stat
import subprocess
from pathlib import Path

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import User


ENV_PATH = Path("/home/ai_review_tunnel/ai-review-system/.env")
BACKUP_DIR = Path(
    "/home/ai_review_tunnel/codex_backups/"
    "report_insights_20260806_before_access_migration"
)
ID_KEY = "AGENT2_FACT_ALL_ACCESS_DINGTALK_USER_IDS"
NAME_KEY = "AGENT2_FACT_ALL_ACCESS_NAMES"


def service_environment() -> dict[str, str]:
    pid = subprocess.check_output(
        ["systemctl", "show", "ai-review-api.service", "-p", "MainPID", "--value"],
        text=True,
    ).strip()
    if not pid.isdigit():
        raise RuntimeError("API service PID is unavailable")
    result: dict[str, str] = {}
    for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" not in entry:
            continue
        key_bytes, value_bytes = entry.split(b"=", 1)
        key = key_bytes.decode("utf-8", errors="strict")
        if key in {ID_KEY, NAME_KEY}:
            result[key] = value_bytes.decode("utf-8", errors="strict")
    return result


def csv_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


async def resolved_access_ids(names: tuple[str, ...]) -> tuple[str, ...]:
    if not names:
        raise RuntimeError("no configured all-access names to migrate")
    async with AsyncSessionLocal() as session:
        users = (
            await session.execute(
                select(User).where(User.name.in_(names), User.active.is_(True))
            )
        ).scalars().all()
    by_name: dict[str, list[User]] = {name: [] for name in names}
    for user in users:
        by_name.setdefault(str(user.name), []).append(user)
    invalid = {
        name: len(matches)
        for name, matches in by_name.items()
        if len(matches) != 1
        or not str(getattr(matches[0], "dingtalk_user_id", "") or "").strip()
    }
    if invalid:
        raise RuntimeError(f"all-access name resolution is not unique: {invalid}")
    return tuple(
        str(by_name[name][0].dingtalk_user_id).strip()
        for name in names
    )


def updated_env_text(original: str, ids: tuple[str, ...]) -> str:
    lines = original.splitlines()
    replacement = f"{ID_KEY}={','.join(ids)}"
    found = False
    output: list[str] = []
    for line in lines:
        if line.startswith(f"{ID_KEY}="):
            if not found:
                output.append(replacement)
                found = True
            continue
        output.append(line)
    if not found:
        output.append(replacement)
    return "\n".join(output) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "apply"))
    args = parser.parse_args()

    environment = service_environment()
    names = csv_values(environment.get(NAME_KEY, ""))
    existing_ids = csv_values(environment.get(ID_KEY, ""))
    migrated_ids = await resolved_access_ids(names)
    combined_ids = tuple(dict.fromkeys((*existing_ids, *migrated_ids)))
    print(
        f"access-migration-check names={len(names)} "
        f"existing_ids={len(existing_ids)} resolved_ids={len(migrated_ids)}"
    )
    if args.action == "check":
        return

    if not ENV_PATH.is_file() or ENV_PATH.is_symlink():
        raise RuntimeError("production env path is not a regular file")
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup_path = BACKUP_DIR / "production.env"
    shutil.copy2(ENV_PATH, backup_path)
    backup_path.chmod(0o600)

    original = ENV_PATH.read_text(encoding="utf-8")
    updated = updated_env_text(original, combined_ids)
    temp_path = ENV_PATH.with_name(".env.report-insights-next")
    if temp_path.exists() or temp_path.is_symlink():
        raise RuntimeError("temporary env path already exists")
    temp_path.write_text(updated, encoding="utf-8")
    temp_path.chmod(stat.S_IMODE(ENV_PATH.stat().st_mode))
    os.replace(temp_path, ENV_PATH)
    print(f"access-migration-applied backup={backup_path}")


if __name__ == "__main__":
    asyncio.run(main())
