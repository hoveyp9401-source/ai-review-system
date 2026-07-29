from __future__ import annotations

from typing import Any, Mapping

from app.agent2.tool_calling.context import SHADOW_STATE_NAMESPACE
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.replay_contracts import BlindReplayPack, canonical_digest


SIDE_EFFECT_SNAPSHOT_SCHEMA = "agent2.tool_call_shadow_side_effect_snapshot.v2"
_SNAPSHOT_FIELDS = {
    "schema_version",
    "input_pack_id",
    "input_pack_digest",
    "replay_run_id",
    "capture_phase",
    "shadow_artifact_hash",
    "business_table_fingerprint",
    "pending_count",
    "conversation_state_fingerprint",
    "message_send_count",
}


def build_no_go_evidence(
    *,
    pack: BlindReplayPack,
    shadow: Mapping[str, Any],
    human_sealed_gold_available: bool,
    legacy_actual_available: bool,
    side_effect_before: Mapping[str, Any] | None = None,
    side_effect_after: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    if not human_sealed_gold_available:
        blockers.append("missing_human_sealed_tool_call_gold")
    if not legacy_actual_available:
        blockers.append("missing_legacy_actual_for_same_blind_pack")
    snapshot_result = compare_side_effect_snapshots(
        side_effect_before,
        side_effect_after,
        input_pack_id=pack.pack_id,
        input_pack_digest=pack.digest,
        shadow_artifact_hash=str(shadow["artifact_hash"]),
    )
    if not snapshot_result["verified"]:
        blockers.append("business_state_before_after_unverified")
    elif snapshot_result["changed_fields"]:
        blockers.append("business_state_before_after_changed")

    cases = list(shadow.get("cases") or [])
    side_effect_totals = {
        key: sum(int((case.get("side_effects") or {}).get(key) or 0) for case in cases)
        for key in (
            "business_write_count",
            "business_handler_call_count",
            "pending_write_count",
            "conversation_state_write_count",
            "message_send_count",
            "duplicate_side_effect_count",
        )
    }
    p0_counts = _observed_p0_counts(cases, side_effect_totals, snapshot_result)
    unverified_p0 = [] if human_sealed_gold_available else [
        "wrong_report",
        "wrong_item",
        "wrong_write_date",
        "forged_id_accepted",
    ]
    return {
        "schema_version": "agent2.tool_call_shadow_phase1_evidence.v1",
        "input_pack_id": pack.pack_id,
        "input_pack_digest": pack.digest,
        "shadow_artifact_hash": shadow["artifact_hash"],
        "sample_count": len(cases),
        "model_call_count": sum(int(item.get("model_call_count") or 0) for item in cases),
        "failed_cases": [
            {
                "case_id": item["case_id"],
                "error_type": item.get("error_type"),
                "error_message": item.get("error_message"),
            }
            for item in cases
            if item.get("status") == "failed"
        ],
        "side_effect_totals": side_effect_totals,
        "business_state_before_after": snapshot_result,
        "p0": {
            "observed_counts": p0_counts,
            "observed_total": sum(p0_counts.values()),
            "unverified": unverified_p0,
        },
        "human_sealed_gold_available": human_sealed_gold_available,
        "legacy_actual_available": legacy_actual_available,
        "blockers": blockers,
        "verdict": "NO_GO",
    }


def compare_side_effect_snapshots(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    input_pack_id: str,
    input_pack_digest: str,
    shadow_artifact_hash: str,
) -> dict[str, Any]:
    if before is None or after is None:
        return {
            "verified": False,
            "before_digest": None,
            "after_digest": None,
            "changed_fields": [],
            "binding_errors": ["snapshots_missing"],
        }
    _validate_snapshot(before)
    _validate_snapshot(after)
    binding_errors: list[str] = []
    if before["capture_phase"] != "before" or after["capture_phase"] != "after":
        binding_errors.append("capture_phase_mismatch")
    if before["replay_run_id"] != after["replay_run_id"]:
        binding_errors.append("replay_run_id_mismatch")
    for field, expected in (
        ("input_pack_id", input_pack_id),
        ("input_pack_digest", input_pack_digest),
    ):
        if before[field] != expected or after[field] != expected:
            binding_errors.append(f"{field}_mismatch")
    if after["shadow_artifact_hash"] != shadow_artifact_hash:
        binding_errors.append("shadow_artifact_hash_mismatch")
    if before["shadow_artifact_hash"] not in {None, shadow_artifact_hash}:
        binding_errors.append("before_artifact_hash_mismatch")
    comparable = {
        "business_table_fingerprint",
        "pending_count",
        "conversation_state_fingerprint",
        "message_send_count",
    }
    verified = not binding_errors
    return {
        "verified": verified,
        "before_digest": canonical_digest(before),
        "after_digest": canonical_digest(after),
        "changed_fields": (
            sorted(key for key in comparable if before[key] != after[key])
            if verified
            else []
        ),
        "binding_errors": binding_errors,
    }


def _validate_snapshot(value: Mapping[str, Any]) -> None:
    if set(value) != _SNAPSHOT_FIELDS or value.get("schema_version") != SIDE_EFFECT_SNAPSHOT_SCHEMA:
        raise ValueError("unsupported side-effect snapshot artifact")
    if any(
        not isinstance(value.get(field), int) or int(value[field]) < 0
        for field in ("pending_count", "message_send_count")
    ):
        raise ValueError("side-effect snapshot counts must be non-negative integers")
    if any(
        not isinstance(value.get(field), str) or not value[field]
        for field in (
            "input_pack_id",
            "input_pack_digest",
            "replay_run_id",
            "business_table_fingerprint",
            "conversation_state_fingerprint",
        )
    ):
        raise ValueError("side-effect snapshot bindings and fingerprints are required")
    if value.get("capture_phase") not in {"before", "after"}:
        raise ValueError("side-effect snapshot capture phase is invalid")
    artifact_hash = value.get("shadow_artifact_hash")
    if artifact_hash is not None and (not isinstance(artifact_hash, str) or not artifact_hash):
        raise ValueError("side-effect snapshot artifact hash is invalid")
    if value["capture_phase"] == "after" and artifact_hash is None:
        raise ValueError("after snapshot requires the Shadow artifact hash")


def _observed_p0_counts(
    cases: list[Mapping[str, Any]],
    side_effects: Mapping[str, int],
    snapshot: Mapping[str, Any],
) -> dict[str, int]:
    counts = {
        "wrong_user": sum(item.get("principal_scope_matches") is False for item in cases),
        "wrong_tenant": sum(item.get("tenant_scope_matches") is False for item in cases),
        "clear_pending_bypass": 0,
        "shadow_side_effect": sum(
            value for key, value in side_effects.items()
            if key != "duplicate_side_effect_count"
        ),
        "receipt_false_success": 0,
        "duplicate_side_effect": int(side_effects.get("duplicate_side_effect_count") or 0),
        "state_namespace_crossing": sum(
            item.get("namespace") != SHADOW_STATE_NAMESPACE for item in cases
        ),
        "business_state_changed": len(snapshot.get("changed_fields") or []),
    }
    for case in cases:
        calls = list(case.get("tool_calls") or [])
        receipts = list(case.get("receipts") or [])
        for call, receipt in zip(calls, receipts):
            definition = TOOL_REGISTRY.get(str(call.get("tool_name") or ""))
            if (
                definition is not None
                and definition.object_binding_policy
                == "unique_server_pending_full_scope_and_version"
                and not case.get("pending_present")
                and receipt.get("status") in {"success", "no_op"}
            ):
                counts["clear_pending_bypass"] += 1
        for receipt in receipts:
            facts = receipt.get("safe_user_facts") or {}
            if (
                receipt.get("changed") is True
                or facts.get("actual_write") is True
                or receipt.get("after_version") != receipt.get("before_version")
            ):
                counts["receipt_false_success"] += 1
    return counts
