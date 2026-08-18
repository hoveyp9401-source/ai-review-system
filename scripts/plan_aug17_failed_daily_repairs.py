from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.config import get_settings
from app.llm.client import LLMClient


REPORT_FIELDS = ("today_work", "problems", "tomorrow_plan")
REPORT_DATE = "2026-08-17"
MAX_CONCURRENCY = 3


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_verified_backup(path: Path, expected_sha256: str) -> dict[str, Any]:
    encoded = path.read_bytes()
    actual = _sha256_bytes(encoded)
    if actual != expected_sha256:
        raise RuntimeError(
            f"backup hash mismatch: expected {expected_sha256}, got {actual}"
        )
    value = json.loads(encoded.decode("utf-8"))
    if value.get("schema_version") not in {
        "agent2.aug17.daily-repair-backup.v1",
        "agent2.aug18-morning.daily-repair-backup.v1",
    }:
        raise RuntimeError("unsupported backup schema")
    return value


def _existing_report(backup: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    matches = [
        row
        for row in backup["reports"]
        if row["user_id"] == user_id and row["report_date"] == REPORT_DATE
    ]
    if len(matches) > 1:
        raise RuntimeError("duplicate Aug17 report in backup")
    return matches[0] if matches else None


def _timeline(backup: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
    rows = [
        row for row in backup["timeline_events"] if row["user_id"] == user_id
    ]
    rows.sort(key=lambda row: (row["received_at"], row["id"]))
    return [
        {
            "source_message_index": index,
            "received_at": row["received_at"],
            "business_result": (
                row.get("response_payload", {})
                .get("_agent2_turn_observation_v1", {})
                .get("business_result_status", "unknown")
            ),
            "text": row["text"],
        }
        for index, row in enumerate(rows, start=1)
        if row["text"]
    ]


def _existing_prompt(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "status": report["status"],
        "confirmation_type": report["confirmation_type"],
        "confirmed_by_user": report["confirmed_by_user"],
        "fields": {
            field: [
                {"existing_item_index": index, "content": content}
                for index, content in enumerate(report[field], start=1)
            ]
            for field in REPORT_FIELDS
        },
        "acknowledged_empty_fields": sorted(
            field
            for field in REPORT_FIELDS
            if bool(
                (report.get("section_status") or {}).get(
                    f"{field}_acknowledged_empty"
                )
            )
        ),
    }


def _planner_system_prompt() -> str:
    return """You are Agent2 reconstructing one employee's 2026-08-17 Daily Report after a production failure. Use semantic understanding, not keywords. Return one JSON object only.

The payload contains a trusted current report snapshot and the ordered user-message timeline. Produce the complete final report the system should hold after honoring the user's messages. The user's exact words are the highest authority.

Rules:
1. Output decision as change or no_change, all three fields today_work/problems/tomorrow_plan, acknowledged_empty_fields, and a concise reason.
2. Each final item must be exactly one evidence object. Preserve a trusted current item with {"existing_item_index":N}; add or replace from the timeline with {"source_message_index":N,"exact_quote":"one contiguous verbatim user passage"}. Do not output a separate rewritten content value.
3. Every exact_quote must occur verbatim in that one source message and preserve actors, attribution, negation, conditions, numbers, deadlines, and pending status. Do not invent, normalize, or silently shorten meaning.
4. Preserve trusted current content unless a later user message clearly corrects, replaces, removes, or supersedes it. A later clearly complete structured report may supersede an earlier dictation or earlier full draft. Do not blindly append repeated retries of the same report.
5. A submit/save/complaint message contains no report item by itself. It may confirm adoption of the immediately preceding complete user-authored report, but never supplies missing work content.
6. Respect user grouping. Numbered or bulleted entries are separate items unless they contain explicit subitems. Natural clauses may be combined only when they are one independently editable matter. Do not omit a later risk or plan section after a work list.
7. Put a field in acknowledged_empty_fields only when a user message explicitly says that field is empty, or when preserving the trusted report's existing explicit empty acknowledgement. A field cannot both contain items and be acknowledged empty.
8. no_change means the materialized fields and empty acknowledgements exactly match the trusted current report. If no current report exists, decision must be change.
9. Do not decide submission metadata or report status. This task reconstructs content only.

Required shape:
{"decision":"change|no_change","fields":{"today_work":[evidence...],"problems":[evidence...],"tomorrow_plan":[evidence...]},"acknowledged_empty_fields":["problems"],"reason":"..."}
"""


def _reviewer_system_prompt() -> str:
    return """You are an independent Agent2 reviewer for a historical Daily Report repair. Return one JSON object only: {"decision":"approve|reject","reason":"..."}.

Compare the entire ordered user timeline, trusted current report, and proposed final materialized report. Approve only if the proposal preserves the latest user intent completely and exactly: no missing matter, invention, wrong field, arbitrary split/merge, duplicate retry, stale version retained after a clear correction, or unintended loss of trusted existing content. Submission-only and complaint messages are not report content. Verify every proposed source quote is verbatim in its cited message and that every preserved existing item is correctly cited. Empty fields require explicit evidence or a preserved trusted acknowledgement. If rejecting, identify the exact source index, field, and defect concisely. Do not rewrite the plan.
"""


def _parse_json_object(raw: str) -> dict[str, Any]:
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("model output is not an object")
    return decoded


def _validate_and_materialize(
    raw_plan: dict[str, Any],
    *,
    report: dict[str, Any] | None,
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    if set(raw_plan) != {
        "decision",
        "fields",
        "acknowledged_empty_fields",
        "reason",
    }:
        raise ValueError("plan has unexpected or missing top-level fields")
    decision = raw_plan["decision"]
    if decision not in {"change", "no_change"}:
        raise ValueError("invalid repair decision")
    fields = raw_plan["fields"]
    if not isinstance(fields, dict) or set(fields) != set(REPORT_FIELDS):
        raise ValueError("plan must contain exactly three report fields")
    acknowledged = raw_plan["acknowledged_empty_fields"]
    if (
        not isinstance(acknowledged, list)
        or any(field not in REPORT_FIELDS for field in acknowledged)
        or len(acknowledged) != len(set(acknowledged))
    ):
        raise ValueError("invalid acknowledged_empty_fields")
    if not isinstance(raw_plan["reason"], str) or not raw_plan["reason"].strip():
        raise ValueError("repair plan needs a concise reason")
    source_by_index = {
        row["source_message_index"]: row["text"] for row in timeline
    }
    materialized: dict[str, list[str]] = {field: [] for field in REPORT_FIELDS}
    normalized_evidence: dict[str, list[dict[str, Any]]] = {
        field: [] for field in REPORT_FIELDS
    }
    for field in REPORT_FIELDS:
        entries = fields[field]
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError(f"invalid {field} evidence list")
        existing_values = list((report or {}).get(field) or ())
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{field} evidence must be an object")
            if set(entry) == {"existing_item_index"}:
                index = entry["existing_item_index"]
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 1
                    or index > len(existing_values)
                ):
                    raise ValueError(f"{field} existing index is invalid")
                content = str(existing_values[index - 1])
                normalized_evidence[field].append(
                    {"existing_item_index": index}
                )
            elif set(entry) == {"source_message_index", "exact_quote"}:
                index = entry["source_message_index"]
                quote = entry["exact_quote"]
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index not in source_by_index
                    or not isinstance(quote, str)
                    or not quote.strip()
                    or quote not in source_by_index[index]
                ):
                    raise ValueError(f"{field} source evidence is invalid")
                content = quote.strip()
                normalized_evidence[field].append(
                    {
                        "source_message_index": index,
                        "exact_quote": quote,
                    }
                )
            else:
                raise ValueError(f"{field} evidence has an invalid shape")
            if content in materialized[field]:
                raise ValueError(f"{field} contains a duplicate item")
            materialized[field].append(content)
        if field in acknowledged and materialized[field]:
            raise ValueError(f"{field} cannot be filled and acknowledged empty")
    existing_fields = {
        field: list((report or {}).get(field) or ()) for field in REPORT_FIELDS
    }
    existing_acknowledged = sorted(
        field
        for field in REPORT_FIELDS
        if bool(
            ((report or {}).get("section_status") or {}).get(
                f"{field}_acknowledged_empty"
            )
        )
    )
    actual_change = materialized != existing_fields or sorted(acknowledged) != (
        existing_acknowledged
    )
    if decision == "no_change" and (report is None or actual_change):
        raise ValueError("no_change does not match materialized report")
    if decision == "change" and not actual_change:
        raise ValueError("change plan does not change the report")
    return {
        "decision": decision,
        "fields": normalized_evidence,
        "acknowledged_empty_fields": sorted(acknowledged),
        "reason": raw_plan["reason"].strip(),
        "materialized_fields": materialized,
        "changes_content": actual_change,
    }


def _validate_review(raw_review: dict[str, Any]) -> dict[str, str]:
    if set(raw_review) != {"decision", "reason"}:
        raise ValueError("review has unexpected or missing fields")
    decision = raw_review["decision"]
    reason = raw_review["reason"]
    if decision not in {"approve", "reject"}:
        raise ValueError("invalid review decision")
    if not isinstance(reason, str):
        raise ValueError("review reason must be text")
    if decision == "reject" and not reason.strip():
        raise ValueError("rejected plan needs a reason")
    return {"decision": decision, "reason": reason.strip()}


async def _complete_plan(
    client: LLMClient,
    payload: dict[str, Any],
    *,
    feedback: str | None,
) -> tuple[dict[str, Any], str]:
    user_payload = dict(payload)
    if feedback:
        user_payload["independent_review_feedback"] = feedback
        user_payload["repair_instruction"] = (
            "Correct only the identified defect, then re-audit the entire timeline."
        )
    raw = await client.complete_json(
        system_prompt=_planner_system_prompt(),
        user_prompt=json.dumps(user_payload, ensure_ascii=False),
        model=CANARY_MODEL_NAME,
        thinking_enabled=True,
        timeout_seconds=180,
        max_retries=0,
        max_tokens=12000,
    )
    return _parse_json_object(raw), raw


async def _complete_review(
    client: LLMClient,
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, str], str]:
    review_payload = {
        **payload,
        "proposed_plan": {
            key: plan[key]
            for key in (
                "decision",
                "fields",
                "acknowledged_empty_fields",
                "reason",
                "materialized_fields",
            )
        },
    }
    raw = await client.complete_json(
        system_prompt=_reviewer_system_prompt(),
        user_prompt=json.dumps(review_payload, ensure_ascii=False),
        model=CANARY_MODEL_NAME,
        thinking_enabled=True,
        timeout_seconds=180,
        max_retries=0,
        max_tokens=8000,
    )
    return _validate_review(_parse_json_object(raw)), raw


