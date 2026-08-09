from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

SnapshotAction = Literal["added", "missing", "unchanged", "changed"]


class InvalidCaseSnapshot(ValueError):
    pass


@dataclass(frozen=True)
class SnapshotFieldGroups:
    progress: frozenset[str] = frozenset()
    plan: frozenset[str] = frozenset()
    lifecycle: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        overlap = (
            (self.progress & self.plan)
            | (self.progress & self.lifecycle)
            | (self.plan & self.lifecycle)
        )
        if overlap:
            fields = ", ".join(sorted(overlap))
            raise InvalidCaseSnapshot(
                f"snapshot fields cannot belong to multiple groups: {fields}"
            )


@dataclass(frozen=True)
class SnapshotFieldChange:
    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class SnapshotRecordChange:
    source_case_id: str
    action: SnapshotAction
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    master_changes: tuple[SnapshotFieldChange, ...] = ()
    progress_changes: tuple[SnapshotFieldChange, ...] = ()
    plan_changes: tuple[SnapshotFieldChange, ...] = ()
    lifecycle_changes: tuple[SnapshotFieldChange, ...] = ()


@dataclass(frozen=True)
class CaseSnapshotDiff:
    added: tuple[SnapshotRecordChange, ...]
    missing: tuple[SnapshotRecordChange, ...]
    unchanged: tuple[SnapshotRecordChange, ...]
    changed: tuple[SnapshotRecordChange, ...]
    timeline_candidates: tuple[SnapshotRecordChange, ...] = ()
    deletes: tuple[str, ...] = ()


_PROGRESS_FIELDS = frozenset({"content", "summary", "details"})
_PLAN_FIELDS = frozenset({"next_plan", "plan_date"})
_LIFECYCLE_FIELDS = frozenset({"status", "procedure_node", "lifecycle_stage"})
_IDENTITY_FIELDS = frozenset({"source_case_id"})
_SNAPSHOT_METADATA_FIELDS = frozenset(
    {
        "__raw_snapshot__",
        "__row_number__",
        "created_at",
        "erp_updated_at",
        "external_progress_id",
        "file_hash",
        "progress_date",
        "snapshot_date",
        "source_batch_id",
        "source_file",
        "source_header_hash",
        "source_profile_key",
        "source_profile_version",
        "source_row_number",
        "source_sheet",
        "source_updated_at",
        "updated_at",
        "uploaded_at",
    }
)
_DEFAULT_FIELD_GROUPS = SnapshotFieldGroups(
    progress=_PROGRESS_FIELDS,
    plan=_PLAN_FIELDS,
    lifecycle=_LIFECYCLE_FIELDS,
)


