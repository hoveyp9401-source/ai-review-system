from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.probe_personal_weekly_brief_flash_transport import (
    _TokenCappedClient,
    _error_payload,
    _write_private_json,
)
from scripts.run_agent2_personal_weekly_brief_model_eval import (
    _assert_partial_without_plan,
)


class _RecordingClient:
    def __init__(self) -> None:
        self.kwargs = None

    async def complete_json(self, **kwargs):
        self.kwargs = kwargs
        return "{}"


def test_probe_caps_weekly_tokens_without_changing_other_request_fields() -> None:
    base = _RecordingClient()
    client = _TokenCappedClient(base, max_tokens=2400)  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete_json(
            system_prompt="system",
            user_prompt="user",
            max_tokens=8000,
            thinking_enabled=True,
        )
    )

    assert result == "{}"
    assert base.kwargs == {
        "system_prompt": "system",
        "user_prompt": "user",
        "max_tokens": 2400,
        "thinking_enabled": True,
    }


def test_probe_classifies_remote_disconnect_without_copying_error_text() -> None:
    error = RuntimeError("Server disconnected without sending a response")

    payload = _error_payload(error)

    assert payload["error_chain"][0]["category"] == (
        "server_disconnected_before_response"
    )
    assert "Server disconnected" not in json.dumps(payload)


def test_probe_output_is_private_atomic_and_not_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "probe.json"
    payload = {"status": "PASS", "results": []}

    _write_private_json(output, payload)

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        _write_private_json(output, payload)


def test_partial_date_oracle_accepts_natural_equivalent_open_loop_wording() -> None:
    sources = ("daily:partial:mon", "daily:partial:thu")
    content = SimpleNamespace(
        completed=SimpleNamespace(
            items=(SimpleNamespace(source_ids=sources),)
        ),
        plan_progress=SimpleNamespace(items=()),
        possible_open_loops=SimpleNamespace(
            items=(
                SimpleNamespace(
                    source_ids=("daily:partial:thu",),
                    text="付款条件尚未获得业务部门最终确认。",
                ),
            )
        ),
        message_text="付款条件尚未获得业务部门最终确认。",
    )

    assert _assert_partial_without_plan(content)["plan_items"] == 0
