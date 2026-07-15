from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.blind_pack_builder import build_blind_pack  # noqa: E402
from app.agent2.evaluation.review_packet import build_reviewer_packet  # noqa: E402
from app.agent2.evaluation.runtime_scoring import SealedLabelStore  # noqa: E402
from app.agent2.runtime.replay import load_runtime_dialogue_cases  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a blind replay pack focused on the historical 73-case Runtime ledger."
    )
    parser.add_argument("--manifest", default="evals/agent2/runtime/phase1_inputs.json")
    parser.add_argument(
        "--ledger",
        default="evals/agent2/runtime/phase1_acceptance/case_ledger.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/agent2_runtime_blind/ledger73",
    )
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    cases, _ = load_runtime_dialogue_cases(manifest["inputs"])
    ledger_rows = [
        json.loads(line)
        for line in Path(args.ledger).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    focus = {
        (str(row["conversation_case_id"]), str(row["turn_id"])): {
            "ledger_focus": True,
            "historical_anomaly_kind": row["anomaly_kind"],
            "historical_risk_level": row["risk_level"],
            "historical_root_cause_category": row["root_cause_category"],
            "present_in_before_replay": bool(row.get("present_in_before_replay", True)),
            "newly_surfaced_after_fix": bool(row.get("newly_surfaced_after_fix", False)),
            "old_closure_evidence_eligible": False,
        }
        for row in ledger_rows
    }
    selected = []
    found_keys: set[tuple[str, str]] = set()
    for case in cases:
        focus_indices = [
            index
            for index, turn in enumerate(case.turns)
            if (case.dialogue_id, turn.turn_id) in focus
        ]
        if not focus_indices:
            continue
        max_index = max(focus_indices)
        prefix = case.turns[: max_index + 1]
        found_keys.update(
            (case.dialogue_id, turn.turn_id)
            for turn in prefix
            if (case.dialogue_id, turn.turn_id) in focus
        )
        selected.append(replace(case, turns=prefix))
    missing = sorted(set(focus) - found_keys)
    if missing:
        raise ValueError(f"ledger focus turns missing from source corpus: {missing[:10]}")
    blind_pack, all_labels = build_blind_pack(
        selected,
        pack_id="agent2-runtime-ledger73-blind-20260710",
        focus_annotations=focus,
    )
    focus_labels = [
        label
        for label in all_labels.labels
        if isinstance(label.get("adjudication"), dict)
        and label["adjudication"].get("ledger_focus") is True
    ]
    labels = SealedLabelStore.seal(input_pack=blind_pack, labels=focus_labels)
    reviewer_packet = build_reviewer_packet(blind_pack, labels)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "blind_input_pack.json": blind_pack.as_mapping(),
        "sealed_labels.json": labels.as_mapping(),
        "independent_reviewer_packet.json": reviewer_packet,
    }
    for name, payload in files.items():
        (output / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "source_ledger_rows": len(ledger_rows),
                "focus_labels": len(labels.labels),
                "blind_dialogues": len(blind_pack.cases),
                "blind_turns_with_prefix": sum(len(case.turns) for case in blind_pack.cases),
                "blind_digest": blind_pack.digest,
                "sealed_label_hash": labels.seal_hash,
                "human_approved_count": reviewer_packet["human_approved_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
