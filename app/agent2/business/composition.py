from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.agent2.business.compiler import Phase2BusinessCommandCompiler, Phase2CommandCompilation
from app.agent2.business.admission import (
    bind_business_execution_context,
    validate_business_candidate_admission,
    validate_case_progress_ticket_object,
)
from app.agent2.business.contracts import (
    BusinessCommand,
    BusinessCommandContext,
    BusinessReceipt,
    DeleteCaseProgress,
    LinkCaseProgress,
    QueryCaseProgress,
    QueryPartyCases,
    UpdateCaseProgress,
)
from app.agent2.business.case_progress import resolve_case_target
from app.agent2.business.case_labels import case_stage_label, case_type_label
from app.agent2.business.repositories import (
    CaseProgressSqlRepository,
    CaseFollowupPolicySqlRepository,
    CaseSqlRepository,
    PartyQueryScope,
    PartySqlRepository,
)
from app.agent2.command_planner_v3 import PlanningBlock, TypedBusinessCommand


class AsyncBusinessExecutor(Protocol):
    async def execute(
        self, command: BusinessCommand, context: BusinessCommandContext
    ) -> BusinessReceipt: ...


@dataclass(frozen=True)
class BusinessActionResult:
    semantic_command_id: str
    semantic_command_type: str
    compiled_command_type: str = ""
    receipt: BusinessReceipt | None = None
    block: PlanningBlock | None = None
    outcome_context: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.block is not None:
            return "blocked"
        return self.receipt.status if self.receipt is not None else "failed"


@dataclass(frozen=True)
class BusinessCompositionResult:
    source_message_id: str
    actions: tuple[BusinessActionResult, ...]

    @property
    def executed_count(self) -> int:
        return sum(item.status in {"executed", "duplicate"} for item in self.actions)

    @property
    def blocked_count(self) -> int:
        return sum(item.status == "blocked" for item in self.actions)


