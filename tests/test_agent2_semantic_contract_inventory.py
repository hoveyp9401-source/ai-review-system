from __future__ import annotations

import re

from app.agent2.admission_artifact_sink_sql import (
    _READ_ONLY_ADMITTED_OPERATIONS,
)
from app.agent2.command_planner_v3 import _READ_ONLY_COGNITIVE_ACTIONS
from app.agent2.cognitive_core_v3 import _ACTION_ENTITY_TYPES
from app.agent2.domain_admission import (
    _READ_ONLY_ACTIONS,
    _REPORT_ACTIONS,
    _action_requires_execution_ticket,
)
from app.agent2.semantic_interpreter_v3 import PROMPT_PATH


_NON_REPORT_ADMISSION_ACTIONS = {
    "record_case_progress",
    "update_case_progress",
    "delete_case_progress",
    "link_case_progress",
    "answer_case_query",
    "query_case_progress",
    "query_operation_status",
    "search_enterprise_knowledge",
    "record_travel_event",
    "respond_travel_collaboration",
    "update_case_followup_policy",
    "trigger_case_followup_now",
}


def test_prompt_action_allowlist_matches_the_runtime_contract():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"Use only semantic action types (?P<body>.+?)\. These are required capabilities",
        prompt,
        flags=re.DOTALL,
    )
    assert match is not None
    prompt_actions = set(re.findall(r"`([a-z0-9_]+)`", match.group("body")))
    expected = set(_ACTION_ENTITY_TYPES) | {
        "continue_pending",
        "submit_daily_report",
    }
    assert prompt_actions == expected


def test_case_followup_policy_is_in_the_closed_prompt_vocabulary():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "`case_followup_policy`" in prompt
    assert "`case_followup_policy={" in prompt
    assert "`update_case_followup_policy`" in prompt
    assert "`trigger_case_followup_now`" in prompt


def test_every_runtime_semantic_action_has_a_domain_admission_contract():
    runtime_actions = set(_ACTION_ENTITY_TYPES) | {"submit_daily_report"}

    assert runtime_actions == set(_REPORT_ACTIONS) | _NON_REPORT_ADMISSION_ACTIONS


def test_read_only_inventory_is_identical_at_admission_planner_and_sql_sink():
    expected = {
        "answer_case_query",
        "query_case_progress",
        "query_daily_report",
        "query_operation_status",
        "query_periodic_report",
        "search_enterprise_knowledge",
    }

    assert set(_READ_ONLY_ACTIONS) == expected
    assert set(_READ_ONLY_COGNITIVE_ACTIONS) == expected
    assert set(_READ_ONLY_ADMITTED_OPERATIONS) == expected


def test_every_non_read_only_semantic_action_requires_an_execution_ticket():
    runtime_actions = set(_ACTION_ENTITY_TYPES) | {"submit_daily_report"}

    assert {
        action
        for action in runtime_actions
        if not _action_requires_execution_ticket(action)
    } == set(_READ_ONLY_ACTIONS)
    assert all(
        _action_requires_execution_ticket(action)
        for action in runtime_actions - set(_READ_ONLY_ACTIONS)
    )
