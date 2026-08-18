from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.config import get_settings
from app.llm.client import LLMClient
from scripts.plan_aug17_failed_daily_repairs import (
    _existing_report,
    _read_verified_backup,
    _timeline,
)


def _read_verified_json(path: Path, expected_sha256: str) -> dict[str, Any]:
    encoded = path.read_bytes()
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            f"artifact hash mismatch: expected {expected_sha256}, got {actual}"
        )
    value = json.loads(encoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("artifact is not a JSON object")
    return value


def _system_prompt() -> str:
    return """You are Agent2 reconstructing only the submission-confirmation metadata of 2026-08-17 Daily Reports after production failures. Use semantic understanding, not keywords. Return one JSON object only.

For every anonymous user in the payload, decide:
- preserve: keep the existing confirmation metadata, or use normal automatic submission for a newly reconstructed report.
- mark_user_confirmed: the user explicitly asked to submit or confirm that exact 2026-08-17 report, but the trusted current report is not already user-confirmed.

Rules:
1. A report title, content paste, '已完成' inside a title, a save-only request, complaint, or general acknowledgement is not submission authorization.
2. Submission authority must come from an explicit user request in the ordered timeline and must bind unambiguously to the 2026-08-17 report. Cite exactly one source_message_index for mark_user_confirmed; preserve uses null.
3. If the trusted current report is already user_confirmed, choose preserve even if the user repeated submit.
4. If the user only supplied content and never explicitly asked to submit, choose preserve; a newly reconstructed historical report will follow the system's normal automatic-submission metadata.
5. Do not decide or rewrite report content.

Required shape:
{"users":[{"anonymous_user":"morning-01","decision":"preserve|mark_user_confirmed","source_message_index":null,"reason":"..."}]}
Return every input user exactly once and in the same order.
"""


def _review_prompt() -> str:
    return """You are an independent Agent2 reviewer of Daily Report submission-metadata recovery. Return one JSON object only: {"decision":"approve|reject","reason":"..."}.

Approve only if every user decision follows the ordered user messages and trusted current confirmation metadata. mark_user_confirmed requires one explicit, unambiguous request to submit or confirm the exact 2026-08-17 report and a valid cited source index. Do not treat report content, a title saying completed, saving, complaints, or acknowledgements as submission authorization. Already user-confirmed reports must remain preserve. Identify the exact alias and source index when rejecting.
"""


def _validate(
    raw: dict[str, Any],
    *,
    payload_users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if set(raw) != {"users"} or not isinstance(raw["users"], list):
        raise ValueError("submission plan must contain only users")
    rows = raw["users"]
    aliases = [row["anonymous_user"] for row in payload_users]
    if [row.get("anonymous_user") for row in rows if isinstance(row, dict)] != aliases:
        raise ValueError("submission plan aliases are missing, duplicated, or reordered")
    validated = []
    for row, source in zip(rows, payload_users, strict=True):
        if not isinstance(row, dict) or set(row) != {
            "anonymous_user",
            "decision",
            "source_message_index",
            "reason",
        }:
            raise ValueError("invalid submission-plan row shape")
        decision = row["decision"]
        source_index = row["source_message_index"]
        reason = row["reason"]
        if decision not in {"preserve", "mark_user_confirmed"}:
            raise ValueError("invalid submission decision")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("submission decision requires a reason")
        valid_indexes = {
            item["source_message_index"] for item in source["ordered_user_timeline"]
        }
        if decision == "mark_user_confirmed":
            if (
                not isinstance(source_index, int)
                or isinstance(source_index, bool)
                or source_index not in valid_indexes
            ):
                raise ValueError("mark_user_confirmed needs valid source evidence")
        elif source_index is not None:
            raise ValueError("preserve cannot cite submission evidence")
        validated.append(
            {
                "anonymous_user": row["anonymous_user"],
                "decision": decision,
                "source_message_index": source_index,
                "reason": reason.strip(),
            }
        )
    return validated


def _write(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    return {
        "path": str(path),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--content-plan", type=Path, required=True)
    parser.add_argument("--content-plan-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backup = _read_verified_backup(args.backup, args.backup_sha256)
    content_plan = _read_verified_json(
        args.content_plan,
        args.content_plan_sha256,
    )
    if content_plan.get("backup_sha256") != args.backup_sha256:
        raise RuntimeError("content plan belongs to another backup")
    users = sorted(backup["users"], key=lambda row: row["id"])
    content_by_user = {
        row["user_id"]: row for row in content_plan.get("results", [])
    }
    payload_users = []
    for index, user in enumerate(users, start=1):
        user_id = user["id"]
        report = _existing_report(backup, user_id)
        content_row = content_by_user.get(user_id)
        if content_row is None:
            raise RuntimeError("content plan does not cover every user")
        payload_users.append(
            {
                "anonymous_user": f"morning-{index:02d}",
                "trusted_current_confirmation": (
                    None
                    if report is None
                    else {
                        "status": report["status"],
                        "confirmation_type": report["confirmation_type"],
                        "confirmed_by_user": report["confirmed_by_user"],
                    }
                ),
                "content_repair_decision": content_row["plan"]["decision"],
                "ordered_user_timeline": _timeline(backup, user_id),
            }
        )
    client = LLMClient(get_settings())
    try:
        raw_plan_text = await client.complete_json(
            system_prompt=_system_prompt(),
            user_prompt=json.dumps({"users": payload_users}, ensure_ascii=False),
            model=CANARY_MODEL_NAME,
            thinking_enabled=True,
            timeout_seconds=180,
            max_retries=0,
            max_tokens=10000,
        )
        rows = _validate(json.loads(raw_plan_text), payload_users=payload_users)
        review_payload = {"users": payload_users, "proposed_decisions": rows}
        raw_review_text = await client.complete_json(
            system_prompt=_review_prompt(),
            user_prompt=json.dumps(review_payload, ensure_ascii=False),
            model=CANARY_MODEL_NAME,
            thinking_enabled=True,
            timeout_seconds=180,
            max_retries=0,
            max_tokens=8000,
        )
    finally:
        await client.close()
    review = json.loads(raw_review_text)
    if (
        not isinstance(review, dict)
        or set(review) != {"decision", "reason"}
        or review.get("decision") not in {"approve", "reject"}
        or not isinstance(review.get("reason"), str)
    ):
        raise RuntimeError("invalid independent submission review")
    if review["decision"] != "approve":
        raise RuntimeError(f"submission plan rejected: {review['reason']}")
    value = {
        "schema_version": "agent2.aug18-morning.submission-repair-plan.v1",
        "backup_sha256": args.backup_sha256,
        "content_plan_sha256": args.content_plan_sha256,
        "model": CANARY_MODEL_NAME,
        "thinking_enabled": True,
        "users": rows,
        "review": review,
        "raw_plan_response_sha256": hashlib.sha256(
            raw_plan_text.encode("utf-8")
        ).hexdigest(),
        "raw_review_response_sha256": hashlib.sha256(
            raw_review_text.encode("utf-8")
        ).hexdigest(),
    }
    written = _write(args.output, value)
    print(
        json.dumps(
            {
                "status": "pass",
                "users": len(rows),
                "mark_user_confirmed": sum(
                    row["decision"] == "mark_user_confirmed" for row in rows
                ),
                "preserve": sum(row["decision"] == "preserve" for row in rows),
                "review": review,
                "output": written,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
