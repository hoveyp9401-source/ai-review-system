from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent2.audit_viewer import load_execution_turns, render_audit_viewer_html


DEFAULT_INPUT = Path("outputs/agent2_contract_history_daily_execution/daily_execution_results.jsonl")
DEFAULT_OUTPUT = Path("outputs/agent2_audit_viewer.html")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a static Agent2 cognitive audit viewer.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Replay or harness JSONL path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="HTML output path.")
    args = parser.parse_args()

    turns = load_execution_turns(args.input)
    output = render_audit_viewer_html(turns, args.output)
    print(f"rendered {len(turns)} turns -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
