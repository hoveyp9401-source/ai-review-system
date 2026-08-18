from __future__ import annotations

from typing import Any

from app.agent2.tool_calling.deepseek_adapter import _CompletionResponse


def is_focused_daily_plan_request(
    tool_schemas: list[dict[str, Any]],
) -> bool:
    return [
        item.get("function", {}).get("name")
        for item in tool_schemas
        if isinstance(item, dict)
    ] == ["plan_daily_report"]


def focused_daily_fallback_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": '{"decision":"not_daily"}',
        },
        metadata={"finish_reason": "stop"},
    )


def focused_daily_fallback_response_body(*, model: str) -> dict[str, Any]:
    return {
        "id": "focused-daily-generic-path-test",
        "model": model,
        "created": 1,
        "choices": [
            {
                "finish_reason": "stop",
                "message": focused_daily_fallback_completion().message,
            }
        ],
        "usage": {},
    }
