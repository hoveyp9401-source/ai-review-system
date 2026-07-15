from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


_SECRET_FILENAME_RE = re.compile(
    r"(^\.env(?:\.|$)|credential|secret|token|password|passwd|"
    r"\.(?:pem|p12|pfx|key)$)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^[ \t]*(?:export[ \t]+)?[A-Z0-9_]*(?:API_KEY|APP_SECRET|CLIENT_SECRET|"
    r"ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|TOKEN)"
    r"[ \t]*[:=][ \t]*[^\s${<]"
)
_MAX_SECRET_SCAN_BYTES = 1_000_000


@dataclass(frozen=True)
class FileInventory:
    path: str
    git_state: str
    category: str
    recommended_disposition: str
    size_bytes: int
    sha256: str | None
    sensitivity_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryInventory:
    root: str
    files: tuple[FileInventory, ...]
    sensitive_ignored_candidates: tuple[FileInventory, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_version": "agent2.release_baseline.inventory.v1",
            "root": self.root,
            "files": [asdict(item) for item in self.files],
            "sensitive_ignored_candidates": [
                asdict(item) for item in self.sensitive_ignored_candidates
            ],
        }


def scan_repository(root: Path) -> RepositoryInventory:
    """Return a read-only Git and sensitivity inventory for one repository.

    The interface never mutates, deletes, moves, stages, or rewrites repository
    files. Secret-like values may be inspected only to emit closed signal names;
    file content and matched values are never returned.
    """

    resolved_root = root.resolve(strict=True)
    tracked = set(_git_paths(resolved_root, "ls-files"))
    untracked = set(
        _git_paths(resolved_root, "ls-files", "--others", "--exclude-standard")
    )
    modified = set(_git_paths(resolved_root, "diff", "--name-only"))
    staged = set(_git_paths(resolved_root, "diff", "--cached", "--name-only"))

    files = tuple(
        _inventory_file(
            resolved_root,
            relative_path,
            git_state=_git_state(relative_path, tracked, untracked, modified, staged),
        )
        for relative_path in sorted(tracked | untracked)
        if _safe_regular_file(resolved_root, relative_path)
    )

    ignored_paths = _git_paths(
        resolved_root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
    )
    sensitive_ignored = []
    for relative_path in sorted(set(ignored_paths)):
        if _is_dependency_cache(relative_path):
            continue
        if not _safe_regular_file(resolved_root, relative_path):
            continue
        signals = _sensitivity_signals(resolved_root / relative_path, relative_path)
        if not signals:
            continue
        candidate = _inventory_file(
            resolved_root,
            relative_path,
            git_state="ignored",
            known_signals=signals,
        )
        sensitive_ignored.append(candidate)

    return RepositoryInventory(
        root=resolved_root.as_posix(),
        files=files,
        sensitive_ignored_candidates=tuple(sensitive_ignored),
    )


