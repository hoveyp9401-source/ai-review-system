from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.determinism_report import build_determinism_report  # noqa: E402
from app.agent2.runtime.blind import BlindActualArtifact  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two completed Agent2 Blind Runtime artifacts exactly."
    )
    parser.add_argument("--first", required=True)
    parser.add_argument("--second", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    first = BlindActualArtifact.from_mapping(_read(args.first))
    second = BlindActualArtifact.from_mapping(_read(args.second))
    report = build_determinism_report(first, second)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "comparable": report["comparable"],
                "determinism_passed": report["determinism_passed"],
                "turns": report["turns"],
                "safety_envelope_stable": report["safety_envelope_stable"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
