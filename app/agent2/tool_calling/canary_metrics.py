from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.registry import TOOL_REGISTRY


class CanaryMetricEvent(BaseModel):
    """Aggregate-only Canary telemetry; business text and object IDs are absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_names: tuple[str, ...] = Field(default=(), max_length=50)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    clarification_count: int = Field(ge=0)
    receipt_mismatch_count: int = Field(ge=0)
    rollback_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    model_error_count: int = Field(ge=0)

    @field_validator("tool_names")
    @classmethod
    def tools_must_come_from_registry(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        unavailable = {
            name
            for name in value
            if name not in TOOL_REGISTRY
            or ExecutionMode.CANARY_EXECUTE
            not in TOOL_REGISTRY[name].enabled_modes
        }
        if unavailable:
            raise ValueError(
                f"tool unavailable in Canary: {sorted(unavailable)[0]}"
            )
        return value

    def safe_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "agent2.tool_call_canary.metric.v1",
            "tool_names": list(self.tool_names),
            "tool_call_count": len(self.tool_names),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "clarification_count": self.clarification_count,
            "receipt_mismatch_count": self.receipt_mismatch_count,
            "rollback_count": self.rollback_count,
            "latency_ms": self.latency_ms,
            "model_error_count": self.model_error_count,
        }


class CanaryMetricsRecorder:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(
            "agent2.tool_calling.canary_metrics"
        )

    def record(self, event: CanaryMetricEvent) -> None:
        self._logger.info(
            "agent2_tool_call_canary_metric %s",
            json.dumps(
                event.safe_payload(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
