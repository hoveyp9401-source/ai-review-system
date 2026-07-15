from __future__ import annotations

import hashlib
import json
import os
import threading
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any


TENANT_COLLECTIONS = (
    "companies",
    "departments",
    "teams",
    "users",
    "roles",
    "sources",
    "metric_definitions",
    "cases",
    "daily_submissions",
    "weekly_submissions",
    "monthly_submissions",
    "performance_metrics",
    "target_collections",
    "report_runs",
    "manual_metric_entries",
    "travels",
    "quality_issues",
)


class SandboxRepository:
    """Atomic JSON repository used only by the explicitly enabled sandbox."""

    def __init__(self, path: str | Path, *, sandbox_enabled: bool):
        self.path = Path(path)
        self.sandbox_enabled = bool(sandbox_enabled)
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None

    def reset(self, seed: dict[str, Any]) -> dict[str, Any]:
        if not self.sandbox_enabled:
            raise PermissionError("seed/reset is sandbox-only")
        normalized = _normalize_seed(seed)
        with self._lock:
            with self._process_lock():
                return self._write_snapshot_locked(normalized)

    def reset_tenant(self, seed: dict[str, Any], tenant_id: str) -> dict[str, Any]:
        if not self.sandbox_enabled:
            raise PermissionError("seed/reset is sandbox-only")
        normalized_seed = _normalize_seed(seed)
        if tenant_id not in {item["id"] for item in normalized_seed["tenants"]}:
            raise LookupError("tenant not found in sandbox seed")
        if not self.path.exists():
            raise FileNotFoundError("sandbox must be seeded before tenant reset")
        with self._lock:
            with self._process_lock():
                current = _normalize_seed(json.loads(self.path.read_text(encoding="utf-8")))
                current["metadata"] = deepcopy(normalized_seed["metadata"])
                current["tenants"] = _replace_partition(
                    current["tenants"],
                    normalized_seed["tenants"],
                    tenant_id=tenant_id,
                    tenant_key="id",
                )
                for collection in TENANT_COLLECTIONS:
                    current[collection] = _replace_partition(
                        current[collection],
                        normalized_seed[collection],
                        tenant_id=tenant_id,
                        tenant_key="tenant_id",
                    )
                result = self._write_snapshot_locked(current)
        return {**result, "tenant_id": tenant_id, "scope": "tenant_only"}

    def _write_snapshot_locked(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        encoded = _stable_json(snapshot)
        content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(encoded + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        self._snapshot = deepcopy(snapshot)
        return {
            "seed_id": snapshot["metadata"]["seed_id"],
            "content_hash": content_hash,
            "record_counts": _record_counts(snapshot),
            "fixture": True,
        }

    @contextmanager
    def _process_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is not None:
                return deepcopy(self._snapshot)
            if not self.path.exists():
                raise FileNotFoundError(f"legal ops sandbox is not seeded: {self.path}")
            snapshot = json.loads(self.path.read_text(encoding="utf-8"))
            self._snapshot = _normalize_seed(snapshot)
            return deepcopy(self._snapshot)

    def tenant_snapshot(self, tenant_id: str) -> dict[str, Any]:
        snapshot = self.load()
        tenant = next((item for item in snapshot["tenants"] if item["id"] == tenant_id), None)
        if tenant is None:
            raise LookupError("tenant not found in authorized sandbox")
        scoped: dict[str, Any] = {
            "metadata": deepcopy(snapshot["metadata"]),
            "tenant": deepcopy(tenant),
        }
        for collection in TENANT_COLLECTIONS:
            scoped[collection] = [
                deepcopy(item) for item in snapshot.get(collection, []) if item.get("tenant_id") == tenant_id
            ]
        return scoped

    def verify_tenant(self, tenant_id: str) -> dict[str, Any]:
        scoped = self.tenant_snapshot(tenant_id)
        violations: list[str] = []
        ids: set[str] = {scoped["tenant"]["id"]}
        for collection in TENANT_COLLECTIONS:
            for item in scoped[collection]:
                if item.get("tenant_id") != tenant_id:
                    violations.append(f"{collection}:{item.get('id')}:cross_tenant")
                item_id = str(item.get("id") or item.get("source_id") or "")
                if item_id:
                    ids.add(item_id)
                if collection == "cases":
                    for node in item.get("lifecycle", {}).get("nodes", []):
                        if node.get("tenant_id") != tenant_id:
                            violations.append(f"case_node:{node.get('id')}:cross_tenant")
                        source_id = node.get("origin", {}).get("source_id")
                        if source_id and source_id not in {source["id"] for source in scoped["sources"]}:
                            violations.append(f"case_node:{node.get('id')}:missing_source:{source_id}")
        return {
            "tenant_id": tenant_id,
            "valid": not violations,
            "violations": violations,
            "record_counts": {key: len(scoped[key]) for key in TENANT_COLLECTIONS},
        }


def _normalize_seed(seed: dict[str, Any]) -> dict[str, Any]:
    snapshot = deepcopy(seed)
    metadata = snapshot.setdefault("metadata", {})
    if not metadata.get("fixture"):
        raise ValueError("sandbox seed must be explicitly marked as fixture")
    if not metadata.get("seed_id"):
        raise ValueError("sandbox seed_id is required")
    snapshot.setdefault("tenants", [])
    for collection in TENANT_COLLECTIONS:
        snapshot.setdefault(collection, [])
    return snapshot


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _record_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    return {"tenants": len(snapshot["tenants"]), **{key: len(snapshot[key]) for key in TENANT_COLLECTIONS}}


def _replace_partition(
    current: list[dict[str, Any]],
    seed: list[dict[str, Any]],
    *,
    tenant_id: str,
    tenant_key: str,
) -> list[dict[str, Any]]:
    first_target_index = next(
        (index for index, item in enumerate(current) if item.get(tenant_key) == tenant_id),
        len(current),
    )
    retained = [deepcopy(item) for item in current if item.get(tenant_key) != tenant_id]
    insert_at = min(first_target_index, len(retained))
    replacement = [deepcopy(item) for item in seed if item.get(tenant_key) == tenant_id]
    return [*retained[:insert_at], *replacement, *retained[insert_at:]]
