from __future__ import annotations

from collections.abc import Mapping
from typing import Any


FORBIDDEN_ORACLE_FIELDS = frozenset(
    {
        "expected",
        "baseline",
        "gold",
        "reference_decision",
        "expected_write_intent",
        "expected_domain",
        "expected_command",
        "historical_actual_output",
        "closure_result",
        "root_cause_annotation",
        "score",
        "reviewer_conclusion",
        "risk_annotation",
        "provenance",
        "independent_review_status",
        "adjudication",
        "label",
        "labels",
        "seal_hash",
        "sealed_label_hash",
    }
)


def assert_no_oracle_fields(value: Any, *, path: str) -> None:
    """Reject label/oracle keys before data crosses the cognitive seam.

    The check is recursive and key-based. User text may discuss words such as
    "expected"; only structured fields can become an oracle side channel.
    """

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized.startswith("expected_") or normalized in FORBIDDEN_ORACLE_FIELDS:
                raise ValueError(f"forbidden oracle field at {path}.{key}")
            assert_no_oracle_fields(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            assert_no_oracle_fields(nested, path=f"{path}[{index}]")
