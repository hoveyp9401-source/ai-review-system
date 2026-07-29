from __future__ import annotations

from dataclasses import dataclass

from app.agent2.business.contracts import (
    BusinessCommand,
    BusinessCommandError,
    CreateCaseProgress,
    CreateTravelIntent,
    DeleteCaseProgress,
    LinkCaseProgress,
    QueryCaseProgress,
    QueryPartyCases,
    RespondTravelCollaboration,
    SnoozeCaseFollowup,
    UpdateCaseProgress,
    UpdateTravelIntent,
)
from app.agent2.case_followup_commands import TriggerCaseFollowupNow, UpdateCaseFollowupPolicy


@dataclass(frozen=True)
class BusinessEffectPolicy:
    """Fail-closed domain/effect switches for the isolated Phase 2 route."""

    party_query_enabled: bool = True
    case_progress_enabled: bool = True
    case_progress_write_enabled: bool = True
    travel_enabled: bool = True
    travel_write_enabled: bool = True
    case_followup_policy_enabled: bool = False
    case_followup_trigger_allowlist: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: object) -> "BusinessEffectPolicy":
        return cls(
            party_query_enabled=bool(
                getattr(settings, "agent2_business_party_query_enabled", False)
            ),
            case_progress_enabled=bool(
                getattr(settings, "agent2_business_case_progress_enabled", False)
            ),
            case_progress_write_enabled=bool(
                getattr(settings, "agent2_business_case_progress_write_enabled", False)
            ),
            travel_enabled=bool(
                getattr(settings, "agent2_business_travel_enabled", False)
            ),
            travel_write_enabled=bool(
                getattr(settings, "agent2_business_travel_write_enabled", False)
            ),
            case_followup_policy_enabled=bool(
                getattr(settings, "case_followup_enabled", False)
            ),
            case_followup_trigger_allowlist=frozenset(
                value.strip()
                for value in str(
                    getattr(settings, "case_followup_trigger_allowlist", "") or ""
                ).replace(";", ",").split(",")
                if value.strip()
            ),
        )

    def require(self, command: BusinessCommand) -> None:
        if isinstance(command, QueryPartyCases):
            self._require(self.party_query_enabled, "party_query_kill_switch_closed")
            return
        if isinstance(command, QueryCaseProgress):
            self._require(self.case_progress_enabled, "case_progress_domain_kill_switch_closed")
            return
        if isinstance(
            command,
            (CreateCaseProgress, UpdateCaseProgress, DeleteCaseProgress, LinkCaseProgress),
        ):
            self._require(self.case_progress_enabled, "case_progress_domain_kill_switch_closed")
            self._require(
                self.case_progress_write_enabled,
                "case_progress_write_kill_switch_closed",
            )
            return
        if isinstance(command, (CreateTravelIntent, UpdateTravelIntent, RespondTravelCollaboration)):
            self._require(self.travel_enabled, "travel_domain_kill_switch_closed")
            self._require(self.travel_write_enabled, "travel_write_kill_switch_closed")
            return
        if isinstance(command, (UpdateCaseFollowupPolicy, SnoozeCaseFollowup)):
            self._require(
                self.case_followup_policy_enabled,
                "case_followup_policy_kill_switch_closed",
            )
            return
        if isinstance(command, TriggerCaseFollowupNow):
            self._require(
                self.case_followup_policy_enabled,
                "case_followup_policy_kill_switch_closed",
            )
            self._require(
                "manual" in self.case_followup_trigger_allowlist,
                "case_followup_manual_trigger_kill_switch_closed",
            )

    @staticmethod
    def _require(enabled: bool, code: str) -> None:
        if not enabled:
            raise BusinessCommandError(code, "kill_switch", "Agent2 business effect is disabled")
