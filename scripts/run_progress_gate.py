from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    timeout_seconds: int = 300


def build_checks() -> list[Check]:
    python = sys.executable
    return [
        Check(
            name="progress-outbox-pytest",
            command=[python, "-m", "pytest", "tests/test_progress_outbox.py", "-q"],
        )
    ]


def run_check(check: Check) -> bool:
    print(f"\n=== PROGRESS_GATE_START {check.name} ===", flush=True)
    print(" ".join(check.command), flush=True)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            check.command,
            cwd=ROOT,
            timeout=check.timeout_seconds,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(f"PROGRESS_GATE_TIMEOUT {check.name} after {check.timeout_seconds}s", flush=True)
        return False
    seconds = round(time.perf_counter() - started, 1)
    ok = completed.returncode == 0
    status = "PASS" if ok else "FAIL"
    print(f"=== PROGRESS_GATE_END {check.name} {status} seconds={seconds} ===", flush=True)
    return ok


def main() -> int:
    failures = []
    started = time.perf_counter()
    checks = build_checks()
    for check in checks:
        if not run_check(check):
            failures.append(check.name)

    total_seconds = round(time.perf_counter() - started, 1)
    print("\nPROGRESS_GATE_SUMMARY_START", flush=True)
    print(f"total={len(checks)} pass={len(checks) - len(failures)} fail={len(failures)} seconds={total_seconds}", flush=True)
    if failures:
        print("failures=" + ",".join(failures), flush=True)
    print("PROGRESS_GATE_SUMMARY_END", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
