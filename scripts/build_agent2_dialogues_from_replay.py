from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Agent2 dialogue JSONL from historical replay rows.")
    parser.add_argument("--input", required=True, help="workflow_gate_replay JSON containing rows with raw_text.")
    parser.add_argument("--output", required=True, help="Output dialogue JSONL path.")
    parser.add_argument("--gap-minutes", type=int, default=30, help="Split dialogue when adjacent messages exceed this gap.")
    parser.add_argument("--min-turns", type=int, default=2, help="Only emit dialogues with at least this many turns.")
    parser.add_argument("--dedupe-window-seconds", type=int, default=8, help="Drop duplicate rows for same user/text near in time.")
    parser.add_argument("--max-turns", type=int, default=80, help="Split very long dialogues into chunks.")
    args = parser.parse_args()

    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = list(source.get("rows") or [])
    normalized = [_normalize_row(row) for row in rows if str(row.get("raw_text") or "").strip()]
    deduped = _dedupe_rows(normalized, window=timedelta(seconds=args.dedupe_window_seconds))
    dialogues = _build_dialogues(
        deduped,
        gap=timedelta(minutes=args.gap_minutes),
        min_turns=args.min_turns,
        max_turns=args.max_turns,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for dialogue in dialogues:
            handle.write(json.dumps(dialogue, ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        "input_rows": len(rows),
        "rows_with_text": len(normalized),
        "deduped_rows": len(deduped),
        "dialogues": len(dialogues),
        "turns": sum(len(dialogue["turns"]) for dialogue in dialogues),
        "source_kind": dict(Counter(row["source_kind"] for row in deduped)),
        "dialogue_turns_min": min((len(dialogue["turns"]) for dialogue in dialogues), default=0),
        "dialogue_turns_max": max((len(dialogue["turns"]) for dialogue in dialogues), default=0),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    created_at = _parse_datetime(str(row.get("created_at") or ""))
    raw_text = str(row.get("raw_text") or "").strip()
    user_key = str(row.get("user_id") or row.get("dingtalk_user_id") or row.get("user_name") or "unknown")
    return {
        "source_kind": str(row.get("source_kind") or ""),
        "source_id": str(row.get("source_id") or ""),
        "created_at": created_at,
        "created_at_text": str(row.get("created_at") or ""),
        "user_key": user_key,
        "user_id": str(row.get("user_id") or ""),
        "user_name": str(row.get("user_name") or ""),
        "dingtalk_user_id": str(row.get("dingtalk_user_id") or ""),
        "raw_text": raw_text,
        "raw_text_hash": str(row.get("raw_text_hash") or ""),
        "raw_text_chars": int(row.get("raw_text_chars") or len(raw_text)),
        "legacy_processed": bool(row.get("legacy_processed") or False),
        "legacy_action": str(row.get("legacy_action") or ""),
        "legacy_write_impact": bool(row.get("legacy_write_impact") or False),
        "legacy_impact_kind": str(row.get("legacy_impact_kind") or ""),
        "legacy_status": str(row.get("legacy_status") or ""),
        "report_date": str(row.get("report_date") or ""),
        "metadata": {
            "source_kind": str(row.get("source_kind") or ""),
            "source_id": str(row.get("source_id") or ""),
            "legacy_action": str(row.get("legacy_action") or ""),
            "legacy_write_impact": bool(row.get("legacy_write_impact") or False),
            "legacy_impact_kind": str(row.get("legacy_impact_kind") or ""),
            "legacy_status": str(row.get("legacy_status") or ""),
            "report_date": str(row.get("report_date") or ""),
            "remote_primary_workflow": str(row.get("primary_workflow") or ""),
        },
    }


def _dedupe_rows(rows: list[dict[str, Any]], *, window: timedelta) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    last_seen: dict[tuple[str, str], datetime] = {}
    for row in sorted(rows, key=lambda item: (item["created_at"], item["source_kind"], item["source_id"])):
        key = (row["user_key"], row["raw_text_hash"] or row["raw_text"])
        previous_time = last_seen.get(key)
        if previous_time is not None and abs(row["created_at"] - previous_time) <= window:
            continue
        last_seen[key] = row["created_at"]
        result.append(row)
    return result


def _build_dialogues(
    rows: list[dict[str, Any]],
    *,
    gap: timedelta,
    min_turns: int,
    max_turns: int,
) -> list[dict[str, Any]]:
    by_user: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_user.setdefault(row["user_key"], []).append(row)

    dialogues: list[dict[str, Any]] = []
    for user_key, user_rows in sorted(by_user.items()):
        current: list[dict[str, Any]] = []
        previous_time: datetime | None = None
        chunk_index = 1
        for row in sorted(user_rows, key=lambda item: item["created_at"]):
            should_split = previous_time is not None and row["created_at"] - previous_time > gap
            if should_split or len(current) >= max_turns:
                _append_dialogue(dialogues, current, user_key=user_key, chunk_index=chunk_index, min_turns=min_turns)
                chunk_index += 1
                current = []
            current.append(row)
            previous_time = row["created_at"]
        _append_dialogue(dialogues, current, user_key=user_key, chunk_index=chunk_index, min_turns=min_turns)
    return dialogues


def _append_dialogue(
    dialogues: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    user_key: str,
    chunk_index: int,
    min_turns: int,
) -> None:
    if len(rows) < min_turns:
        return
    first = rows[0]
    dialogue_id = f"history-{_safe_id(user_key)}-{first['created_at'].strftime('%Y%m%d%H%M%S')}-{chunk_index}"
    turns = [
        {
            "turn_id": f"turn-{index}",
            "text": row["raw_text"],
            "metadata": {
                **row["metadata"],
                "created_at": row["created_at_text"],
                "raw_text_hash": row["raw_text_hash"],
                "raw_text_chars": row["raw_text_chars"],
            },
        }
        for index, row in enumerate(rows, start=1)
    ]
    dialogues.append(
        {
            "dialogue_id": dialogue_id,
            "source": "server_history",
            "sender_id": first["user_id"] or user_key,
            "sender_name": first["user_name"],
            "dingtalk_user_id": first["dingtalk_user_id"],
            "conversation_id": dialogue_id,
            "metadata": {
                "user_key": user_key,
                "start_at": first["created_at_text"],
                "end_at": rows[-1]["created_at_text"],
                "turn_count": len(turns),
                "source_kind_counts": dict(Counter(row["source_kind"] for row in rows)),
            },
            "turns": turns,
        }
    )


def _parse_datetime(value: str) -> datetime:
    if not value:
        return datetime.min
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value)
    return cleaned.strip("-")[:48] or "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
