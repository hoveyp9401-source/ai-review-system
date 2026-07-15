from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agent2.business.contracts import (
    BusinessCommandError,
    CreateCaseProgress,
    CreateTravelIntent,
    QueryCaseProgress,
    QueryPartyCases,
)
from app.agent2.business.policy import BusinessEffectPolicy


NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _party_query() -> QueryPartyCases:
    return QueryPartyCases(
        command_id="party-query-1",
        party_id="party-1",
        match_basis="canonical_name",
    )


def _progress_query() -> QueryCaseProgress:
    return QueryCaseProgress(command_id="progress-query-1", case_id="case-1")


def _progress_write() -> CreateCaseProgress:
    return CreateCaseProgress(
        command_id="progress-create-1",
        case_id="case-1",
        occurred_at=NOW,
        progress_type="court_update",
        summary="法院下周重新查控",
        details="",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=0.99,
    )


def _travel_write() -> CreateTravelIntent:
    return CreateTravelIntent(
        command_id="travel-create-1",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=NOW,
        end_at=NOW,
        time_precision="day",
        purpose_summary="",
        related_case_ids=(),
        confidence=0.99,
    )


def test_policy_from_settings_is_fail_closed_when_switches_are_absent():
    policy = BusinessEffectPolicy.from_settings(SimpleNamespace())

    for command in (_party_query(), _progress_query(), _progress_write(), _travel_write()):
        with pytest.raises(BusinessCommandError) as exc:
            policy.require(command)
        assert exc.value.stage == "kill_switch"


def test_domain_query_can_stay_enabled_while_write_effect_is_closed():
    policy = BusinessEffectPolicy(
        party_query_enabled=True,
        case_progress_enabled=True,
        case_progress_write_enabled=False,
        travel_enabled=True,
        travel_write_enabled=False,
    )

    policy.require(_party_query())
    policy.require(_progress_query())
    with pytest.raises(BusinessCommandError) as progress_error:
        policy.require(_progress_write())
    with pytest.raises(BusinessCommandError) as travel_error:
        policy.require(_travel_write())
    assert progress_error.value.code == "case_progress_write_kill_switch_closed"
    assert travel_error.value.code == "travel_write_kill_switch_closed"


def test_all_switches_open_allows_target_business_commands():
    policy = BusinessEffectPolicy()

    for command in (_party_query(), _progress_query(), _progress_write(), _travel_write()):
        policy.require(command)
