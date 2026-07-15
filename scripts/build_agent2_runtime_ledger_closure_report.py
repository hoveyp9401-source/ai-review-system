from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.ledger_report import build_ledger_closure_report  # noqa: E402
from app.agent2.evaluation.runtime_scoring import SealedLabelStore  # noqa: E402
from app.agent2.runtime.blind import BlindActualArtifact, BlindInputPack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the non-oracle 73-ledger machine-candidate closure report."
    )
    parser.add_argument("--blind-input", required=True)
    parser.add_argument("--actual", required=True)
    parser.add_argument("--sealed-labels", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    input_pack = BlindInputPack.from_mapping(_read(args.blind_input))
    actual = BlindActualArtifact.from_mapping(_read(args.actual))
    labels = SealedLabelStore.from_mapping(_read(args.sealed_labels))
    report = build_ledger_closure_report(input_pack, actual, labels)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
