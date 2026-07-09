from __future__ import annotations

from collections import Counter

from app.agent2.harness.schemas import ActualOutcome, HarnessCase, HarnessResult, HarnessRunSummary


def judge_case(case: HarnessCase, actual: ActualOutcome) -> HarnessResult:
    expected = case.expected
    failures: list[str] = []
    warnings = list(actual.warnings)

    if expected.primary_workflow is not None and actual.primary_workflow != expected.primary_workflow:
        failures.append(
            f"primary_workflow expected {expected.primary_workflow!r}, got {actual.primary_workflow!r}"
        )

    if expected.matched_workflows is not None and actual.matched_workflows != expected.matched_workflows:
        failures.append(
            f"matched_workflows expected {expected.matched_workflows!r}, got {actual.matched_workflows!r}"
        )

    for workflow in expected.must_include_workflows:
        if workflow not in actual.matched_workflows:
            failures.append(f"matched_workflows missing required workflow {workflow!r}")

    for workflow in expected.must_not_include_workflows:
        if workflow in actual.matched_workflows:
            failures.append(f"matched_workflows contains forbidden workflow {workflow!r}")

    for effect_type in expected.expected_effects:
        if effect_type not in actual.effect_types:
            failures.append(f"effect_types missing required effect {effect_type!r}")

    for effect_type in expected.forbidden_effects:
        if effect_type in actual.effect_types:
            failures.append(f"effect_types contains forbidden effect {effect_type!r}")

    for action_type in expected.expected_coordination_actions:
        if action_type not in actual.coordination_action_types:
            failures.append(f"coordination_action_types missing required action {action_type!r}")

    for action_type in expected.forbidden_coordination_actions:
        if action_type in actual.coordination_action_types:
            failures.append(f"coordination_action_types contains forbidden action {action_type!r}")

    for candidate_type in expected.expected_sandbox_candidates:
        if candidate_type not in actual.sandbox_candidate_types:
            failures.append(f"sandbox_candidate_types missing required candidate {candidate_type!r}")

    for candidate_type in expected.forbidden_sandbox_candidates:
        if candidate_type in actual.sandbox_candidate_types:
            failures.append(f"sandbox_candidate_types contains forbidden candidate {candidate_type!r}")

    if actual.sandbox_notification_count:
        failures.append(f"sandbox_notification_count expected 0, got {actual.sandbox_notification_count!r}")

    if actual.sandbox_official_write_count:
        failures.append(f"sandbox_official_write_count expected 0, got {actual.sandbox_official_write_count!r}")

    actual_commands = [str(command.get("operation") or "") for command in actual.commands]
    for command in expected.expected_commands:
        if command not in actual_commands:
            failures.append(f"commands missing required operation {command!r}")

    for command in expected.forbidden_commands:
        if command in actual_commands:
            failures.append(f"commands contains forbidden operation {command!r}")

    cognitive_action_types = _cognitive_action_types(actual)
    for action_type in expected.expected_user_actions:
        if action_type not in cognitive_action_types:
            failures.append(f"cognitive actions missing required action {action_type!r}")

    for action_type in expected.forbidden_user_actions:
        if action_type in cognitive_action_types:
            failures.append(f"cognitive actions contains forbidden action {action_type!r}")

    if expected.cognitive_allow_write is not None:
        actual_allow_write = bool(actual.cognitive_decision.get("allow_write"))
        if actual_allow_write != expected.cognitive_allow_write:
            failures.append(
                "cognitive_allow_write expected "
                f"{expected.cognitive_allow_write!r}, got {actual_allow_write!r}"
            )

    if expected.should_enter_daily is not None and actual.gate_allow_legacy_daily != expected.should_enter_daily:
        failures.append(
            "should_enter_daily expected "
            f"{expected.should_enter_daily!r}, got {actual.gate_allow_legacy_daily!r}"
        )

    if expected.need_confirmation is not None and actual.gate_need_confirmation != expected.need_confirmation:
        failures.append(
            f"need_confirmation expected {expected.need_confirmation!r}, got {actual.gate_need_confirmation!r}"
        )

    if expected.need_clarification is not None and actual.gate_need_clarification != expected.need_clarification:
        failures.append(
            f"need_clarification expected {expected.need_clarification!r}, got {actual.gate_need_clarification!r}"
        )

    if expected.safe_to_write is not None:
        actual_safe = (
            actual.gate_allow_legacy_daily
            and not actual.gate_need_confirmation
            and not actual.gate_need_clarification
        )
        if actual_safe != expected.safe_to_write:
            failures.append(f"safe_to_write expected {expected.safe_to_write!r}, got {actual_safe!r}")

    if expected.gate_reply_type is not None and actual.gate_reply_type != expected.gate_reply_type:
        failures.append(f"gate_reply_type expected {expected.gate_reply_type!r}, got {actual.gate_reply_type!r}")

    if expected.safety_commit_policy is not None and actual.safety_commit_policy != expected.safety_commit_policy:
        failures.append(
            "safety_commit_policy expected "
            f"{expected.safety_commit_policy!r}, got {actual.safety_commit_policy!r}"
        )

    if expected.min_segments is not None and len(actual.segments) < expected.min_segments:
        failures.append(f"min_segments expected >= {expected.min_segments}, got {len(actual.segments)}")

    segment_workflows = _segment_workflow_set(actual)
    for workflow in expected.segment_workflows:
        if workflow not in segment_workflows:
            failures.append(f"segments missing workflow {workflow!r}")

    if expected.target_field is not None and expected.target_field not in actual.target_fields:
        failures.append(f"target_field expected {expected.target_field!r}, got {actual.target_fields!r}")

    if expected.expected_knowledge_status is not None and actual.knowledge_status != expected.expected_knowledge_status:
        failures.append(
            f"knowledge_status expected {expected.expected_knowledge_status!r}, got {actual.knowledge_status!r}"
        )

    for source_type in expected.expected_knowledge_sources:
        if source_type not in actual.knowledge_source_types:
            failures.append(f"knowledge_source_types missing required source {source_type!r}")

    for source_type in expected.forbidden_knowledge_sources:
        if source_type in actual.knowledge_source_types:
            failures.append(f"knowledge_source_types contains forbidden source {source_type!r}")

    if expected.report_date is not None:
        warnings.append("report_date expectation is recorded but DailyCommand date resolution is not implemented")

    for violation in actual.contract_invariant_violations:
        failures.append(
            "contract invariant violation "
            f"{violation.get('rule')!r}: {violation.get('message') or violation}"
        )

    passed = not failures
    severity = case.severity if failures else "low"
    return HarnessResult(
        case_id=case.case_id,
        source=case.source,
        passed=passed,
        severity=severity,
        expected_failure=case.expected_failure,
        failures=failures,
        warnings=warnings,
        tags=list(case.tags),
        actual=actual,
        expected=expected,
    )


