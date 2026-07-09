from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent2.harness.schemas import HarnessCase


@dataclass(frozen=True)
class LegacyProbeResult:
    mode: str = "not_implemented"
    write_impact: bool | None = None
    changed_fields: list[str] = field(default_factory=list)
    reason: str = "legacy dry-run is intentionally disabled in the first harness version"
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "write_impact": self.write_impact,
            "changed_fields": list(self.changed_fields),
            "reason": self.reason,
            "raw": dict(self.raw),
        }


def probe_legacy(case: HarnessCase) -> LegacyProbeResult:
    _ = case
    return LegacyProbeResult()
