from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from scripts.run_agent2_isolated_api_smoke import (
    ADVERSE_CASE_COVERAGE,
    _independent_daily_snapshot_review,
    _load_adverse_inputs,
    _sanitized_exception,
    _SmokeCheckError,
    _write_private_json,
)


def test_isolated_api_smoke_failure_does_not_store_report_text() -> None:
    secret = "一整段真实日报正文与金额123456"

    payload = _sanitized_exception(AssertionError(secret))

    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert payload["error_type"] == "AssertionError"
    assert len(payload["error_sha256"]) == 64

    coded = _sanitized_exception(
        _SmokeCheckError("daily_snapshot_review_rejected")
    )
    assert coded["error_code"] == "daily_snapshot_review_rejected"


def test_isolated_api_smoke_output_is_owner_only(tmp_path) -> None:
    output = tmp_path / "smoke.json"

    _write_private_json(output, {"status": "pass"})

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "pass"
    }
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_isolated_api_smoke_requires_all_semantic_review_dimensions() -> None:
    class _Client:
        async def complete_json(self, **kwargs):
            assert kwargs["thinking_enabled"] is True
            return json.dumps(
                {
                    "faithful": True,
                    "complete": True,
                    "correct_fields": True,
                    "partitioned": True,
                    "no_invention": True,
                    "reason_code": "ok",
                }
            )

    decision = await _independent_daily_snapshot_review(
        _Client(),
        source_messages=("今天完成合同复核",),
        stored_fields={
            "today_work": ["完成合同复核"],
            "problems": [],
            "tomorrow_plan": [],
        },
    )

    assert decision["reason_code"] == "ok"


def test_adverse_input_loader_keeps_non_text_failures_visible(tmp_path) -> None:
    replayable = hashlib.sha256("日报输入".encode("utf-8")).hexdigest()
    artifact = tmp_path / "adverse.json"
    artifact.write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "input_sha256": replayable,
                        "message_text": "日报输入",
                        "replayable_text": True,
                    },
                    {
                        "input_sha256": "f" * 64,
                        "replayable_text": False,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    inputs, evidence = _load_adverse_inputs(artifact)

    assert inputs == {replayable: "日报输入"}
    assert evidence["artifact_unique_inputs"] == 2
    assert len(evidence["artifact_sha256"]) == 64
    assert evidence["non_replayable_input_sha256"] == ["f" * 64]