def summarize_results(results: list[HarnessResult]) -> HarnessRunSummary:
    failed = [result for result in results if not result.passed]
    unexpected_failed = [result for result in failed if not result.expected_failure]
    failure_by_severity = Counter(result.severity for result in unexpected_failed)
    failure_by_tag: Counter[str] = Counter()
    legacy_adapter_status_counts: Counter[str] = Counter()
    for result in unexpected_failed:
        failure_by_tag.update(result.tags)
    for result in results:
        actual = result.actual
        if not actual:
            continue
        for item in actual.legacy_adapter_results:
            legacy_adapter_status_counts.update([str(item.get("status") or "unknown")])

    return HarnessRunSummary(
        total_cases=len(results),
        passed_cases=sum(1 for result in results if result.passed),
        failed_cases=len(failed),
        expected_failure_cases=sum(1 for result in failed if result.expected_failure),
        unexpected_failure_cases=len(unexpected_failed),
        failure_by_severity=dict(failure_by_severity),
        failure_by_tag=dict(failure_by_tag),
        multi_intent_failure_count=_count_failed_with_tag(unexpected_failed, "multi_intent"),
        dangerous_action_failure_count=_count_failed_with_tag(unexpected_failed, "dangerous_action"),
        naked_confirmation_failure_count=_count_failed_with_tag(unexpected_failed, "naked_confirmation"),
        non_daily_false_allow_count=sum(
            1
            for result in unexpected_failed
            if result.expected
            and result.expected.should_enter_daily is False
            and result.actual
            and result.actual.gate_allow_legacy_daily
        ),
        daily_false_block_count=sum(
            1
            for result in unexpected_failed
            if result.expected
            and result.expected.should_enter_daily is True
            and result.actual
            and not result.actual.gate_allow_legacy_daily
        ),
        legacy_adapter_status_counts=dict(legacy_adapter_status_counts),
    )


def _segment_workflow_set(actual: ActualOutcome) -> set[str]:
    workflows: set[str] = set()
    for segment in actual.segments:
        primary = segment.get("primary_workflow")
        if primary:
            workflows.add(str(primary))
        for workflow in segment.get("matched_workflows") or []:
            workflows.add(str(workflow))
    return workflows


def _cognitive_action_types(actual: ActualOutcome) -> list[str]:
    actions = actual.cognitive_decision.get("actions") or []
    result: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("action_type") or "")
        if action_type and action_type not in result:
            result.append(action_type)
    return result


def _count_failed_with_tag(results: list[HarnessResult], tag: str) -> int:
    return sum(1 for result in results if tag in result.tags)
