"""Fail unless proactive DingTalk sends use the currently active inbound robot."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import text

SCHEMA_VERSION = "agent2.dingtalk.robot_identity_alignment.v1"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-process-id", type=int, required=True)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def evaluate_robot_identity(
    *,
    configured_robot_code: str,
    app_key: str,
    app_secret_present: bool,
    active_robots: tuple[dict[str, object], ...],
) -> dict[str, object]:
    active_code = (
        str(active_robots[0].get("robot_code") or "")
        if len(active_robots) == 1
        else ""
    )
    passed = bool(
        configured_robot_code
        and app_key
        and app_secret_present
        and active_code
        and configured_robot_code == active_code
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if passed else "FAIL",
        "active_inbound_robot_count": len(active_robots),
        "active_inbound_events": (
            int(active_robots[0].get("events") or 0)
            if len(active_robots) == 1
            else 0
        ),
        "active_inbound_users": (
            int(active_robots[0].get("users") or 0)
            if len(active_robots) == 1
            else 0
        ),
        "app_key_present": bool(app_key),
        "app_secret_present": app_secret_present,
        "explicit_outbound_robot_configured": bool(configured_robot_code),
        "outbound_matches_active_robot": bool(
            configured_robot_code and configured_robot_code == active_code
        ),
        "configured_robot_fingerprint": _fingerprint(configured_robot_code),
        "active_robot_fingerprint": _fingerprint(active_code),
    }


def _load_process_environment(pid: int) -> None:
    if pid <= 1:
        raise RuntimeError("source process id is invalid")
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    values = {
        item.split(b"=", 1)[0].decode(): item.split(b"=", 1)[1].decode()
        for item in raw
        if b"=" in item
    }
    if not values:
        raise RuntimeError("source process environment is empty")
    os.environ.clear()
    os.environ.update(values)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.lookback_days < 1 or args.lookback_days > 30:
        raise ValueError("lookback days are invalid")
    _load_process_environment(args.source_process_id)
    from app.config import Settings
    from app.db import AsyncSessionLocal, engine

    settings = Settings(_env_file=None)
    since = datetime.now(UTC) - timedelta(days=args.lookback_days)
    async with AsyncSessionLocal() as session:
        rows = tuple(
            dict(row)
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT payload->>'robotCode' AS robot_code,
                               count(*) AS events,
                               count(DISTINCT dingtalk_user_id) AS users
                        FROM webhook_events
                        WHERE received_at >= :since
                          AND platform = 'dingtalk'
                          AND COALESCE(payload->>'robotCode', '') <> ''
                        GROUP BY payload->>'robotCode'
                        ORDER BY max(received_at) DESC
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
        )
        await session.rollback()
    payload = evaluate_robot_identity(
        configured_robot_code=str(settings.dingtalk_robot_code or "").strip(),
        app_key=str(settings.dingtalk_app_key or "").strip(),
        app_secret_present=bool(str(settings.dingtalk_app_secret or "").strip()),
        active_robots=rows,
    )
    await engine.dispose()
    return payload


async def _main() -> int:
    args = _args()
    payload = await _run(args)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output is not None:
        if args.output.exists() or args.output.is_symlink():
            raise RuntimeError("identity evidence path already exists")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        os.chmod(args.output, 0o600)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
