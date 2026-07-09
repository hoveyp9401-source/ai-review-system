from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from app.agent2.harness.schemas import HarnessCase


def discover_case_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.jsonl")))
        elif path.is_file():
            files.append(path)
    return files


def load_cases(paths: Iterable[str | Path]) -> list[HarnessCase]:
    cases: list[HarnessCase] = []
    for path in discover_case_files(paths):
        cases.extend(load_jsonl_cases(path))
    return cases


def load_jsonl_cases(path: str | Path) -> list[HarnessCase]:
    case_path = Path(path)
    cases: list[HarnessCase] = []
    with case_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {case_path}:{line_no}: {exc}") from exc
            try:
                cases.append(HarnessCase.from_mapping(payload))
            except Exception as exc:
                raise ValueError(f"Invalid harness case in {case_path}:{line_no}: {exc}") from exc
    return cases
