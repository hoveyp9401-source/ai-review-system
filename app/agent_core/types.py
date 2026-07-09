from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DailySnapshot:
    today_work: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    tomorrow_plan: list[str] = field(default_factory=list)
    status: str = "collecting"
    section_status: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "today_work": list(self.today_work),
            "problems": list(self.problems),
            "tomorrow_plan": list(self.tomorrow_plan),
            "status": self.status,
        }


@dataclass(frozen=True)
class OperationLedgerEntry:
    operation_id: str
    workflow: str
    capability: str
    operation: str
    write_policy: str
    plan_id: str = ""
    authorization_id: str = ""
    authorization_status: str = ""
    before_state: dict[str, Any] = field(default_factory=dict)
    after_state: dict[str, Any] = field(default_factory=dict)
    changed: bool = False
    read_only: bool = False
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "workflow": self.workflow,
            "capability": self.capability,
            "operation": self.operation,
            "write_policy": self.write_policy,
            "plan_id": self.plan_id,
            "authorization_id": self.authorization_id,
            "authorization_status": self.authorization_status,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "changed": self.changed,
            "read_only": self.read_only,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
        }
