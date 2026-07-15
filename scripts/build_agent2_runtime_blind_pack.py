from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.blind_pack_builder import build_blind_pack  # noqa: E402
from app.agent2.runtime.replay import load_runtime_dialogue_cases  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split scored Agent2 dialogue sources into blind inputs and sealed labels."
    )
    parser.add_argument("--manifest", default="evals/agent2/runtime/phase1_inputs.json")
    parser.add_argument("--pack-id", default="agent2-runtime-phase1-blind-20260710")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--blind-output",
        default="outputs/agent2_runtime_blind/blind_input_pack.json",
    )
    parser.add_argument(
        "--sealed-label-output",
        default="outputs/agent2_runtime_blind/sealed_labels.json",
    )
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("blind pack manifest requires a non-empty inputs array")
    cases, _ = load_runtime_dialogue_cases(inputs)
    if args.limit > 0:
        cases = cases[: args.limit]
        args.pack_id = f"{args.pack_id}-limit-{args.limit}"
    blind_pack, labels = build_blind_pack(cases, pack_id=args.pack_id)
    blind_path = Path(args.blind_output)
    label_path = Path(args.sealed_label_output)
    if blind_path.resolve() == label_path.resolve():
        raise ValueError("blind inputs and sealed labels must use different files")
    blind_path.parent.mkdir(parents=True, exist_ok=True)
    label_path.parent.mkdir(parents=True, exist_ok=True)
    blind_path.write_text(
        json.dumps(blind_pack.as_mapping(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    label_path.write_text(
        json.dumps(labels.as_mapping(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "blind_cases": len(blind_pack.cases),
                "blind_turns": sum(len(case.turns) for case in blind_pack.cases),
                "blind_digest": blind_pack.digest,
                "sealed_label_count": len(labels.labels),
                "sealed_label_hash": labels.seal_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