async def _plan_user(
    client: LLMClient,
    semaphore: asyncio.Semaphore,
    *,
    anonymous_user: str,
    user_id: str,
    backup: dict[str, Any],
) -> dict[str, Any]:
    async with semaphore:
        report = _existing_report(backup, user_id)
        timeline = _timeline(backup, user_id)
        failed_indexes = sorted(
            {
                index
                for index, row in enumerate(
                    [
                        item
                        for item in backup["timeline_events"]
                        if item["user_id"] == user_id and item["text"]
                    ],
                    start=1,
                )
                if (
                    row.get("response_payload", {})
                    .get("_agent2_turn_observation_v1", {})
                    .get("business_result_status")
                    == "failed"
                )
            }
        )
        payload = {
            "report_date": REPORT_DATE,
            "trusted_current_report": _existing_prompt(report),
            "ordered_user_timeline": timeline,
            "failed_source_message_indexes": failed_indexes,
        }
        raw_plan, raw_plan_text = await _complete_plan(
            client,
            payload,
            feedback=None,
        )
        repaired_once = False
        try:
            plan = _validate_and_materialize(
                raw_plan,
                report=report,
                timeline=timeline,
            )
        except ValueError as exc:
            repaired_once = True
            raw_plan, raw_plan_text = await _complete_plan(
                client,
                payload,
                feedback=f"structural validation: {exc}",
            )
            plan = _validate_and_materialize(
                raw_plan,
                report=report,
                timeline=timeline,
            )
        review, raw_review_text = await _complete_review(client, payload, plan)
        first_review = review
        if review["decision"] == "reject" and not repaired_once:
            repaired_once = True
            raw_plan, raw_plan_text = await _complete_plan(
                client,
                payload,
                feedback=review["reason"],
            )
            plan = _validate_and_materialize(
                raw_plan,
                report=report,
                timeline=timeline,
            )
            review, raw_review_text = await _complete_review(
                client,
                payload,
                plan,
            )
        return {
            "anonymous_user": anonymous_user,
            "user_id": user_id,
            "report_id": report["id"] if report is not None else None,
            "report_preexisted": report is not None,
            "source_timeline_sha256": _sha256_json(timeline),
            "plan": plan,
            "plan_sha256": _sha256_json(plan),
            "review": review,
            "review_sha256": _sha256_json(review),
            "repaired_once": repaired_once,
            "first_review": first_review,
            "raw_plan_response_sha256": hashlib.sha256(
                raw_plan_text.encode("utf-8")
            ).hexdigest(),
            "raw_review_response_sha256": hashlib.sha256(
                raw_review_text.encode("utf-8")
            ).hexdigest(),
        }


