from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.fuzz_report import (  # noqa: E402
    build_fuzz_failure_report,
    build_fuzz_retry_pack,
)
from app.agent2.evaluation.runtime_scoring import SealedLabelStore  # noqa: E402
from app.agent2.runtime.blind import BlindActualArtifact, BlindInputPack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Cluster adversarial Blind Runtime failures.")
    parser.add_argument("--blind-input", required=True)
    parser.add_argument("--actual", required=True)
    parser.add_argument("--sealed-labels", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--retry-blind-output", required=True)
    parser.add_argument("--retry-label-output", required=True)
    args = parser.parse_args()
    input_pack = BlindInputPack.from_mapping(_read(args.blind_input))
    actual = BlindActualArtifact.from_mapping(_read(args.actual))
    labels = SealedLabelStore.from_mapping(_read(args.sealed_labels))
    report = build_fuzz_failure_report(input_pack, actual, labels)
    retry_pack, retry_labels = build_fuzz_retry_pack(input_pack, report, labels)
    _write(args.output, report)
    _write(args.retry_blind_output, retry_pack.as_mapping())
    _write(args.retry_label_output, retry_labels.as_mapping())
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