def _git_paths(root: Path, *args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args, "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    )


def _git_state(
    path: str,
    tracked: set[str],
    untracked: set[str],
    modified: set[str],
    staged: set[str],
) -> str:
    if path in untracked:
        return "untracked"
    if path in staged and path in modified:
        return "staged_and_modified"
    if path in staged:
        return "staged"
    if path in modified:
        return "modified"
    if path in tracked:
        return "tracked"
    return "unknown"


def _safe_regular_file(root: Path, relative_path: str) -> bool:
    path = root / relative_path
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _inventory_file(
    root: Path,
    relative_path: str,
    *,
    git_state: str,
    known_signals: tuple[str, ...] | None = None,
) -> FileInventory:
    path = root / relative_path
    signals = known_signals or _sensitivity_signals(path, relative_path)
    category = _category(relative_path)
    disposition = _disposition(category, signals)
    try:
        data = path.read_bytes()
    except (OSError, PermissionError):
        size_bytes = 0
        digest = None
        signals = tuple(dict.fromkeys((*signals, "unreadable")))
        disposition = "restricted_backup_only"
    else:
        size_bytes = len(data)
        digest = hashlib.sha256(data).hexdigest()
    return FileInventory(
        path=relative_path,
        git_state=git_state,
        category=category,
        recommended_disposition=disposition,
        size_bytes=size_bytes,
        sha256=digest,
        sensitivity_signals=signals,
    )


def _sensitivity_signals(path: Path, relative_path: str) -> tuple[str, ...]:
    signals: list[str] = []
    filename = Path(relative_path).name.lower()
    if filename != ".env.example" and _SECRET_FILENAME_RE.search(filename):
        signals.append("secret_filename")
    try:
        if path.stat().st_size <= _MAX_SECRET_SCAN_BYTES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _SECRET_ASSIGNMENT_RE.search(text):
                signals.append("secret_assignment")
    except (OSError, PermissionError):
        signals.append("unreadable")
    return tuple(dict.fromkeys(signals))


def _category(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    top = normalized.split("/", 1)[0].lower()
    name = Path(normalized).name.lower()
    is_root_file = "/" not in normalized
    if top == "__codex_tmp_sync__":
        return "temporary_transfer"
    if top in {"app", "workflows"}:
        return "source_runtime"
    if top == "tests":
        return "test"
    if is_root_file and (
        name.startswith("test_") or name.startswith("tests_test_")
    ) and name.endswith(".py"):
        return "test"
    if is_root_file and name.endswith(".py"):
        return "source_snapshot"
    if top in {"deploy"} or name in {
        ".env.example",
        ".gitignore",
        "dockerfile",
        "docker-compose.yml",
        "pytest.ini",
        "requirements.txt",
        "skills-lock.json",
        "pyproject.toml",
    }:
        return "deployment"
    if top in {"database", "migrations"} or name.endswith(".sql"):
        return "migration"
    if top == "scripts":
        return "tooling"
    if top == "evals":
        return "evaluation"
    if top in {"artifacts", "outputs"} or normalized.startswith("docs/evidence/"):
        return "evidence_report"
    if top == "docs" or name in {"context.md", "readme.md"} or (
        is_root_file and name.endswith(".md")
    ):
        return "documentation_contract"
    if top in {
        ".codex_tmp",
        "tmp",
        "backups",
        "remote_current",
        "remote_edit",
        "server_patch",
    } or top.startswith("tmp_") or "upload_tmp" in top:
        return "temporary_transfer"
    if top in {"data", "dist"} or Path(normalized).suffix.lower() in {
        ".csv",
        ".xlsx",
        ".xls",
        ".docx",
        ".db",
        ".sqlite",
        ".sqlite3",
    }:
        return "data_export"
    return "unknown"


def _is_dependency_cache(relative_path: str) -> bool:
    parts = relative_path.replace("\\", "/").lower().split("/")
    return any(
        part in {"venv", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
        for part in parts
    )


def _disposition(category: str, signals: Iterable[str]) -> str:
    signal_set = set(signals)
    if signal_set & {"secret_filename", "unreadable"}:
        return "restricted_backup_only"
    if "secret_assignment" in signal_set:
        if category in {
            "source_runtime",
            "source_snapshot",
            "test",
            "deployment",
            "migration",
            "tooling",
            "documentation_contract",
        }:
            return "review_before_commit"
        return "restricted_backup_only"
    if category in {
        "source_runtime",
        "test",
        "deployment",
        "migration",
        "tooling",
        "documentation_contract",
    }:
        return "commit_candidate"
    if category == "source_snapshot":
        return "review_before_commit"
    if category in {"evaluation", "evidence_report"}:
        return "review_before_commit"
    if category == "temporary_transfer":
        return "gitignore_candidate"
    if category == "data_export":
        return "restricted_backup_only"
    return "unknown_review"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = scan_repository(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "files": len(report.files),
                "sensitive_ignored_candidates": len(
                    report.sensitive_ignored_candidates
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
