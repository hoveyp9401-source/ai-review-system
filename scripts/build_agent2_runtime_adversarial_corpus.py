from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.adversarial_corpus import generate_adversarial_corpus  # noqa: E402
from app.agent2.evaluation.review_packet import build_reviewer_packet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic four-dimensional Runtime adversarial corpus."
    )
    parser.add_argument("--seed", type=int, default=20260710)
    parser.add_argument("--variants", type=int, default=4)
    parser.add_argument(
        "--output-dir",
        default="outputs/agent2_runtime_adversarial",
    )
    args = parser.parse_args()
    corpus = generate_adversarial_corpus(
        seed=args.seed,
        variants_per_template=args.variants,
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "adversarial_source.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in corpus.source_records
        ),
        encoding="utf-8",
    )
    payloads = {
        "blind_input_pack.json": corpus.input_pack.as_mapping(),
        "sealed_labels.json": corpus.sealed_labels.as_mapping(),
        "independent_reviewer_packet.json": build_reviewer_packet(
            corpus.input_pack,
            corpus.sealed_labels,
        ),
    }
    for name, payload in payloads.items():
        (output / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    categories: dict[str, int] = {}
    for row in corpus.source_records:
        category = str(row["category"])
        categories[category] = categories.get(category, 0) + 1
    print(
        json.dumps(
            {
                "seed": args.seed,
                "cases": len(corpus.source_records),
                "categories": categories,
                "blind_digest": corpus.input_pack.digest,
                "sealed_label_hash": corpus.sealed_labels.seal_hash,
                "independent_review_status": "pending",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