def _write_plan(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
        payload,
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
        "sha256": _sha256_bytes(encoded),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--resume-plan", type=Path)
    parser.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
    parser.add_argument("--alias-prefix", default="affected")
    args = parser.parse_args()
    backup = _read_verified_backup(args.backup, args.backup_sha256)
    users = sorted(backup["users"], key=lambda row: row["id"])
    requested = {
        value.strip() for value in args.only.split(",") if value.strip()
    }
    task_specs = [
        (f"{args.alias_prefix}-{index:02d}", user)
        for index, user in enumerate(users, start=1)
        if not requested or f"{args.alias_prefix}-{index:02d}" in requested
    ]
    if requested and requested != {alias for alias, _user in task_specs}:
        raise RuntimeError("--only contains an unknown affected-user alias")
    resumed_results: list[dict[str, Any]] = []
    resumed_failures: list[dict[str, str]] = []
    if args.resume_plan is not None:
        resumed = json.loads(args.resume_plan.read_text(encoding="utf-8"))
        if resumed.get("backup_sha256") != args.backup_sha256:
            raise RuntimeError("resume plan belongs to a different backup")
        resumed_results = [
            row
            for row in resumed.get("results", [])
            if row.get("anonymous_user") not in requested
        ]
        resumed_failures = [
            row
            for row in resumed.get("failures", [])
            if row.get("anonymous_user") not in requested
        ]
    client = LLMClient(get_settings())
    if args.concurrency < 1 or args.concurrency > MAX_CONCURRENCY:
        raise RuntimeError("concurrency must be between 1 and 3")
    semaphore = asyncio.Semaphore(args.concurrency)
    try:
        results = await asyncio.gather(
            *(
                _plan_user(
                    client,
                    semaphore,
                    anonymous_user=anonymous_user,
                    user_id=user["id"],
                    backup=backup,
                )
                for anonymous_user, user in task_specs
            ),
            return_exceptions=True,
        )
    finally:
        await client.close()
    failures = resumed_failures + [
        {
            "anonymous_user": task_specs[index - 1][0],
            "error": f"{type(result).__name__}: {result}",
        }
        for index, result in enumerate(results, start=1)
        if isinstance(result, BaseException)
    ]
    successful = resumed_results + [
        result for result in results if isinstance(result, dict)
    ]
    successful.sort(key=lambda row: row["anonymous_user"])
    non_approved = [
        row["anonymous_user"]
        for row in successful
        if row["review"]["decision"] != "approve"
    ]
    payload = {
        "schema_version": "agent2.aug17.daily-repair-plan.v1",
        "backup_path": str(args.backup),
        "backup_sha256": args.backup_sha256,
        "model": CANARY_MODEL_NAME,
        "thinking_enabled": True,
        "results": successful,
        "failures": failures,
        "non_approved": non_approved,
    }
    written = _write_plan(args.output, payload)
    summary = {
        "status": (
            "pass" if not failures and not non_approved else "failed"
        ),
        "planned_users": len(successful),
        "changed_users": sum(
            row["plan"]["decision"] == "change" for row in successful
        ),
        "no_change_users": sum(
            row["plan"]["decision"] == "no_change" for row in successful
        ),
        "repaired_once": sum(row["repaired_once"] for row in successful),
        "non_approved": non_approved,
        "failures": failures,
        "output": written,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if summary["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