class Phase2BusinessComposer:
    """Compile and execute each business segment independently.

    One ambiguous segment cannot erase successful sibling-domain actions. Each
    executable command receives its own receipt and idempotency key while all
    commands retain the same source message reference in the execution context.
    """

    def __init__(
        self,
        *,
        case_repository: CaseSqlRepository,
        party_repository: PartySqlRepository | None = None,
        progress_repository: CaseProgressSqlRepository | None = None,
        followup_policy_repository: CaseFollowupPolicySqlRepository | None = None,
        executor: AsyncBusinessExecutor,
        compiler: Phase2BusinessCommandCompiler | None = None,
    ):
        self.case_repository = case_repository
        self.party_repository = party_repository
        self.progress_repository = progress_repository
        self.followup_policy_repository = followup_policy_repository
        self.executor = executor
        self.compiler = compiler or Phase2BusinessCommandCompiler()

    async def execute(
        self,
        candidates: tuple[TypedBusinessCommand, ...],
        context: BusinessCommandContext,
    ) -> BusinessCompositionResult:
        cases = await self.case_repository.list_visible(context)
        followup_policies = (
            await self.followup_policy_repository.list_visible(context)
            if self.followup_policy_repository is not None
            else ()
        )
        actions: list[BusinessActionResult] = []
        for candidate in candidates:
            execution_context = bind_business_execution_context(candidate, context)
            if candidate.command_type == "query_case_risk":
                compilation = await self._compile_party_query(candidate, execution_context)
            elif candidate.command_type in {
                "update_case_progress_candidate",
                "delete_case_progress_candidate",
                "query_case_progress_candidate",
                "link_case_progress_candidate",
            }:
                compilation = await self._compile_progress_action(candidate, execution_context, cases)
            else:
                compilation = self.compiler.compile(
                    candidate, execution_context, cases=cases,
                    followup_policies=followup_policies,
                )
            if compilation.block is not None:
                actions.append(
                    BusinessActionResult(
                        semantic_command_id=str(candidate.command_id),
                        semantic_command_type=candidate.command_type,
                        block=compilation.block,
                        outcome_context=dict(compilation.outcome_context),
                    )
                )
                continue
            command = compilation.command
            if command is None:
                actions.append(
                    BusinessActionResult(
                        semantic_command_id=str(candidate.command_id),
                        semantic_command_type=candidate.command_type,
                        block=PlanningBlock(
                            str(candidate.sub_decision_id),
                            "phase2_compiler_returned_no_outcome",
                        ),
                    )
                )
                continue
            receipt = await self.executor.execute(command, execution_context)
            actions.append(
                BusinessActionResult(
                    semantic_command_id=str(candidate.command_id),
                    semantic_command_type=candidate.command_type,
                    compiled_command_type=command.command_type,
                    receipt=receipt,
                    outcome_context=dict(compilation.outcome_context),
                )
            )
        return BusinessCompositionResult(context.source_message_id, tuple(actions))

    async def _compile_party_query(
        self,
        candidate: TypedBusinessCommand,
        context: BusinessCommandContext,
    ) -> Phase2CommandCompilation:
        if self.party_repository is None:
            return Phase2CommandCompilation(
                block=PlanningBlock(str(candidate.sub_decision_id), "party_repository_required")
            )
        entities = candidate.payload.get("entities") or ()
        if len(entities) != 1 or not isinstance(entities[0], dict):
            return Phase2CommandCompilation(
                block=PlanningBlock(str(candidate.sub_decision_id), "single_case_query_entity_required")
            )
        entity = entities[0]
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        query = str(
            attributes.get("matter_hint")
            or attributes.get("query")
            or entity.get("value")
            or ""
        ).strip()
        resolution = await self.party_repository.resolve(
            query,
            scope=PartyQueryScope(context.tenant_id, context.allowed_case_ids),
        )
        if resolution.status == "needs_clarification":
            return Phase2CommandCompilation(
                block=PlanningBlock(
                    str(candidate.sub_decision_id),
                    "party_target_needs_clarification",
                    ",".join(item.party_id for item in resolution.candidates),
                )
            )
        if resolution.status != "resolved":
            return Phase2CommandCompilation(
                block=PlanningBlock(str(candidate.sub_decision_id), "party_target_not_found")
            )
        question = " ".join(
            str(value or "")
            for value in (
                attributes.get("question"),
                attributes.get("topic"),
                entity.get("value"),
            )
        )
        return Phase2CommandCompilation(
            command=QueryPartyCases(
                command_id=str(candidate.command_id),
                party_id=resolution.party_id,
                match_basis=resolution.match_basis,
                role_type=_party_role_from_question(question),
                include_recent_progress="进展" in question or "最近" in question,
            )
        )

    async def _compile_progress_action(
        self,
        candidate: TypedBusinessCommand,
        context: BusinessCommandContext,
        cases: tuple,
    ) -> Phase2CommandCompilation:
        admission_block = validate_business_candidate_admission(candidate, context)
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        entities = candidate.payload.get("entities") or ()
        if len(entities) != 1 or not isinstance(entities[0], dict):
            return Phase2CommandCompilation(
                block=PlanningBlock(str(candidate.sub_decision_id), "single_case_progress_ref_required")
            )
        entity = entities[0]
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        case_hint = str(attributes.get("case_hint") or "").strip()
        resolved_case_id = ""
        if case_hint:
            case_resolution = resolve_case_target(case_hint, cases, context)
            if case_resolution.status == "needs_clarification":
                return Phase2CommandCompilation(
                    block=PlanningBlock(
                        str(candidate.sub_decision_id),
                        "case_target_needs_clarification",
                        ",".join(case_resolution.candidate_case_ids),
                    )
                )
            if case_resolution.status != "resolved":
                return Phase2CommandCompilation(
                    block=PlanningBlock(str(candidate.sub_decision_id), "case_target_not_found")
                )
            resolved_case_id = case_resolution.case_id
        if candidate.command_type == "query_case_progress_candidate":
            if not resolved_case_id:
                return Phase2CommandCompilation(
                    block=PlanningBlock(str(candidate.sub_decision_id), "case_target_required")
                )
            return Phase2CommandCompilation(
                command=QueryCaseProgress(
                    command_id=str(candidate.command_id),
                    case_id=resolved_case_id,
                    start_at=_optional_datetime(attributes.get("start_at")),
                    end_at=_optional_datetime(attributes.get("end_at")),
                ),
                outcome_context={
                    "case_name": _case_name_for_id(cases, resolved_case_id),
                },
            )
        if self.progress_repository is None:
            return Phase2CommandCompilation(
                block=PlanningBlock(str(candidate.sub_decision_id), "case_progress_repository_required")
            )
        target = await self.progress_repository.resolve_write_target(
            context,
            progress_id=str(attributes.get("progress_id") or "").strip(),
            case_id=resolved_case_id,
        )
        if target.status == "needs_clarification":
            return Phase2CommandCompilation(
                block=PlanningBlock(
                    str(candidate.sub_decision_id),
                    "case_progress_target_needs_clarification",
                    ",".join(target.candidate_progress_ids),
                )
            )
        if target.status != "resolved":
            return Phase2CommandCompilation(
                block=PlanningBlock(str(candidate.sub_decision_id), "case_progress_target_not_found")
            )
        outcome_context = {
            "case_name": _case_name_for_id(cases, target.case_id),
        }
        admission_block = validate_case_progress_ticket_object(
            candidate,
            progress_id=target.progress_id,
            case_id=target.case_id,
            progress_version=target.version,
        )
        if admission_block is not None:
            return Phase2CommandCompilation(block=admission_block)
        if candidate.command_type == "update_case_progress_candidate":
            summary = _optional_text(attributes.get("replacement_summary"))
            details = _optional_text(attributes.get("replacement_details"))
            if summary is None and details is None:
                return Phase2CommandCompilation(
                    block=PlanningBlock(str(candidate.sub_decision_id), "case_progress_replacement_required")
                )
            return Phase2CommandCompilation(
                command=UpdateCaseProgress(
                    command_id=str(candidate.command_id),
                    progress_id=target.progress_id,
                    expected_version=target.version,
                    summary=summary,
                    details=details,
                ),
                outcome_context=outcome_context,
            )
        if candidate.command_type == "delete_case_progress_candidate":
            return Phase2CommandCompilation(
                command=DeleteCaseProgress(
                    command_id=str(candidate.command_id),
                    progress_id=target.progress_id,
                    expected_version=target.version,
                    reason=str(attributes.get("delete_reason") or "user_requested_delete").strip(),
                ),
                outcome_context=outcome_context,
            )
        if candidate.command_type == "link_case_progress_candidate":
            return Phase2CommandCompilation(
                command=LinkCaseProgress(
                    command_id=str(candidate.command_id),
                    progress_id=target.progress_id,
                    expected_version=target.version,
                    related_party_ids=_string_tuple(attributes.get("related_party_ids")),
                    related_document_ids=_string_tuple(attributes.get("related_document_ids")),
                    related_travel_intent_ids=_string_tuple(attributes.get("related_travel_intent_ids")),
                ),
                outcome_context=outcome_context,
            )
        return Phase2CommandCompilation(
            block=PlanningBlock(str(candidate.sub_decision_id), "unsupported_case_progress_candidate")
        )


