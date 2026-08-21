import hashlib
import json
from types import SimpleNamespace

from app.agent2.tool_calling.canary_config import (
    canary_prompt_sha256,
    canary_system_prompt,
)
from app.agent2.tool_calling.canary_service import _runtime_attestation
from app.agent2.tool_calling.contracts import (
    ApplyNextWeeklyPlanArgs,
    QueryNextWeeklyPlanArgs,
    SubmitNextWeeklyPlanArgs,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    deepseek_tool_schemas,
    runtime_registry_tool_names,
    validate_tool_arguments,
)


def test_weekly_plan_prompt_keeps_plan_report_and_execution_facts_separate():
    prompt = canary_system_prompt()

    for phrase in (
        "weekly work plan",
        "Monday through Saturday",
        "query_next_weekly_plan",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
        "explicitly_empty",
        "unfilled",
        "accepted suggestion",
        "not completion evidence",
        "direct conversation",
    ):
        assert phrase in prompt


def test_weekly_plan_policy_is_absent_when_weekly_tools_are_not_allowed():
    prompt = canary_system_prompt(
        allowed_tool_names=frozenset({"add_daily_items", "confirm_report"})
    )

    assert "Weekly Work Plan boundary:" not in prompt
    assert "apply_next_weekly_plan" not in prompt
    assert "On a Friday, a bare current-turn phrase" not in prompt


def test_prompt_hash_matches_each_rendered_tool_scope():
    daily_tools = frozenset({"add_daily_items", "confirm_report"})
    weekly_tools = frozenset((*daily_tools, "apply_next_weekly_plan"))

    daily_prompt = canary_system_prompt(allowed_tool_names=daily_tools)
    weekly_prompt = canary_system_prompt(allowed_tool_names=weekly_tools)

    assert daily_prompt != weekly_prompt
    assert canary_prompt_sha256(allowed_tool_names=daily_tools) == hashlib.sha256(
        daily_prompt.encode("utf-8")
    ).hexdigest()
    assert canary_prompt_sha256(allowed_tool_names=weekly_tools) == hashlib.sha256(
        weekly_prompt.encode("utf-8")
    ).hexdigest()


def test_runtime_attestation_hashes_the_prompt_for_enabled_registry_tools():
    base = dict(
        dingtalk_app_key="key",
        dingtalk_app_secret="secret",
        legal_daily_dashboard_enabled=False,
        agent2_cross_user_daily_read_enabled=False,
        agent2_performance_tool_enabled=False,
        legal_ops_data_intake_enabled=False,
        agent2_performance_knowledge_enabled=False,
        agent2_current_weekly_report_enabled=False,
    )
    daily_settings = SimpleNamespace(
        **base,
        agent2_weekly_plan_enabled=False,
        agent2_weekly_plan_write_enabled=False,
    )
    weekly_settings = SimpleNamespace(
        **base,
        agent2_weekly_plan_enabled=True,
        agent2_weekly_plan_write_enabled=True,
    )

    daily_attestation = _runtime_attestation(daily_settings)
    weekly_attestation = _runtime_attestation(weekly_settings)

    assert daily_attestation.prompt_sha256 == canary_prompt_sha256(
        allowed_tool_names=frozenset(runtime_registry_tool_names(daily_settings))
    )
    assert weekly_attestation.prompt_sha256 == canary_prompt_sha256(
        allowed_tool_names=frozenset(runtime_registry_tool_names(weekly_settings))
    )
    assert daily_attestation.prompt_sha256 != weekly_attestation.prompt_sha256


def test_weekly_plan_prompt_requires_one_preview_and_explicit_submission():
    prompt = " ".join(canary_system_prompt().split()).lower()

    assert "do not interview the user one day at a time" in prompt
    assert "one complete preview" in prompt
    assert "never auto-submit" in prompt
    assert "exact server dates" in prompt
    assert "not found a later record" in prompt
    assert "while the person is filling a daily report" in prompt
    assert "capture_suggestion" in prompt
    assert "not permission to guess a formal day" in prompt
    assert "remain separate records" in prompt


def test_weekly_plan_prompt_keeps_generic_tomorrow_plan_in_active_daily_report():
    prompt = " ".join(canary_system_prompt().split()).lower()

    assert (
        "tomorrow_plan is the authenticated person's next reporting-day plan"
        in prompt
    )
    assert "a generic plan addition" in prompt
    assert (
        "does not authorize creating a weekly work plan or suggestion" in prompt
    )
    assert "existing daily work or daily content needs no change" in prompt
    assert "do not capture them as weekly plan suggestions" in prompt


