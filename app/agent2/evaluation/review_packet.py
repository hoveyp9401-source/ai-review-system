from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agent2.json_immutability import thaw_json_value
from app.agent2.runtime.blind import BlindInputPack

from .runtime_scoring import SealedLabelStore


def build_reviewer_packet(
    blind_pack: BlindInputPack,
    labels: SealedLabelStore,
) -> dict[str, Any]:
    blind_pack = BlindInputPack.from_mapping(blind_pack.as_mapping())
    labels = SealedLabelStore.from_mapping(labels.as_mapping())
    if blind_pack.pack_id != labels.input_pack_id:
        raise ValueError("review packet inputs have different pack IDs")
    if blind_pack.digest != labels.input_pack_digest:
        raise ValueError("review packet inputs have different pack digests")
    turns: dict[tuple[str, str], tuple[Any, list[dict[str, str]]]] = {}
    for case in blind_pack.cases:
        prior: list[dict[str, str]] = []
        for turn in case.turns:
            turns[(case.case_id, turn.turn_id)] = (turn, list(prior[-3:]))
            prior.append({"turn_id": turn.turn_id, "role": "user", "text": turn.raw_text})
    records: list[dict[str, Any]] = []
    for label in labels.labels:
        key = (str(label["case_id"]), str(label["turn_id"]))
        found = turns.get(key)
        if found is None:
            raise ValueError(f"review label has no blind input: {key[0]}/{key[1]}")
        turn, prior = found
        candidate = thaw_json_value({
            key_name: value
            for key_name, value in dict(label).items()
            if key_name not in {"case_id", "turn_id", "independent_review_status"}
        })
        records.append(
            {
                "schema_version": "agent2.semantic_review_record.v1",
                "case_id": key[0],
                "turn_id": key[1],
                "input_text": turn.raw_text,
                "relevant_prior_turns": prior,
                "machine_proposed_label": candidate,
                "review_priority": _review_priority(candidate),
                "independent_review_status": "pending",
                "adjudication": None,
                "disagreement_status": "unreviewed",
            }
        )
    body = {
        "schema_version": "agent2.semantic_reviewer_packet.v1",
        "input_pack_id": blind_pack.pack_id,
        "input_pack_digest": blind_pack.digest,
        "sealed_label_hash": labels.seal_hash,
        "record_count": len(records),
        "human_approved_count": 0,
        "records": records,
    }
    return {**body, "packet_hash": _json_digest(body)}


def _review_priority(candidate: dict[str, Any]) -> str:
    risk = str(candidate.get("risk_annotation") or "").lower()
    if "critical" in risk or candidate.get("expected_write_intent") is True:
        return "P0"
    if "high" in risk or candidate.get("expected_clarification_requirement") is True:
        return "P1"
    return "P2"


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
