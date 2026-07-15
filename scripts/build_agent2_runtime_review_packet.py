from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.review_packet import build_reviewer_packet  # noqa: E402
from app.agent2.evaluation.runtime_scoring import SealedLabelStore  # noqa: E402
from app.agent2.runtime.blind import BlindInputPack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an independent-review packet from blind inputs and machine labels."
    )
    parser.add_argument("--blind-input", required=True)
    parser.add_argument("--sealed-labels", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    blind_pack = BlindInputPack.from_mapping(
        json.loads(Path(args.blind_input).read_text(encoding="utf-8"))
    )
    labels = SealedLabelStore.from_mapping(
        json.loads(Path(args.sealed_labels).read_text(encoding="utf-8"))
    )
    packet = build_reviewer_packet(blind_pack, labels)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "record_count": packet["record_count"],
                "human_approved_count": packet["human_approved_count"],
                "packet_hash": packet["packet_hash"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
