from scripts.audit_agent2_production_safety import _csv_values, _json_count, _scope_hash


def test_scope_hash_is_stable_and_does_not_disclose_raw_identity() -> None:
    raw = "sandbox-agent2-phase2-20260711"

    first = _scope_hash(raw)

    assert first == _scope_hash(raw)
    assert len(first) == 16
    assert raw not in first


def test_config_scope_parser_is_deterministic_and_deduplicated() -> None:
    assert _csv_values("tenant-a,tenant-b;tenant-a\ntenant-c") == (
        "tenant-a",
        "tenant-b",
        "tenant-c",
    )


def test_permission_scope_summary_counts_only_explicit_lists() -> None:
    assert _json_count({"writable_case_ids": ["case-1", "case-2"]}, "writable_case_ids") == 2
    assert _json_count({"writable_case_ids": "*"}, "writable_case_ids") == 0
    assert _json_count(None, "writable_case_ids") == 0