def compare_case_snapshots(
    previous: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    *,
    field_groups: SnapshotFieldGroups | None = None,
) -> CaseSnapshotDiff:
    """Compare two complete source snapshots without producing write actions.

    Records are addressed only by the mapping key ``source_case_id``.  A
    missing current record is returned for review and never appears in
    ``deletes``.  Optional field groups extend the canonical field groups used
    to identify progress, plan, and lifecycle changes in original ERP columns.
    """

    groups = _resolved_field_groups(field_groups)
    previous_by_id = _validated_snapshot(previous, label="previous")
    current_by_id = _validated_snapshot(current, label="current")
    added: list[SnapshotRecordChange] = []
    missing: list[SnapshotRecordChange] = []
    unchanged: list[SnapshotRecordChange] = []
    changed: list[SnapshotRecordChange] = []
    timeline_candidates: list[SnapshotRecordChange] = []

    all_case_ids = sorted(set(previous_by_id) | set(current_by_id))
    for source_case_id in all_case_ids:
        before = previous_by_id.get(source_case_id)
        after = current_by_id.get(source_case_id)
        before_copy = dict(before) if before is not None else None
        after_copy = dict(after) if after is not None else None
        if before is None:
            added.append(
                SnapshotRecordChange(
                    source_case_id,
                    "added",
                    None,
                    after_copy,
                )
            )
        elif after is None:
            missing.append(
                SnapshotRecordChange(
                    source_case_id,
                    "missing",
                    before_copy,
                    None,
                )
            )
        else:
            field_changes = _field_changes(before, after)
            if not field_changes:
                unchanged.append(
                    SnapshotRecordChange(
                        source_case_id,
                        "unchanged",
                        before_copy,
                        after_copy,
                    )
                )
                continue
            master_changes = tuple(
                item
                for item in field_changes
                if _category(item.field, groups) == "master"
            )
            progress_changes = tuple(
                item
                for item in field_changes
                if _category(item.field, groups) == "progress"
            )
            plan_changes = tuple(
                item for item in field_changes if _category(item.field, groups) == "plan"
            )
            lifecycle_changes = tuple(
                item
                for item in field_changes
                if _category(item.field, groups) == "lifecycle"
            )
            change = SnapshotRecordChange(
                source_case_id,
                "changed",
                before_copy,
                after_copy,
                master_changes,
                progress_changes,
                plan_changes,
                lifecycle_changes,
            )
            changed.append(change)
            timeline_changes = (
                progress_changes + plan_changes + lifecycle_changes
            )
            if any(
                _normalized_value(item.after) is not None
                for item in timeline_changes
            ):
                timeline_candidates.append(change)

    return CaseSnapshotDiff(
        added=tuple(added),
        missing=tuple(missing),
        unchanged=tuple(unchanged),
        changed=tuple(changed),
        timeline_candidates=tuple(timeline_candidates),
    )


def _resolved_field_groups(
    custom: SnapshotFieldGroups | None,
) -> SnapshotFieldGroups:
    if custom is None:
        return _DEFAULT_FIELD_GROUPS
    return SnapshotFieldGroups(
        progress=_DEFAULT_FIELD_GROUPS.progress | custom.progress,
        plan=_DEFAULT_FIELD_GROUPS.plan | custom.plan,
        lifecycle=_DEFAULT_FIELD_GROUPS.lifecycle | custom.lifecycle,
    )


def _validated_snapshot(
    snapshot: Mapping[str, Mapping[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw_case_id, record in snapshot.items():
        source_case_id = str(raw_case_id or "").strip()
        if not source_case_id:
            raise InvalidCaseSnapshot(f"{label} snapshot has a blank source_case_id")
        if source_case_id in indexed:
            raise InvalidCaseSnapshot(
                f"{label} snapshot has duplicate source_case_id: {source_case_id}"
            )
        record_copy = dict(record)
        embedded_id = str(record_copy.get("source_case_id") or "").strip()
        if embedded_id and embedded_id != source_case_id:
            raise InvalidCaseSnapshot(
                f"{label} snapshot source_case_id mismatch for {source_case_id}"
            )
        indexed[source_case_id] = record_copy
    return indexed


def _field_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> tuple[SnapshotFieldChange, ...]:
    changes = []
    excluded = _IDENTITY_FIELDS | _SNAPSHOT_METADATA_FIELDS
    for field in sorted((set(before) | set(after)) - excluded):
        old_value = before.get(field)
        new_value = after.get(field)
        if _normalized_value(old_value) != _normalized_value(new_value):
            changes.append(SnapshotFieldChange(field, old_value, new_value))
    return tuple(changes)


def _category(field: str, groups: SnapshotFieldGroups) -> str:
    if field in groups.progress:
        return "progress"
    if field in groups.plan:
        return "plan"
    if field in groups.lifecycle:
        return "lifecycle"
    return "master"


def _normalized_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        collapsed = re.sub(r"\s+", " ", value).strip()
        return collapsed or None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _normalized_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_normalized_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        normalized = (_normalized_value(item) for item in value)
        return tuple(sorted(normalized, key=repr))
    return value
