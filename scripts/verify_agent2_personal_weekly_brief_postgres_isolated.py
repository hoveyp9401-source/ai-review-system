#!/usr/bin/env python3
"""Verify the personal-weekly migration in an ephemeral local PostgreSQL."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import uuid4
import zipfile


SCHEMA_VERSION = "agent2.personal_weekly_brief.postgres_isolated_gate.v1"
ALLOWED_DOWNLOAD_HOST = "sbp.enterprisedb.com"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--rollback", type=Path, required=True)
    parser.add_argument("--archive-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-temp-root", type=Path)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"isolated PostgreSQL command failed: {Path(command[0]).name}; "
            f"returncode={result.returncode}; stderr={result.stderr[-1200:]}"
        )
    return result


def _run_without_captured_handles(
    command: list[str],
    *,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    """Avoid keeping a capture pipe open in the long-lived postgres child."""

    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"isolated PostgreSQL control command failed: {Path(command[0]).name}; "
            f"returncode={result.returncode}"
        )
    return result


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _download(url: str, target: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_DOWNLOAD_HOST:
        raise ValueError("PostgreSQL archive URL is not the allow-listed EDB host")
    downloaded = 0
    next_notice = 25 * 1024 * 1024
    with urlopen(url, timeout=180) as response, target.open("xb") as output:
        expected_size = int(response.headers.get("Content-Length") or "0")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            downloaded += len(chunk)
            if downloaded >= next_notice:
                print(
                    json.dumps(
                        {
                            "event": "postgres_archive_download_progress",
                            "downloaded_mb": round(downloaded / 1024 / 1024, 1),
                        }
                    ),
                    flush=True,
                )
                next_notice += 25 * 1024 * 1024
    if downloaded < 20 * 1024 * 1024:
        raise RuntimeError("downloaded PostgreSQL archive is unexpectedly small")
    if expected_size and downloaded != expected_size:
        raise RuntimeError("downloaded PostgreSQL archive is incomplete")


def _extract(archive: Path, target: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if not members or any(
            Path(member.filename).is_absolute()
            or ".." in Path(member.filename).parts
            for member in members
        ):
            raise RuntimeError("PostgreSQL archive contains an unsafe path")
        bundle.extractall(target)
    candidates = tuple(target.rglob("initdb.exe"))
    if len(candidates) != 1:
        raise RuntimeError("PostgreSQL archive did not contain exactly one initdb.exe")
    return candidates[0].parent


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary evidence output already exists: {temporary}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _transactional(sql: str) -> str:
    """Wrap embeddable release SQL for standalone isolated verification."""

    return "BEGIN;\n" + sql.rstrip() + "\nCOMMIT;\n"


def main() -> int:
    args = _args()
    if not args.migration.is_file():
        raise FileNotFoundError("migration file not found")
    if not args.rollback.is_file():
        raise FileNotFoundError("rollback file not found")
    started_at = datetime.now(UTC)
    temp_root = (
        args.reuse_temp_root
        if args.reuse_temp_root is not None
        else Path(tempfile.mkdtemp(prefix="codex-pwb-postgres-"))
    )
    resolved_temp = temp_root.resolve()
    system_temp = Path(tempfile.gettempdir()).resolve()
    if (
        system_temp not in resolved_temp.parents
        or not resolved_temp.name.startswith("codex-pwb-postgres-")
    ):
        raise RuntimeError("temporary PostgreSQL root is outside the system temp directory")
    if args.reuse_temp_root is not None and not temp_root.is_dir():
        raise RuntimeError("requested reusable PostgreSQL temp root does not exist")

    archive = temp_root / "postgresql-binaries.zip"
    extracted = temp_root / "runtime"
    data = temp_root / "data"
    log = temp_root / "postgres.log"
    server_started = False
    bin_dir: Path | None = None
    checks: dict[str, Any] = {}
    failure = ""
    try:
        if archive.is_file():
            with zipfile.ZipFile(archive) as bundle:
                damaged_member = bundle.testzip()
            if damaged_member is not None:
                raise RuntimeError("downloaded PostgreSQL archive failed CRC verification")
            print(json.dumps({"event": "postgres_archive_reused_and_verified"}), flush=True)
        else:
            print(json.dumps({"event": "postgres_archive_download_started"}), flush=True)
            _download(args.archive_url, archive)
            print(json.dumps({"event": "postgres_archive_downloaded"}), flush=True)
        with zipfile.ZipFile(archive) as bundle:
            damaged_member = bundle.testzip()
        if damaged_member is not None:
            raise RuntimeError("downloaded PostgreSQL archive failed CRC verification")
        checks["archive_sha256"] = _sha256(archive)
        checks["archive_size_bytes"] = archive.stat().st_size
        checks["archive_zip_crc"] = "PASS"
        existing_initdb = tuple(extracted.rglob("initdb.exe")) if extracted.is_dir() else ()
        if len(existing_initdb) == 1:
            bin_dir = existing_initdb[0].parent
        else:
            if extracted.exists():
                shutil.rmtree(extracted)
            bin_dir = _extract(archive, extracted)
        if data.exists():
            shutil.rmtree(data)
        if log.exists():
            log.unlink()
        initdb = str(bin_dir / "initdb.exe")
        pg_ctl = str(bin_dir / "pg_ctl.exe")
        psql = str(bin_dir / "psql.exe")
        createdb = str(bin_dir / "createdb.exe")
        dropdb = str(bin_dir / "dropdb.exe")
        _run(
            [
                initdb,
                "-D",
                str(data),
                "-A",
                "trust",
                "-U",
                "postgres",
                "--encoding=UTF8",
                "--no-locale",
            ]
        )
        print(json.dumps({"event": "postgres_initdb_completed"}), flush=True)
        port = _free_local_port()
        _run_without_captured_handles(
            [
                pg_ctl,
                "-D",
                str(data),
                "-l",
                str(log),
                "-o",
                f"-h 127.0.0.1 -p {port}",
                "-w",
                "start",
            ]
        )
        server_started = True
        print(
            json.dumps({"event": "postgres_server_started", "listen_host": "127.0.0.1"}),
            flush=True,
        )
        common = ["-h", "127.0.0.1", "-p", str(port), "-U", "postgres"]
        version = _run([psql, *common, "-d", "postgres", "-Atc", "SHOW server_version"]).stdout.strip()
        checks["postgresql_version"] = version
        database_name = f"pwb_gate_{uuid4().hex[:12]}"
        _run([createdb, *common, database_name])
        print(json.dumps({"event": "postgres_database_created"}), flush=True)
        database_args = [psql, *common, "-d", database_name, "-v", "ON_ERROR_STOP=1"]
        before = _run(
            [*database_args, "-Atc", "SELECT to_regclass('public.agent2_personal_weekly_briefs') IS NULL"]
        ).stdout.strip()
        if before != "t":
            raise AssertionError("isolated database was not empty before migration")
        migration_text = args.migration.read_text(encoding="utf-8")
        rollback_text = args.rollback.read_text(encoding="utf-8")
        checks["migration_sha256"] = hashlib.sha256(
            migration_text.encode("utf-8")
        ).hexdigest()
        checks["rollback_sha256"] = hashlib.sha256(
            rollback_text.encode("utf-8")
        ).hexdigest()
        _run(database_args, input_text=_transactional(migration_text))
        checks["first_apply"] = "PASS"
        print(json.dumps({"event": "postgres_migration_first_apply_passed"}), flush=True)
        _run(database_args, input_text=_transactional(migration_text))
        checks["idempotent_second_apply"] = "PASS"
        print(json.dumps({"event": "postgres_migration_second_apply_passed"}), flush=True)
        table_present = _run(
            [*database_args, "-Atc", "SELECT to_regclass('public.agent2_personal_weekly_briefs') IS NOT NULL"]
        ).stdout.strip()
        if table_present != "t":
            raise AssertionError("migration table is missing")
        public_grants = _run(
            [
                *database_args,
                "-Atc",
                (
                    "SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE table_schema='public' "
                    "AND table_name='agent2_personal_weekly_briefs' "
                    "AND grantee='PUBLIC'"
                ),
            ]
        ).stdout.strip()
        if public_grants != "0":
            raise AssertionError("migration granted table access to PUBLIC")
        checks["public_table_privileges_absent"] = "PASS"

        _run(database_args, input_text=_transactional(rollback_text))
        empty_rollback_absent = _run(
            [
                *database_args,
                "-Atc",
                "SELECT to_regclass('public.agent2_personal_weekly_briefs') IS NULL",
            ]
        ).stdout.strip()
        if empty_rollback_absent != "t":
            raise AssertionError("empty rollback did not remove the table")
        checks["empty_table_rollback"] = "PASS"
        print(json.dumps({"event": "postgres_empty_rollback_passed"}), flush=True)

        _run(database_args, input_text=_transactional(migration_text))
        apply_after_rollback_present = _run(
            [
                *database_args,
                "-Atc",
                "SELECT to_regclass('public.agent2_personal_weekly_briefs') IS NOT NULL",
            ]
        ).stdout.strip()
        if apply_after_rollback_present != "t":
            raise AssertionError("apply after rollback did not restore the table")
        checks["apply_after_rollback"] = "PASS"
        print(json.dumps({"event": "postgres_apply_after_rollback_passed"}), flush=True)

        brief_id = str(uuid4())
        insert_sql = f"""
        INSERT INTO agent2_personal_weekly_briefs (
            brief_id, tenant_id, owner_user_id, conversation_id,
            week_start, week_end, snapshot_at, source_snapshot,
            source_fingerprint, personal_memory_json, content_json,
            message_text, llm_model, status, idempotency_key,
            claim_token, provider_message_id, delivery_receipt_json,
            last_error, retry_count, created_at, updated_at
        ) VALUES (
            '{brief_id}', 'tenant-isolated', 'owner-isolated', 'conversation-isolated',
            DATE '2026-08-17', DATE '2026-08-21', TIMESTAMPTZ '2026-08-22 09:00:00+08',
            '{{"sources":[]}}'::jsonb, repeat('a',64), '{{}}'::jsonb, '{{}}'::jsonb,
            '', '', 'snapshot_ready', 'isolated-owner-week', '', '', '{{}}'::jsonb,
            '', 0, now(), now()
        );
        """
        _run(database_args, input_text=insert_sql)
        refused_rollback = _run(
            database_args,
            input_text=_transactional(rollback_text),
            check=False,
        )
        if refused_rollback.returncode == 0:
            raise AssertionError("rollback did not refuse a nonempty table")
        preserved_after_refusal = _run(
            [
                *database_args,
                "-Atc",
                (
                    "SELECT count(*) FROM agent2_personal_weekly_briefs "
                    f"WHERE brief_id='{brief_id}'"
                ),
            ]
        ).stdout.strip()
        if preserved_after_refusal != "1":
            raise AssertionError("nonempty rollback did not preserve the record")
        checks["nonempty_rollback_refused"] = "PASS"
        checks["backup_required_before_nonempty_rollback"] = "PASS"
        print(json.dumps({"event": "postgres_nonempty_rollback_refused"}), flush=True)
        duplicate = _run(
            database_args,
            input_text=insert_sql.replace(brief_id, str(uuid4()), 1),
            check=False,
        )
        if duplicate.returncode == 0:
            raise AssertionError("owner/week duplicate guard did not reject a duplicate")
        checks["owner_week_duplicate_guard"] = "PASS"
        print(json.dumps({"event": "postgres_duplicate_guard_passed"}), flush=True)
        transitions = f"""
        UPDATE agent2_personal_weekly_briefs
        SET status='generating', generation_started_at=now(),
            recovery_json=recovery_json || '[{{"kind":"generation_started"}}]'::jsonb,
            updated_at=now()
        WHERE brief_id='{brief_id}' AND status='snapshot_ready';
        UPDATE agent2_personal_weekly_briefs
        SET status='generated', content_json='{{"trace":{{}}}}'::jsonb,
            message_text='脱敏简报', llm_model='deepseek-v4-flash',
            generated_at=now(),
            recovery_json=recovery_json || '[{{"kind":"generation_completed"}}]'::jsonb,
            updated_at=now()
        WHERE brief_id='{brief_id}' AND status='generating';
        UPDATE agent2_personal_weekly_briefs
        SET status='claimed', claim_token='isolated-claim', send_started_at=now(),
            recovery_json=recovery_json || '[{{"kind":"send_claimed"}}]'::jsonb,
            updated_at=now()
        WHERE brief_id='{brief_id}' AND status='generated';
        UPDATE agent2_personal_weekly_briefs
        SET status='delivery_pending', provider_message_id='isolated-provider',
            provider_accepted_at=now(),
            recovery_json=recovery_json || '[{{"kind":"provider_accepted"}}]'::jsonb,
            updated_at=now()
        WHERE brief_id='{brief_id}' AND status='claimed';
        UPDATE agent2_personal_weekly_briefs
        SET status='delivered', delivered_at=now(), final_verified_at=now(),
            delivery_receipt_json='{{"delivery_verified":true,"delivered_dingtalk_user_ids":["ding-isolated"]}}'::jsonb,
            recovery_json=recovery_json || '[{{"kind":"delivery_verified"}}]'::jsonb,
            updated_at=now()
        WHERE brief_id='{brief_id}' AND status='delivery_pending';
        """
        _run(database_args, input_text=transitions)
        delivered = _run(
            [
                *database_args,
                "-Atc",
                (
                    "SELECT status || '|' || "
                    "(delivery_receipt_json->>'delivery_verified') || '|' || "
                    "jsonb_array_length(recovery_json)::text || '|' || "
                    "((generation_started_at IS NOT NULL AND generated_at IS NOT NULL "
                    "AND send_started_at IS NOT NULL AND final_verified_at IS NOT NULL)::text) "
                    "FROM agent2_personal_weekly_briefs "
                    f"WHERE brief_id='{brief_id}'"
                ),
            ]
        ).stdout.strip()
        if delivered != "delivered|true|5|true":
            raise AssertionError("delivery transition was not persisted")
        checks["snapshot_to_delivered_transitions"] = "PASS"
        print(json.dumps({"event": "postgres_transitions_passed"}), flush=True)
        _run(
            database_args,
            input_text=(
                "DELETE FROM agent2_personal_weekly_briefs "
                f"WHERE brief_id='{brief_id}';"
            ),
        )
        _run(database_args, input_text=_transactional(rollback_text))
        final_table_absent = _run(
            [
                *database_args,
                "-Atc",
                "SELECT to_regclass('public.agent2_personal_weekly_briefs') IS NULL",
            ]
        ).stdout.strip()
        if final_table_absent != "t":
            raise AssertionError("final empty rollback left the table behind")
        checks["final_empty_rollback_zero_residual"] = "PASS"
        print(json.dumps({"event": "postgres_final_rollback_passed"}), flush=True)
        _run([dropdb, *common, database_name])
        residual_database = _run(
            [
                psql,
                *common,
                "-d",
                "postgres",
                "-Atc",
                f"SELECT count(*) FROM pg_database WHERE datname='{database_name}'",
            ]
        ).stdout.strip()
        if residual_database != "0":
            raise AssertionError("isolated verification database was not removed")
        checks["database_cleanup_zero_residual"] = "PASS"
        print(json.dumps({"event": "postgres_database_cleanup_passed"}), flush=True)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if server_started and bin_dir is not None:
            _run_without_captured_handles(
                [
                    str(bin_dir / "pg_ctl.exe"),
                    "-D",
                    str(data),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                check=False,
            )
            print(json.dumps({"event": "postgres_server_stop_attempted"}), flush=True)
        print(json.dumps({"event": "postgres_temp_cleanup_started"}), flush=True)
        shutil.rmtree(temp_root, ignore_errors=False)
        print(json.dumps({"event": "postgres_temp_cleanup_completed"}), flush=True)

    temp_removed = not temp_root.exists()
    checks["temporary_directory_removed"] = "PASS" if temp_removed else "FAIL"
    status = "PASS" if not failure and temp_removed else "FAIL"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "database_target": "ephemeral_local_postgresql_only",
        "listen_address": "127.0.0.1",
        "production_database_connections": 0,
        "dingtalk_messages_sent": 0,
        "failure": failure,
        "checks": checks,
    }
    _write_json_atomically(args.output, payload)
    print(
        json.dumps(
            {
                "status": status,
                "postgresql_version": checks.get("postgresql_version", ""),
                "temporary_directory_removed": temp_removed,
                "output": str(args.output),
            }
        )
    )
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
