from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import manage_formal_roster_70_4_20260822 as migration


def _rows() -> list[dict[str, object]]:
    return [
        {
            "membership_id": "membership-ding",
            "tenant_id": "tenant-formal",
            "user_id": "user-ding",
            "team_id": "team-2",
            "member_role": "team_lead",
            "effective_from": "2026-08-03",
            "effective_to": None,
            "source": "verified_org_correction",
            "data_complete": True,
            "user_name": "丁益明",
            "user_team_id": "team-2",
            "team_code": "monthly-law-2",
            "team_name": "法务二部",
        },
        {
            "membership_id": "membership-xue",
            "tenant_id": "tenant-formal",
            "user_id": "user-xue",
            "team_id": "team-4",
            "member_role": "team_lead",
            "effective_from": "2026-08-03",
            "effective_to": None,
            "source": "verified_org_correction",
            "data_complete": True,
            "user_name": "薛旭",
            "user_team_id": "team-4",
            "team_code": "monthly-law-4",
            "team_name": "法务四部",
        },
    ]


def _center_team() -> dict[str, object]:
    return {
        "team_id": "team-center",
        "code": "legal-center",
        "name": "法务合约中心（中心层级）",
        "department_name": "法务合约中心",
        "active": False,
    }


def test_roster_migration_keeps_membership_and_leadership_facts_separate() -> None:
    assert migration.TARGET_TEAMS == {
        "丁益明": "monthly-law-2",
        "薛旭": "monthly-law-4",
    }
    assert migration.CENTER_TEAM_CODE == "legal-center"
    assert migration.EFFECTIVE_DATE.isoformat() == "2026-08-22"


def test_pre_migration_contract_requires_exact_72_plus_2() -> None:
    migration._validate_before(
        counts={
            "total_count": 74,
            "child_count": 72,
            "center_count": 2,
            "target_center_count": 0,
        },
        rows=_rows(),
        center_team=_center_team(),
    )


def test_pre_migration_contract_rejects_drift() -> None:
    with pytest.raises(RuntimeError, match="unexpected pre-migration roster counts"):
        migration._validate_before(
            counts={
                "total_count": 74,
                "child_count": 71,
                "center_count": 3,
                "target_center_count": 1,
            },
            rows=_rows(),
            center_team=_center_team(),
        )


def test_private_backup_is_checksum_bound_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "roster-backup.json"
    unsigned = {
        "schema_version": migration.SCHEMA_VERSION,
        "migration_key": migration.MIGRATION_KEY,
        "memberships": _rows(),
    }
    payload = dict(unsigned)
    payload["payload_sha256"] = migration._digest(unsigned)
    migration._write_private_json(path, payload)

    assert migration._load_backup(path) == payload
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        migration._write_private_json(path, payload)


def test_private_backup_rejects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "roster-backup.json"
    unsigned = {
        "schema_version": migration.SCHEMA_VERSION,
        "migration_key": migration.MIGRATION_KEY,
        "memberships": _rows(),
    }
    payload = dict(unsigned)
    payload["payload_sha256"] = migration._digest(unsigned)
    migration._write_private_json(path, payload)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["memberships"][0]["team_id"] = "changed"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        migration._load_backup(path)