def business_composition_reply_text(result: BusinessCompositionResult) -> str:
    lines = ["业务动作结果："]
    for action in result.actions:
        receipt = action.receipt
        if receipt is not None and receipt.status in {"executed", "duplicate"}:
            if action.compiled_command_type == "query_party_cases":
                party = receipt.after.get("party") if isinstance(receipt.after, dict) else {}
                party = party if isinstance(party, dict) else {}
                cases = receipt.after.get("cases", []) if isinstance(receipt.after, dict) else []
                status_counts = receipt.after.get("status_counts", {}) if isinstance(receipt.after, dict) else {}
                lines.append(
                    f"- 主体：{party.get('canonical_name') or receipt.resource_id}；"
                    f"匹配依据：{receipt.after.get('match_basis') or 'unknown'}；"
                    f"案件 {receipt.after.get('case_count', len(cases))} 件；"
                    f"状态：{status_counts}"
                )
                for item in cases[:10]:
                    if isinstance(item, dict):
                        lines.append(
                            f"  - {item.get('case_number') or item.get('external_case_id')} "
                            f"{item.get('case_name')} [{item.get('role_type')}/{item.get('status')}]"
                        )
                continue
            if action.compiled_command_type == "list_assigned_cases":
                cases = receipt.after.get("cases", []) if isinstance(receipt.after, dict) else []
                count = receipt.after.get("case_count", len(cases)) if isinstance(receipt.after, dict) else len(cases)
                lines.append(f"你当前负责 {count} 件案件：")
                for index, item in enumerate(cases, start=1):
                    if not isinstance(item, dict):
                        continue
                    case_number = str(item.get("case_number") or "").strip()
                    case_name = str(item.get("case_name") or "未命名案件").strip()
                    case_type = case_type_label(item.get("case_type"))
                    stage = case_stage_label(item.get("stage") or item.get("status"))
                    metadata = " / ".join(value for value in (case_type, stage) if value)
                    suffix = f"（{metadata}）" if metadata else ""
                    number_prefix = f"{case_number} " if case_number else ""
                    lines.append(f"{index}. {number_prefix}{case_name}{suffix}")
                continue
            if action.compiled_command_type == "query_case_progress":
                items = receipt.after.get("items", []) if isinstance(receipt.after, dict) else []
                lines.append(f"- 案件进展查询：{len(items)} 条")
                for item in items[:10]:
                    if isinstance(item, dict):
                        lines.append(
                            f"  - {item.get('occurred_at')} {item.get('summary')} "
                            f"[{item.get('content_origin')}/v{item.get('version')}]"
                        )
                continue
            prefix = "已处理（重复请求未重复写入）" if receipt.status == "duplicate" else "已完成"
            label = {
                "create_travel_intent": "出差计划登记",
                "create_case_progress": "案件进展记录",
                "update_case_progress": "案件进展修改",
                "delete_case_progress": "案件进展删除",
                "link_case_progress": "案件进展关联",
                "query_party_cases": "主体关联案件查询",
            }.get(action.compiled_command_type, action.compiled_command_type or action.semantic_command_type)
            lines.append(f"- {prefix}：{label}（回执 {receipt.receipt_id}）")
            continue
        if receipt is not None:
            lines.append(
                f"- 未完成：{action.semantic_command_type}，"
                f"{receipt.error_code or receipt.failed_stage or '执行失败'}"
            )
            continue
        block = action.block
        reason = block.reason_code if block is not None else "unknown_block"
        detail = f"（候选：{block.detail}）" if block is not None and block.detail else ""
        lines.append(f"- 尚未记录：{action.semantic_command_type}，{reason}{detail}")
    return "\n".join(lines)


def _party_role_from_question(question: str) -> str:
    mappings = (
        ("申请执行人", "applicant_enforcement"),
        ("被执行人", "person_subject_to_enforcement"),
        ("被告", "defendant"),
        ("原告", "plaintiff"),
        ("第三人", "third_party"),
        ("担保人", "guarantor"),
        ("债务人", "debtor"),
        ("债权人", "creditor"),
    )
    return next((role for marker, role in mappings if marker in question), "")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("case progress query datetime must be timezone-aware")
    return parsed


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _case_name_for_id(cases: tuple, case_id: str) -> str:
    return next(
        (
            str(item.case_name).strip()
            for item in cases
            if str(item.case_id) == str(case_id) and str(item.case_name).strip()
        ),
        "该案件",
    )
