from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from scripts.release_baseline_inventory import scan_repository


def _run(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_scan_repository_classifies_without_mutating_or_disclosing_secrets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "baseline@example.invalid")
    _run(root, "config", "user.name", "Baseline Test")

    (root / ".gitignore").write_text(".env\ntmp/\n", encoding="utf-8")
    (root / "app").mkdir()
    tracked = root / "app" / "runtime.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    _run(root, "add", ".gitignore", "app/runtime.py")
    _run(root, "commit", "-qm", "baseline")

    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    untracked = root / "app" / "new_runtime.py"
    untracked.write_text("NEW_VALUE = 1\n", encoding="utf-8")
    (root / "tmp").mkdir()
    temporary = root / "tmp" / "upload.py"
    temporary.write_text("print('temporary')\n", encoding="utf-8")
    secret = root / ".env"
    secret.write_text("API_TOKEN=do-not-disclose-this-value\n", encoding="utf-8")

    before = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in (tracked, untracked, temporary, secret)
    }

    report = scan_repository(root)

    after = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in (tracked, untracked, temporary, secret)
    }
    assert after == before

    files = {item.path: item for item in report.files}
    assert files["app/runtime.py"].git_state == "modified"
    assert files["app/runtime.py"].category == "source_runtime"
    assert files["app/runtime.py"].recommended_disposition == "commit_candidate"
    assert files["app/new_runtime.py"].git_state == "untracked"

    ignored = {item.path: item for item in report.sensitive_ignored_candidates}
    assert ignored[".env"].git_state == "ignored"
    assert ignored[".env"].recommended_disposition == "restricted_backup_only"
    assert ignored[".env"].sensitivity_signals == ("secret_filename", "secret_assignment")

    payload = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
    assert "do-not-disclose-this-value" not in payload
    assert temporary.exists()
    assert secret.exists()


def test_scan_repository_excludes_dependency_caches_from_sensitive_candidates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "baseline@example.invalid")
    _run(root, "config", "user.name", "Baseline Test")
    (root / ".gitignore").write_text(".env\nvenv/\n", encoding="utf-8")
    _run(root, "add", ".gitignore")
    _run(root, "commit", "-qm", "baseline")

    (root / ".env").write_text("API_TOKEN=keep-private\n", encoding="utf-8")
    cache = root / "venv" / "cache"
    cache.mkdir(parents=True)
    (cache / "token.txt").write_text("API_TOKEN=dependency-fixture\n", encoding="utf-8")

    report = scan_repository(root)

    paths = {item.path for item in report.sensitive_ignored_candidates}
    assert ".env" in paths
    assert "venv/cache/token.txt" not in paths


def test_source_that_handles_secret_fields_requires_review_but_remains_commit_eligible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "baseline@example.invalid")
    _run(root, "config", "user.name", "Baseline Test")
    (root / "app").mkdir()
    source = root / "app" / "config.py"
    source.write_text(
        "import os\npassword = os.getenv('DATABASE_PASSWORD', '')\n",
        encoding="utf-8",
    )

    report = scan_repository(root)

    item = {entry.path: entry for entry in report.files}["app/config.py"]
    assert item.sensitivity_signals == ("secret_assignment",)
    assert item.recommended_disposition == "review_before_commit"


def test_environment_template_is_deployment_input_not_a_secret_backup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "baseline@example.invalid")
    _run(root, "config", "user.name", "Baseline Test")
    template = root / ".env.example"
    template.write_text(
        "API_TOKEN=${API_TOKEN}\nLEGAL_OPS_TOKEN=\nAPP_ENV=development\n",
        encoding="utf-8",
    )

    report = scan_repository(root)

    item = {entry.path: entry for entry in report.files}[".env.example"]
    assert item.category == "deployment"
    assert item.sensitivity_signals == ()
    assert item.recommended_disposition == "commit_candidate"


def test_root_level_snapshots_and_project_controls_receive_reviewable_categories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q")
    _run(root, "config", "user.email", "baseline@example.invalid")
    _run(root, "config", "user.name", "Baseline Test")
    (root / "app_config.py").write_text(
        "password = os.getenv('DATABASE_PASSWORD', '')\n",
        encoding="utf-8",
    )
    (root / "tests_test_runtime.py").write_text("def test_runtime(): pass\n")
    (root / "reports.py").write_text("VALUE = 1\n", encoding="utf-8")
    sync = root / "__codex_tmp_sync__"
    sync.mkdir()
    (sync / "action_intake.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "BASELINE_REPORT.md").write_text("# Baseline\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")

    report = scan_repository(root)

    files = {entry.path: entry for entry in report.files}
    assert files["app_config.py"].category == "source_snapshot"
    assert files["app_config.py"].recommended_disposition == "review_before_commit"
    assert files["tests_test_runtime.py"].category == "test"
    assert files["reports.py"].category == "source_snapshot"
    assert files["reports.py"].recommended_disposition == "review_before_commit"
    assert files["__codex_tmp_sync__/action_intake.py"].category == "temporary_transfer"
    assert (
        files["__codex_tmp_sync__/action_intake.py"].recommended_disposition
        == "gitignore_candidate"
    )
    assert files["BASELINE_REPORT.md"].category == "documentation_contract"
    assert files["pytest.ini"].category == "deployment"
    assert files[".gitignore"].category == "deployment"
