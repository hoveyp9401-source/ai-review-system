from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.agent2.evaluation.historical_daily_replay import (
    run_historical_daily_replay_sync,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay the two historical Daily context failures in memory."
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_historical_daily_replay_sync(args.fixture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
