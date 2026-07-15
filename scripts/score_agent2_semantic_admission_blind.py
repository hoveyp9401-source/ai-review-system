from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.semantic_admission_blind import (  # noqa: E402
    SemanticAdmissionActualArtifact,
)
from app.agent2.evaluation.semantic_admission_scoring import (  # noqa: E402
    SealedSemanticAdmissionLabels,
    score_semantic_admission_actual,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score a completed Semantic Admission actual against separately sealed labels."
    )
    parser.add_argument("--actual", required=True)
    parser.add_argument("--sealed-labels", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    actual = SemanticAdmissionActualArtifact.from_mapping(
        json.loads(Path(args.actual).read_text(encoding="utf-8"))
    )
    labels = SealedSemanticAdmissionLabels.from_mapping(
        json.loads(Path(args.sealed_labels).read_text(encoding="utf-8"))
    )
    score = score_semantic_admission_actual(actual, labels)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(score, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "label_count": score["label_count"],
                "mismatch_count": score["mismatch_count"],
                "independent_metrics_available": score[
                    "independent_metrics_available"
                ],
                "acceptance_eligible": score["acceptance_eligible"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

