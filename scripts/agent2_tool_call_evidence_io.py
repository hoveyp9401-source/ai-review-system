from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = (
    REPO_ROOT / "evals" / "agent2" / "tool_call_shadow"
).resolve()
_ALLOWED_SUFFIXES = {".json", ".jsonl"}


def atomic_write_json(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    output_root: str | Path | None = None,
) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        output_root=output_root,
    )


def atomic_write_jsonl(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    output_root: str | Path | None = None,
) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        output_root=output_root,
    )


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    output_root: str | Path | None = None,
) -> None:
    target, root = _validated_output_path(path, output_root=output_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(target, root=root)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validated_output_path(
    path: str | Path,
    *,
    output_root: str | Path | None,
) -> tuple[Path, Path]:
    root = (
        EVIDENCE_ROOT
        if output_root is None
        else Path(output_root).resolve(strict=False)
    )
    if root == Path(root.anchor):
        raise ValueError("configured evidence root must be a bounded directory")
    raw = Path(path)
    candidate = raw if raw.is_absolute() else root / raw
    target = candidate.resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        scope = (
            "the Tool-Call eval root"
            if output_root is None
            else "the configured evidence root"
        )
        raise ValueError(f"evidence output must stay under {scope}") from exc
    if target == root or target.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError("evidence output must be a JSON or JSONL file")
    return target, root


def _reject_symlink_chain(target: Path, *, root: Path) -> None:
    relative = target.relative_to(root)
    current = root
    if current.is_symlink():
        raise ValueError("evidence root cannot be a symlink")
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("evidence output cannot traverse a symlink")