def test_daily_prompt_uses_the_unique_trusted_previous_report_for_a_brief_copy():
    prompt = " ".join(canary_system_prompt().split()).lower()
    description = TOOL_REGISTRY["copy_previous_to_today"].description.lower()

    assert "call `copy_previous_to_today`" in prompt
    assert "do not ask the user to repeat those facts" in prompt
    assert "do not claim that no source report exists" in prompt
    assert "same as yesterday" in prompt
    assert "need not be preloaded" in description
    assert "let the server verify" in description


def test_weekly_plan_prompt_preserves_repeated_ranges_and_shared_date_scope():
    prompt = " ".join(canary_system_prompt().split()).lower()

    assert "one add operation for every selected exact date" in prompt
    assert "monday through friday every day" in prompt
    assert "leading day applies to every clearly parallel matter" in prompt
    assert "entire current user message" in prompt
    assert "recurrence_scope_quote" in prompt
    assert "include every attached bound, exception, or qualifier" in prompt
    assert "never shorten a phrase" in prompt
    assert "do not turn a repeated dated matter into an undated suggestion" in prompt
    assert "quote `下周每天`, not the whole phrase" in prompt
    assert "stops before the action and work matter" in prompt


def test_weekly_plan_prompt_keeps_operation_control_out_of_plan_content():
    prompt = " ".join(canary_system_prompt().split()).lower()
    description = TOOL_REGISTRY["apply_next_weekly_plan"].description.lower()

    assert "operation-control language out of stored plan text" in prompt
    assert "deliberately not submitting" in prompt
    assert "store only the user's work matter" in prompt
    assert "operation control" in description
    assert "must not be included in content" in description


def test_weekly_plan_prompt_preserves_safe_changes_when_submit_must_wait():
    prompt = " ".join(canary_system_prompt().split()).lower()

    assert "preserve and apply every clear safe change" in prompt
    assert "omit only the unsafe submission" in prompt


def test_weekly_plan_content_review_proof_is_server_only():
    schema = deepseek_tool_schemas(
        frozenset({"apply_next_weekly_plan"})
    )[0]["function"]["parameters"]

    assert "content_reviewed" in ApplyNextWeeklyPlanArgs.model_fields
    assert "content_reviewed" not in schema["properties"]

    submit_schema = deepseek_tool_schemas(
        frozenset({"submit_next_weekly_plan"})
    )[0]["function"]["parameters"]
    assert "reviewed_unfilled_days_as_empty" in (
        SubmitNextWeeklyPlanArgs.model_fields
    )
    assert "reviewed_unfilled_days_as_empty" not in submit_schema["properties"]


def test_weekly_plan_content_field_excludes_operation_instructions():
    schema_text = json.dumps(
        deepseek_tool_schemas(frozenset({"apply_next_weekly_plan"}))[0],
        ensure_ascii=False,
    )

    assert "Only the user's planned work matter" in schema_text
    assert "deliberately not submitting the plan" in schema_text


def test_weekly_plan_prompt_explains_monday_dual_targets_and_report_boundary():
    prompt = " ".join(canary_system_prompt().split()).lower()

    for phrase in (
        "weekly_plan_targets",
        "active_collection",
        "natural_next",
        "on monday",
        "current-week plan",
        "following-week plan",
        "plan_id",
        "weekly report is a current-week review",
        "never use weekly-plan tools",
        "legacy tool names",
        "include its plan_id",
    ):
        assert phrase in prompt


def test_weekly_plan_tool_contract_text_selects_exact_targets_without_renaming_schema():
    query = TOOL_REGISTRY["query_next_weekly_plan"]
    apply = TOOL_REGISTRY["apply_next_weekly_plan"]
    submit = TOOL_REGISTRY["submit_next_weekly_plan"]
    combined = " ".join(
        (query.description, apply.description, submit.description)
    ).lower()

    assert "multiple exact" in combined
    assert "active_collection" in combined
    assert "natural_next" in combined
    assert "weekly report" in combined
    assert "plan_id" in combined
    assert "legacy" in combined
    assert query.input_model is QueryNextWeeklyPlanArgs
    assert apply.input_model is ApplyNextWeeklyPlanArgs
    assert submit.input_model is SubmitNextWeeklyPlanArgs
    assert set(QueryNextWeeklyPlanArgs.model_fields) == {"plan_id"}
    assert QueryNextWeeklyPlanArgs.model_fields["plan_id"].is_required() is False
    assert "selected exact weekly-plan target" in (ApplyNextWeeklyPlanArgs.__doc__ or "").lower()
    assert "selected exact weekly-plan target" in (SubmitNextWeeklyPlanArgs.__doc__ or "").lower()


def test_weekly_plan_query_keeps_single_target_compatibility_and_accepts_exact_plan_id():
    plan_id = "11111111-1111-4111-8111-111111111111"

    assert validate_tool_arguments("query_next_weekly_plan", {}) == {
        "plan_id": None
    }
    assert validate_tool_arguments(
        "query_next_weekly_plan", {"plan_id": plan_id}
    ) == {"plan_id": plan_id}
