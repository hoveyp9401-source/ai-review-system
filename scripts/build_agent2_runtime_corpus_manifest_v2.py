from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize the Agent2 corpus inventory to the offline-hardening v2 schema."
    )
    parser.add_argument(
        "--source",
        default="evals/agent2/runtime/phase1_acceptance/corpus_manifest.json",
    )
    parser.add_argument(
        "--output",
        default="outputs/agent2_runtime_corpus_manifest_v2.json",
    )
    args = parser.parse_args()
    source = json.loads(Path(args.source).read_text(encoding="utf-8"))
    entries = [_normalize_entry(entry) for entry in source.get("entries") or []]
    payload = {
        "schema_version": "agent2.corpus_manifest.v2",
        "source_manifest": args.source,
        "entry_count": len(entries),
        "required_fields": [
            "corpus_id",
            "source",
            "path",
            "hash",
            "schema",
            "conversation_count",
            "turn_count",
            "generation_method",
            "independence",
            "baseline_derived",
            "risk_coverage",
            "recoverability",
            "missing_reason",
        ],
        "target": source.get("target"),
        "search_evidence": source.get("search_evidence"),
        "entries": entries,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "entries": len(entries),
                "local_available": sum(
                    entry["recoverability"] == "local_available" for entry in entries
                ),
                "external_required": sum(
                    entry["recoverability"] == "external_required" for entry in entries
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def _normalize_entry(entry: dict) -> dict:
    path = entry.get("source_path")
    source = str(entry.get("generation_source") or entry.get("corpus_type") or "unknown")
    identity = str(path or entry.get("corpus_type") or source)
    corpus_id = "corpus-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    risk_coverage = entry.get("risk_coverage")
    if not risk_coverage:
        risk_coverage = ["unknown_not_annotated"]
    elif isinstance(risk_coverage, str):
        risk_coverage = [risk_coverage]
    return {
        "corpus_id": corpus_id,
        "source": source,
        "path": path,
        "hash": entry.get("hash"),
        "schema": entry.get("expected_schema"),
        "conversation_count": int(entry.get("conversation_count") or 0),
        "turn_count": int(entry.get("turn_count") or 0),
        "generation_method": str(entry.get("generation_source") or "unknown"),
        "independence": (
            "independent" if bool(entry.get("whether_independent")) else "not_independent"
        ),
        "baseline_derived": bool(entry.get("whether_baseline_derived")),
        "risk_coverage": list(risk_coverage),
        "recoverability": "local_available" if path else "external_required",
        "missing_reason": entry.get("missing_reason"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
