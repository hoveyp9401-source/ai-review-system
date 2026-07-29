from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.legal_daily_dashboard.domain import (
    DashboardActor,
    DashboardRecords,
    ManagerDecisionRecord,
)
from app.legal_daily_dashboard.repository import DashboardRepository


class DashboardNotFound(LookupError):
    """The requested dashboard resource is outside the actor's visible scope."""


class DashboardService:
    """Public interface for scoped legal daily-report management views."""

    def __init__(self, repository: DashboardRepository) -> None:
        self._repository = repository

    async def get_overview(
        self,
        *,
        actor: DashboardActor,
        report_date: date,
        team_ref: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=report_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        if scope.role == "team_lead":
            allowed_team = scope.allowed_team_refs[0] if scope.allowed_team_refs else ""
            requested_team = team_ref or allowed_team
            if not requested_team or requested_team not in scope.allowed_team_refs:
                raise DashboardNotFound("team not found")
            selected_team = requested_team
        else:
            selected_team = team_ref
        all_teams = await self._repository.list_teams(tenant_id=actor.tenant_id)
        visible_teams = (
            all_teams
            if scope.can_view_all_teams
            else tuple(
                team
                for team in all_teams
                if team.ref in scope.allowed_team_refs
            )
        )
        visible_records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=(
                None if scope.can_view_all_teams else scope.allowed_team_refs
            ),
            start_date=report_date,
            end_date=report_date,
        )
        records = (
            _records_for_team(visible_records, selected_team)
            if selected_team
            else visible_records
        )
        metrics = _overview_metrics(records=records, now=now)
        responsibility_complete = bool(
            metrics["responsibility_data_complete"]
        )
        actions = _management_actions(
            records=records,
            teams={team.ref: team.name for team in all_teams},
            report_date=report_date,
            now=now,
        )
        today_confirmation_count = sum(
            1
            for action in actions
            if action["category"] == "today_confirmation"
        )
        team_followup_count = sum(
            1 for action in actions if action["category"] == "team_followup"
        )
        department_support_count = sum(
            1
            for action in actions
            if action["category"] == "department_support"
        )
        confirmation_attention_count = int(
            metrics["pending_confirmation_count"] or 0
        )
        total_attention = len(actions)
        scope_label = "七团队" if scope.can_view_all_teams and not selected_team else "本团队"
        return {
            "date": report_date.isoformat(),
            "as_of": now.isoformat(),
            "scope": {
                "role": scope.role,
                "selected_team_ref": selected_team,
            },
            "metrics": metrics,
            "data_quality": {
                "responsibility": (
                    "complete" if responsibility_complete else "missing"
                ),
                "messages": (
                    []
                    if responsibility_complete
                    else [
                        "缺少可靠的请假、休假、入离职或当天无需提交数据；"
                        "应交与未交人数待核实。"
                    ]
                ),
            },
            "management_summary": {
                "today_confirmation_count": today_confirmation_count,
                "team_followup_count": team_followup_count,
                "department_support_count": department_support_count,
                "confirmation_attention_count": confirmation_attention_count,
                "headline": (
                    f"{scope_label}当前 {total_attention} 项需要关注，其中 "
                    f"{today_confirmation_count} 项今日需确认，"
                    f"{team_followup_count} 项建议团队负责人跟进，"
                    f"{department_support_count} 项需要法务负责人协调。"
                ),
            },
            "actions": actions,
            "teams": [
                {
                    "ref": team.ref,
                    "name": team.name,
                    "metrics": _overview_metrics(
                        records=_records_for_team(
                            visible_records,
                            team.ref,
                        ),
                        now=now,
                    ),
                }
                for team in visible_teams
            ],
        }

    async def get_members(
        self,
        *,
        actor: DashboardActor,
        report_date: date,
        team_ref: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=report_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        if scope.role == "team_lead":
            allowed_team = scope.allowed_team_refs[0] if scope.allowed_team_refs else ""
            selected_team = team_ref or allowed_team
            if not selected_team or selected_team not in scope.allowed_team_refs:
                raise DashboardNotFound("team not found")
        else:
            selected_team = team_ref
        records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=(selected_team,) if selected_team else None,
            start_date=report_date,
            end_date=report_date,
        )
        teams = {
            team.ref: team
            for team in await self._repository.list_teams(
                tenant_id=actor.tenant_id
            )
        }
        obligations = {
            (item.member_ref, item.report_date): item
            for item in records.obligations
        }
        reports = {
            (item.member_ref, item.report_date): item
            for item in records.reports
        }
        pending_review_member_refs = {
            suggestion.member_ref
            for suggestion in _pending_suggestions(records)
            if suggestion.report_date == report_date
        }
        members = []
        for member in records.members:
            obligation = obligations.get((member.ref, report_date))
            report = reports.get((member.ref, report_date))
            status_label, confirmation_label = _member_status(
                obligation=obligation,
                report=report,
                now=now,
            )
            members.append(
                {
                    "ref": member.ref,
                    "name": member.name,
                    "team_name": teams.get(member.team_ref).name
                    if member.team_ref in teams
                    else "",
                    "status_label": status_label,
                    "submitted_at": (
                        report.submitted_at.isoformat()
                        if report and report.submitted_at
                        else None
                    ),
                    "confirmation_label": confirmation_label,
                    "suggest_review": (
                        member.ref in pending_review_member_refs
                    ),
                }
            )
        return {
            "date": report_date.isoformat(),
            "scope": {
                "role": scope.role,
                "selected_team_ref": selected_team,
            },
            "members": members,
        }

    async def resolve_member_team(
        self,
        *,
        actor: DashboardActor,
        member_ref: str,
        on_date: date,
    ) -> str:
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=on_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=(
                None if scope.can_view_all_teams else scope.allowed_team_refs
            ),
            start_date=on_date,
            end_date=on_date,
        )
        member = next(
            (item for item in records.members if item.ref == member_ref),
            None,
        )
        if member is None:
            raise DashboardNotFound("member not found")
        return member.team_ref

    async def get_member_timeline(
        self,
        *,
        actor: DashboardActor,
        member_ref: str,
        end_date: date,
        days: int,
        now: datetime,
    ) -> dict[str, Any]:
        if days not in {7, 14, 30}:
            raise ValueError("days must be 7, 14, or 30")
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=end_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        visible_team_refs = (
            None if scope.can_view_all_teams else scope.allowed_team_refs
        )
        start_date = end_date - timedelta(days=days - 1)
        records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=visible_team_refs,
            start_date=start_date,
            end_date=end_date,
        )
        member = next(
            (item for item in records.members if item.ref == member_ref),
            None,
        )
        if member is None:
            raise DashboardNotFound("member not found")
        teams = {
            team.ref: team
            for team in await self._repository.list_teams(
                tenant_id=actor.tenant_id
            )
        }
        obligations = {
            (item.member_ref, item.report_date): item
            for item in records.obligations
        }
        reports = {
            (item.member_ref, item.report_date): item
            for item in records.reports
        }
        suggestions_by_date: dict[date, list[Any]] = {}
        for suggestion in records.suggestions:
            if suggestion.member_ref == member_ref and suggestion.active:
                suggestions_by_date.setdefault(
                    suggestion.report_date,
                    [],
                ).append(suggestion)
        latest_decisions = _latest_decisions(records)
        linked_items_by_date: dict[date, list[dict[str, Any]]] = {}
        linked_item_dates: set[tuple[str, date]] = set()
        for item in records.work_items:
            if not item.active or member_ref not in item.member_refs:
                continue
            manager_decision = latest_decisions.get(item.ref)
            for item_entry in item.entries:
                if item_entry.member_ref != member_ref:
                    continue
                item_date_key = (item.ref, item_entry.entry_date)
                if item_date_key in linked_item_dates:
                    continue
                linked_item_dates.add(item_date_key)
                linked_items_by_date.setdefault(
                    item_entry.entry_date,
                    [],
                ).append(
                    {
                        "ref": item.ref,
                        "title": item.title,
                        "status_label": _work_item_status_label(
                            item.status
                        ),
                        "summary": item.summary,
                        "manager_status": (
                            _decision_label(manager_decision.decision)
                            if manager_decision
                            else "待负责人处理"
                        ),
                        "manager_decision": (
                            _serialize_decision(manager_decision)
                            if manager_decision
                            else None
                        ),
                        "model_version": item.model_version,
                        "evaluated_at": (
                            item.evaluated_at.isoformat()
                            if item.evaluated_at
                            else None
                        ),
                    }
                )
        entries = []
        for offset in range(days):
            current_date = end_date - timedelta(days=offset)
            obligation = obligations.get((member_ref, current_date))
            report = reports.get((member_ref, current_date))
            status_label, confirmation_label = _member_status(
                obligation=obligation,
                report=report,
                now=now,
            )
            entries.append(
                {
                    "date": current_date.isoformat(),
                    "date_label": _date_label(current_date),
                    "status_label": status_label,
                    "today_work": list(report.today_work) if report else [],
                    "problems": list(report.problems) if report else [],
                    "tomorrow_plan": (
                        list(report.tomorrow_plan) if report else []
                    ),
                    "submitted_at": (
                        report.submitted_at.isoformat()
                        if report and report.submitted_at
                        else None
                    ),
                    "confirmation_label": confirmation_label,
                    "raw_evidence": report.raw_input if report else "",
                    "section_completeness": _section_completeness(
                        report
                    ),
                    "review_suggestions": [
                        _serialize_suggestion(
                            suggestion,
                            manager_decision=latest_decisions.get(
                                suggestion.ref
                            ),
                        )
                        for suggestion in suggestions_by_date.get(
                            current_date,
                            [],
                        )
                    ],
                    "linked_items": linked_items_by_date.get(
                        current_date,
                        [],
                    ),
                }
            )
        return {
            "member": {
                "name": member.name,
                "team_name": (
                    teams[member.team_ref].name
                    if member.team_ref in teams
                    else ""
                ),
            },
            "range": {
                "days": days,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "days": entries,
        }

    async def get_items(
        self,
        *,
        actor: DashboardActor,
        end_date: date,
        team_ref: str | None,
    ) -> dict[str, Any]:
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=end_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        if scope.role == "team_lead":
            allowed_team = scope.allowed_team_refs[0] if scope.allowed_team_refs else ""
            selected_team = team_ref or allowed_team
            if not selected_team or selected_team not in scope.allowed_team_refs:
                raise DashboardNotFound("team not found")
            visible_team_refs: tuple[str, ...] | None = (selected_team,)
        else:
            selected_team = team_ref
            visible_team_refs = (selected_team,) if selected_team else None
        records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=visible_team_refs,
            start_date=end_date - timedelta(days=3650),
            end_date=end_date,
        )
        teams = {
            team.ref: team.name
            for team in await self._repository.list_teams(
                tenant_id=actor.tenant_id
            )
        }
        members = {member.ref: member.name for member in records.members}
        latest_decisions = _latest_decisions(records)
        items = []
        for item in records.work_items:
            if not item.active:
                continue
            manager_decision = latest_decisions.get(item.ref)
            items.append(
                {
                    "ref": item.ref,
                    "title": item.title,
                    "team_name": teams.get(item.team_ref, ""),
                    "member_names": [
                        members[member_ref]
                        for member_ref in item.member_refs
                        if member_ref in members
                    ],
                    "status": item.status,
                    "status_label": _work_item_status_label(item.status),
                    "summary": item.summary,
                    "first_seen": item.first_seen.isoformat(),
                    "last_seen": item.last_seen.isoformat(),
                    "confidence": item.confidence,
                    "model_version": item.model_version,
                    "evaluated_at": (
                        item.evaluated_at.isoformat()
                        if item.evaluated_at
                        else None
                    ),
                    "manager_status": (
                        _decision_label(manager_decision.decision)
                        if manager_decision
                        else "待负责人处理"
                    ),
                    "manager_decision": (
                        _serialize_decision(manager_decision)
                        if manager_decision
                        else None
                    ),
                    "judgement_boundary": (
                        "文字重复本身不代表员工表现异常；"
                        "已识别的外部阻碍应单独展示。"
                    ),
                    "timeline": [
                        {
                            "date": entry.entry_date.isoformat(),
                            "member_name": members.get(
                                entry.member_ref,
                                "",
                            ),
                            "section": entry.section,
                            "quote": entry.quote,
                            "understood": {
                                "object": entry.object_text,
                                "action": entry.action,
                                "result": entry.result,
                                "next_step": entry.next_step,
                                "blocker": entry.blocker,
                            },
                        }
                        for entry in sorted(
                            item.entries,
                            key=lambda value: value.entry_date,
                        )
                    ],
                }
            )
        return {
            "date": end_date.isoformat(),
            "scope": {
                "role": scope.role,
                "selected_team_ref": selected_team,
            },
            "items": items,
        }

    async def record_manager_decision(
        self,
        *,
        actor: DashboardActor,
        target_type: str,
        target_ref: str,
        decision: str,
        note: str,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        allowed_decisions = {
            "normal",
            "waiting_external",
            "followup",
            "completed",
            "system_error",
        }
        if decision not in allowed_decisions:
            raise ValueError("unsupported manager decision")
        if target_type not in {"review_suggestion", "work_item"}:
            raise ValueError("unsupported management target")
        target = await self._repository.find_management_target(
            tenant_id=actor.tenant_id,
            target_type=target_type,
            target_ref=target_ref,
        )
        if target is None:
            raise DashboardNotFound("management target not found")
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=target.target_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        if (
            not scope.can_view_all_teams
            and target.team_ref not in scope.allowed_team_refs
        ):
            raise DashboardNotFound("management target not found")
        saved = await self._repository.save_manager_decision(
            ManagerDecisionRecord(
                ref=str(uuid4()),
                tenant_id=actor.tenant_id,
                target_type=target.target_type,
                target_ref=target.target_ref,
                team_ref=target.team_ref,
                decision=decision,  # type: ignore[arg-type]
                note=note,
                actor_user_id=actor.user_id,
                actor_role=scope.role,
                evidence_snapshot=target.evidence_snapshot,
                idempotency_key=idempotency_key,
                created_at=now,
            )
        )
        return _serialize_decision(saved)

    async def get_trends(
        self,
        *,
        actor: DashboardActor,
        end_date: date,
        period: str,
        team_ref: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        period_days = {"week": 7, "month": 30}
        if period not in period_days:
            raise ValueError("period must be week or month")
        scope = await self._repository.resolve_scope(
            actor=actor,
            on_date=end_date,
        )
        if scope is None:
            raise DashboardNotFound("dashboard access not found")
        if scope.role == "team_lead":
            allowed_team = scope.allowed_team_refs[0] if scope.allowed_team_refs else ""
            selected_team = team_ref or allowed_team
            if not selected_team or selected_team not in scope.allowed_team_refs:
                raise DashboardNotFound("team not found")
            visible_team_refs: tuple[str, ...] | None = (selected_team,)
        else:
            selected_team = team_ref
            visible_team_refs = (selected_team,) if selected_team else None
        start_date = end_date - timedelta(days=period_days[period] - 1)
        records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=visible_team_refs,
            start_date=start_date,
            end_date=end_date,
        )
        all_teams = await self._repository.list_teams(
            tenant_id=actor.tenant_id
        )
        visible_teams = [
            team
            for team in all_teams
            if visible_team_refs is None or team.ref in visible_team_refs
        ]
        team_metrics = [
            _period_team_metrics(
                records=records,
                team_ref=team.ref,
                team_name=team.name,
                start_date=start_date,
                end_date=end_date,
                now=now,
            )
            for team in visible_teams
        ]
        latest_decisions = _latest_decisions(records)
        coordination_needed = []
        for suggestion in records.suggestions:
            if not suggestion.active or suggestion.owner_level != "legal_head":
                continue
            manager_decision = latest_decisions.get(suggestion.ref)
            if manager_decision and _decision_suppresses_pending(
                manager_decision.decision
            ):
                continue
            team_name = next(
                (
                    team.name
                    for team in visible_teams
                    if team.ref == suggestion.team_ref
                ),
                "",
            )
            coordination_needed.append(
                {
                    "team_name": team_name,
                    "title": (
                        suggestion.work_item_title or "日报建议复核"
                    ),
                    "reason": suggestion.reason,
                    "support_needed": suggestion.support_needed,
                    "evidence_dates": [
                        value.isoformat()
                        for value in suggestion.compared_dates
                    ],
                    "model_version": suggestion.model_version,
                    "evaluated_at": (
                        suggestion.evaluated_at.isoformat()
                        if suggestion.evaluated_at
                        else None
                    ),
                }
            )
        long_running_items = [
            {
                "ref": item.ref,
                "team_name": next(
                    (
                        team.name
                        for team in visible_teams
                        if team.ref == item.team_ref
                    ),
                    "",
                ),
                "title": item.title,
                "status": item.status,
                "status_label": _work_item_status_label(item.status),
                "summary": item.summary,
                "first_seen": item.first_seen.isoformat(),
                "last_seen": item.last_seen.isoformat(),
                "confidence": item.confidence,
            }
            for item in records.work_items
            if item.active
            and item.status
            in {
                "no_new_progress",
                "plan_delayed",
                "unresolved_problem",
                "disappeared_without_completion",
            }
            and (
                item.ref not in latest_decisions
                or not _decision_suppresses_pending(
                    latest_decisions[item.ref].decision
                )
            )
        ]
        recurring: dict[str, int] = {}
        for suggestion in records.suggestions:
            if (
                suggestion.active
                and (
                    suggestion.ref not in latest_decisions
                    or not _decision_suppresses_pending(
                        latest_decisions[suggestion.ref].decision
                    )
                )
            ):
                recurring[suggestion.reason_type] = (
                    recurring.get(suggestion.reason_type, 0) + 1
                )
        responsibility_complete = all(
            bool(team["responsibility_data_complete"])
            for team in team_metrics
        )
        return {
            "period": {
                "type": period,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "scope": {
                "role": scope.role,
                "selected_team_ref": selected_team,
            },
            "responsibility_data_complete": responsibility_complete,
            "review_status": {
                "active_count": sum(
                    1
                    for suggestion in records.suggestions
                    if suggestion.active
                ),
                "pending_count": sum(
                    1
                    for suggestion in _pending_suggestions(records)
                ),
                "handled_count": sum(
                    1
                    for suggestion in records.suggestions
                    if suggestion.active
                    and suggestion.ref in latest_decisions
                ),
            },
            "teams": team_metrics,
            "recurring_problems": [
                {
                    "type": reason_type,
                    "label": _reason_type_label(reason_type),
                    "count": count,
                }
                for reason_type, count in sorted(
                    recurring.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "long_running_items": long_running_items,
            "recent_focus": [
                {
                    "team_name": next(
                        (
                            team.name
                            for team in visible_teams
                            if team.ref == item.team_ref
                        ),
                        "",
                    ),
                    "title": item.title,
                    "status_label": _work_item_status_label(item.status),
                }
                for item in records.work_items
                if item.active
            ],
            "coordination_needed": coordination_needed,
            "unsubmitted_trend": _unsubmitted_trend(
                records=records,
                start_date=start_date,
                end_date=end_date,
                now=now,
            ),
        }

    def render_briefing(self, trend: dict[str, Any]) -> str:
        period_label = (
            "周度" if trend["period"]["type"] == "week" else "月度"
        )
        lines = [
            f"法务日报负责人{period_label}简报",
            (
                f"{trend['period']['start_date']} 至 "
                f"{trend['period']['end_date']}"
            ),
            "",
            "一、提交与确认",
        ]
        for team in trend["teams"]:
            expected = (
                str(team["expected_count"])
                if team["expected_count"] is not None
                else "待核实"
            )
            lines.append(
                f"- {team['team_name']}：已提交 {team['submitted_count']}，"
                f"应交 {expected}，未最终确认 "
                f"{team['not_finally_confirmed_count']}，延迟提交 "
                f"{team['late_submitted_count']}。"
            )
        if not trend["responsibility_data_complete"]:
            lines.extend(
                [
                    "",
                    "数据声明：责任数据不完整；请假、休假、入离职或"
                    "当天无需提交信息缺失时，应交与未交人数不得视为确定值。",
                ]
            )
        lines.extend(["", "二、近期工作重点"])
        if trend["recent_focus"]:
            for item in trend["recent_focus"]:
                lines.append(
                    f"- {item['team_name']}｜{item['title']}："
                    f"{item['status_label']}。"
                )
        else:
            lines.append("- 当前没有可汇总的近期重点。")

        lines.extend(["", "三、反复出现的问题"])
        if trend["recurring_problems"]:
            for item in trend["recurring_problems"]:
                lines.append(
                    f"- {item['label']}：{item['count']} 次。"
                )
        else:
            lines.append("- 当前没有反复出现的复核线索。")

        lines.extend(["", "四、长期未推进事项"])
        if not trend["long_running_items"]:
            lines.append("- 当前没有待复核的长期未推进事项。")
        for item in trend["long_running_items"]:
            lines.append(
                f"- {item['team_name']}｜{item['title']}："
                f"{item['status_label']}。{item['summary']}"
            )
        lines.extend(["", "五、需要法务负责人协调"])
        if trend["coordination_needed"]:
            for item in trend["coordination_needed"]:
                lines.append(
                    f"- {item['team_name']}｜{item['title']}："
                    f"{item['support_needed']}（原因：{item['reason']}）"
                )
        else:
            lines.append("- 当前没有待协调事项。")
        review_status = trend.get("review_status") or {}
        lines.extend(
            [
                "",
                "六、建议复核处理情况",
                (
                    f"- 当前建议 {review_status.get('active_count', 0)} 条，"
                    f"待处理 {review_status.get('pending_count', 0)} 条，"
                    f"已有负责人记录 {review_status.get('handled_count', 0)} 条。"
                ),
            ]
        )
        lines.extend(
            [
                "",
                "说明：本简报不包含个人综合评价，不自动发送消息；"
                "所有提示均应回到原日报证据。",
            ]
        )
        return "\n".join(lines)


def _records_for_team(
    records: DashboardRecords,
    team_ref: str,
) -> DashboardRecords:
    return DashboardRecords(
        members=tuple(
            member
            for member in records.members
            if member.team_ref == team_ref
        ),
        obligations=tuple(
            obligation
            for obligation in records.obligations
            if obligation.team_ref == team_ref
        ),
        reports=tuple(
            report
            for report in records.reports
            if report.team_ref == team_ref
        ),
        suggestions=tuple(
            suggestion
            for suggestion in records.suggestions
            if suggestion.team_ref == team_ref
        ),
        work_items=tuple(
            item
            for item in records.work_items
            if item.team_ref == team_ref
        ),
        decisions=tuple(
            decision
            for decision in records.decisions
            if decision.team_ref == team_ref
        ),
    )


def records_for_team(
    records: DashboardRecords,
    team_ref: str,
) -> DashboardRecords:
    """Return the existing read projection narrowed to one team."""

    return _records_for_team(records, team_ref)


def _overview_metrics(
    *,
    records: DashboardRecords,
    now: datetime,
) -> dict[str, int | bool | None]:
    obligations = {
        (item.member_ref, item.report_date): item
        for item in records.obligations
    }
    reports = {
        (item.member_ref, item.report_date): item
        for item in records.reports
    }
    unknown_responsibility = 0
    known_required = []
    exempt_count = 0
    for member in records.members:
        member_obligations = [
            item
            for (member_ref, _), item in obligations.items()
            if member_ref == member.ref
        ]
        obligation = member_obligations[0] if member_obligations else None
        if obligation is None or not obligation.data_complete:
            unknown_responsibility += 1
            continue
        if obligation.required:
            known_required.append(obligation)
        else:
            exempt_count += 1

    completed_reports = [
        report
        for report in records.reports
        if report.status == "completed"
    ]
    pending_reports = [
        report
        for report in records.reports
        if report.status == "pending_confirmation"
    ]
    user_confirmed_count = sum(
        1 for report in completed_reports if report.confirmed_by_user
    )
    auto_submitted_count = sum(
        1
        for report in completed_reports
        if report.confirmation_type == "auto_submitted_timeout"
    )
    not_finally_confirmed_count = sum(
        1
        for report in (*completed_reports, *pending_reports)
        if not report.confirmed_by_user
    )

    missing_required = []
    for obligation in known_required:
        report = reports.get((obligation.member_ref, obligation.report_date))
        if report is None or report.status not in {
            "completed",
            "pending_confirmation",
        }:
            missing_required.append(obligation)
    any_deadline_reached = any(
        item.deadline_at is not None and now >= item.deadline_at
        for item in known_required
    )
    overdue_known_count = sum(
        1
        for item in missing_required
        if item.deadline_at is not None and now >= item.deadline_at
    )
    outstanding_known_count = sum(
        1
        for item in missing_required
        if item.deadline_at is None or now < item.deadline_at
    )
    responsibility_complete = unknown_responsibility == 0
    return {
        "responsibility_data_complete": responsibility_complete,
        "expected_count": len(known_required) if responsibility_complete else None,
        "expected_known_count": len(known_required),
        "unknown_responsibility_count": unknown_responsibility,
        "exempt_count": exempt_count,
        "submitted_count": len(completed_reports),
        "user_confirmed_count": user_confirmed_count,
        "auto_submitted_count": auto_submitted_count,
        "pending_confirmation_count": len(pending_reports),
        "not_finally_confirmed_count": not_finally_confirmed_count,
        "outstanding_count": (
            outstanding_known_count
            if responsibility_complete
            else None
        ),
        "outstanding_known_count": outstanding_known_count,
        "overdue_count": (
            overdue_known_count
            if responsibility_complete and any_deadline_reached
            else None
        ),
        "overdue_known_count": overdue_known_count,
        "review_suggested_count": _pending_suggestion_count(records),
        "attention_member_count": len(
            {
                suggestion.member_ref
                for suggestion in _pending_suggestions(records)
            }
        ),
        "stalled_item_count": _pending_stalled_item_count(records),
    }


def overview_metrics(
    *,
    records: DashboardRecords,
    now: datetime,
) -> dict[str, int | bool | None]:
    """Reuse dashboard submission metrics without changing dashboard access rules."""

    return _overview_metrics(records=records, now=now)


def management_review_actions(
    *,
    records: DashboardRecords,
    teams: dict[str, str],
    report_date: date,
    now: datetime,
) -> tuple[dict[str, Any], ...]:
    """Return unresolved model-backed actions using the dashboard's decisions."""

    return tuple(
        action
        for action in _management_actions(
            records=records,
            teams=teams,
            report_date=report_date,
            now=now,
        )
        if action.get("target_type") in {"review_suggestion", "work_item"}
    )


def _member_status(
    *,
    obligation: Any,
    report: Any,
    now: datetime,
) -> tuple[str, str]:
    _, status_label, confirmation_label = classify_submission(
        obligation=obligation,
        report=report,
        now=now,
    )
    return status_label, confirmation_label


def classify_submission(
    *,
    obligation: Any,
    report: Any,
    now: datetime,
) -> tuple[str, str, str]:
    """Return one stable state code plus the existing user-facing labels."""

    if report is not None and report.status == "completed":
        if report.confirmation_type == "auto_submitted_timeout":
            return (
                "submitted",
                "已提交 · 自动提交",
                "超时自动提交",
            )
        if report.confirmed_by_user:
            return (
                "submitted",
                "已提交 · 最终确认",
                "用户最终确认",
            )
        if report.confirmation_type == "admin_confirmed":
            return (
                "submitted",
                "已提交 · 管理确认",
                "管理员确认",
            )
        return (
            "submitted",
            "已提交 · 未最终确认",
            "尚未最终确认",
        )
    if report is not None and report.status == "pending_confirmation":
        return (
            "pending_confirmation",
            "未最终确认",
            "尚未最终确认",
        )
    if obligation is None or not obligation.data_complete:
        return (
            "responsibility_unknown",
            "是否应交待核实",
            "责任数据缺失",
        )
    if not obligation.required:
        reason = obligation.reason or "当天无需提交"
        return "exempt", f"无需提交 · {reason}", reason
    if obligation.deadline_at is not None and now >= obligation.deadline_at:
        return "overdue", "未交", "截止后未交"
    return "not_yet_due", "尚未提交", "截止前尚未提交"


def _section_completeness(report: Any) -> dict[str, Any]:
    if report is None:
        return {
            "known": False,
            "complete": False,
            "today_work": False,
            "problems": False,
            "tomorrow_plan": False,
            "missing_sections": [],
        }
    status = report.section_status or {}
    fields = {
        "today_work": bool(report.today_work)
        or bool(status.get("today_work"))
        or bool(status.get("today_work_acknowledged_empty")),
        "problems": bool(report.problems)
        or bool(status.get("problems"))
        or bool(status.get("problems_acknowledged_empty")),
        "tomorrow_plan": bool(report.tomorrow_plan)
        or bool(status.get("tomorrow_plan"))
        or bool(status.get("tomorrow_plan_acknowledged_empty")),
    }
    labels = {
        "today_work": "今日工作",
        "problems": "问题风险",
        "tomorrow_plan": "明日计划",
    }
    return {
        "known": True,
        "complete": all(fields.values()),
        **fields,
        "missing_sections": [
            labels[field]
            for field, completed in fields.items()
            if not completed
        ],
    }


def _date_label(value: date) -> str:
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return f"{value.month} 月 {value.day} 日（{weekdays[value.weekday()]}）"


def _serialize_suggestion(
    suggestion: Any,
    *,
    manager_decision: ManagerDecisionRecord | None = None,
) -> dict[str, Any]:
    return {
        "ref": suggestion.ref,
        "label": "建议复核",
        "reason": suggestion.reason,
        "evidence": [
            {
                "date": evidence.evidence_date.isoformat(),
                "section": evidence.section,
                "quote": evidence.quote,
            }
            for evidence in suggestion.evidence
        ],
        "compared_dates": [
            compared_date.isoformat()
            for compared_date in suggestion.compared_dates
        ],
        "confidence": suggestion.confidence,
        "model_version": suggestion.model_version,
        "evaluated_at": (
            suggestion.evaluated_at.isoformat()
            if suggestion.evaluated_at
            else None
        ),
        "manager_status": (
            _decision_label(manager_decision.decision)
            if manager_decision
            else "待负责人处理"
        ),
        "manager_decision": (
            _serialize_decision(manager_decision)
            if manager_decision
            else None
        ),
    }


def _management_actions(
    *,
    records: DashboardRecords,
    teams: dict[str, str],
    report_date: date,
    now: datetime,
) -> list[dict[str, Any]]:
    members = {member.ref: member for member in records.members}
    latest_decisions = _latest_decisions(records)
    actions = _objective_management_actions(
        records=records,
        teams=teams,
        report_date=report_date,
        now=now,
    )
    for suggestion in records.suggestions:
        if not suggestion.active:
            continue
        manager_decision = latest_decisions.get(suggestion.ref)
        if manager_decision and _decision_suppresses_pending(
            manager_decision.decision
        ):
            continue
        category = (
            "department_support"
            if suggestion.owner_level == "legal_head"
            else "team_followup"
        )
        actions.append(
            {
                "target_type": "review_suggestion",
                "target_ref": suggestion.ref,
                "source_type": "review_suggestion",
                "category": category,
                "category_label": (
                    "需要协调支撑"
                    if category == "department_support"
                    else "事项需跟进"
                ),
                "team_name": teams.get(suggestion.team_ref, ""),
                "member_name": (
                    members[suggestion.member_ref].name
                    if suggestion.member_ref in members
                    else ""
                ),
                "title": suggestion.work_item_title or "日报建议复核",
                "reason": suggestion.reason,
                "support_needed": suggestion.support_needed,
                "compared_dates": [
                    compared_date.isoformat()
                    for compared_date in suggestion.compared_dates
                ],
                "evidence": [
                    {
                        "date": evidence.evidence_date.isoformat(),
                        "section": evidence.section,
                        "quote": evidence.quote,
                    }
                    for evidence in suggestion.evidence
                ],
                "facts": [],
                "confidence": suggestion.confidence,
                "model_version": suggestion.model_version,
                "evaluated_at": (
                    suggestion.evaluated_at.isoformat()
                    if suggestion.evaluated_at
                    else None
                ),
                "manager_status": (
                    _decision_label(manager_decision.decision)
                    if manager_decision
                    else "待负责人处理"
                ),
                "manager_decision": (
                    _serialize_decision(manager_decision)
                    if manager_decision
                    else None
                ),
                "can_record_decision": True,
            }
        )
    represented_items = {
        (suggestion.team_ref, suggestion.work_item_title)
        for suggestion in records.suggestions
        if suggestion.active and suggestion.work_item_title
    }
    stalled_statuses = {
        "no_new_progress",
        "plan_delayed",
        "unresolved_problem",
        "disappeared_without_completion",
    }
    for item in records.work_items:
        if (
            not item.active
            or item.status not in stalled_statuses
            or not item.entries
            or (item.team_ref, item.title) in represented_items
        ):
            continue
        manager_decision = latest_decisions.get(item.ref)
        if manager_decision and _decision_resolves_attention(
            manager_decision.decision
        ):
            continue
        member_names = [
            members[member_ref].name
            for member_ref in item.member_refs
            if member_ref in members
        ]
        actions.append(
            {
                "target_type": "work_item",
                "target_ref": item.ref,
                "source_type": "work_item",
                "category": "team_followup",
                "category_label": "长期事项需跟进",
                "team_name": teams.get(item.team_ref, ""),
                "member_name": "、".join(member_names),
                "title": item.title,
                "reason": item.summary,
                "support_needed": _work_item_support_needed(item.status),
                "compared_dates": sorted(
                    {
                        entry.entry_date.isoformat()
                        for entry in item.entries
                    }
                ),
                "evidence": [
                    {
                        "date": entry.entry_date.isoformat(),
                        "section": entry.section,
                        "quote": entry.quote,
                    }
                    for entry in sorted(
                        item.entries,
                        key=lambda value: value.entry_date,
                    )
                ],
                "facts": [],
                "confidence": item.confidence,
                "model_version": item.model_version,
                "evaluated_at": (
                    item.evaluated_at.isoformat()
                    if item.evaluated_at
                    else None
                ),
                "manager_status": (
                    _decision_label(manager_decision.decision)
                    if manager_decision
                    else "待负责人处理"
                ),
                "manager_decision": (
                    _serialize_decision(manager_decision)
                    if manager_decision
                    else None
                ),
                "can_record_decision": True,
            }
        )
    return actions


def _objective_management_actions(
    *,
    records: DashboardRecords,
    teams: dict[str, str],
    report_date: date,
    now: datetime,
) -> list[dict[str, Any]]:
    reports = {
        (report.member_ref, report.report_date): report
        for report in records.reports
    }
    obligations = {
        (item.member_ref, item.report_date): item
        for item in records.obligations
    }
    actions: list[dict[str, Any]] = []

    def append_action(
        *,
        source_type: str,
        member: Any,
        title: str,
        reason: str,
        support_needed: str,
        manager_status: str,
        facts: list[dict[str, str]],
        evidence: list[dict[str, str]],
    ) -> None:
        actions.append(
            {
                "target_type": "objective_fact",
                "target_ref": (
                    f"objective-{source_type}-{member.ref}-"
                    f"{report_date.isoformat()}"
                ),
                "source_type": source_type,
                "category": "today_confirmation",
                "category_label": "今日需确认",
                "team_name": teams.get(member.team_ref, ""),
                "member_name": member.name,
                "title": title,
                "reason": reason,
                "support_needed": support_needed,
                "compared_dates": [report_date.isoformat()],
                "evidence": evidence,
                "facts": facts,
                "confidence": None,
                "model_version": None,
                "evaluated_at": now.isoformat(),
                "manager_status": manager_status,
                "manager_decision": None,
                "can_record_decision": False,
            }
        )

    for member in records.members:
        report = reports.get((member.ref, report_date))
        obligation = obligations.get((member.ref, report_date))
        evidence = _report_source_evidence(report, report_date)
        if (
            report is not None
            and report.status in {"completed", "pending_confirmation"}
            and not report.confirmed_by_user
        ):
            append_action(
                source_type="pending_confirmation",
                member=member,
                title="日报尚未最终确认",
                reason="当天日报已有内容，但尚未由本人最终确认。",
                support_needed=(
                    "请团队负责人关注最终确认状态；驾驶舱不会代为确认或修改日报。"
                ),
                manager_status="未最终确认",
                facts=[
                    {"label": "日报状态", "value": "未最终确认"},
                    {
                        "label": "确认方式",
                        "value": _confirmation_type_label(report),
                    },
                ],
                evidence=evidence,
            )
        if report is not None:
            completeness = _section_completeness(report)
            if completeness["known"] and not completeness["complete"]:
                missing = "、".join(completeness["missing_sections"])
                append_action(
                    source_type="missing_section",
                    member=member,
                    title="日报栏目需要确认",
                    reason=f"当天日报缺少栏目：{missing}。",
                    support_needed=(
                        "请团队负责人结合原文确认栏目是否确需补充；"
                        "系统不据此评价员工表现。"
                    ),
                    manager_status="栏目缺失",
                    facts=[
                        {"label": "缺少栏目", "value": missing},
                    ],
                    evidence=evidence,
                )
        submitted = (
            report is not None
            and report.status in {"completed", "pending_confirmation"}
        )
        if (
            obligation is not None
            and obligation.data_complete
            and obligation.required
            and not submitted
            and obligation.deadline_at is not None
            and now >= obligation.deadline_at
        ):
            append_action(
                source_type="overdue_submission",
                member=member,
                title="截止后仍未提交日报",
                reason="已到可靠截止时间，系统仍未查询到有效提交。",
                support_needed=(
                    "请团队负责人核实提交情况；本版不会自动催交或发送消息。"
                ),
                manager_status="截止后未交",
                facts=[
                    {
                        "label": "截止时间",
                        "value": obligation.deadline_at.strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                    },
                    {"label": "提交事实", "value": "未查询到有效提交"},
                ],
                evidence=evidence,
            )
    return actions


def _report_source_evidence(
    report: Any,
    report_date: date,
) -> list[dict[str, str]]:
    if report is None:
        return []
    quote = report.raw_input.strip()
    if not quote:
        quote = "\n".join(
            (
                *report.today_work,
                *report.problems,
                *report.tomorrow_plan,
            )
        ).strip()
    if not quote:
        return []
    return [
        {
            "date": report_date.isoformat(),
            "section": "员工日报原文",
            "quote": quote,
        }
    ]


def _confirmation_type_label(report: Any) -> str:
    if report.confirmed_by_user:
        return "用户最终确认"
    if report.confirmation_type == "auto_submitted_timeout":
        return "超时自动提交"
    if report.confirmation_type == "admin_confirmed":
        return "管理员确认"
    if report.status == "pending_confirmation":
        return "尚未最终确认"
    return "未记录"


def _work_item_status_label(status: str) -> str:
    labels = {
        "normal_progress": "正常推进",
        "no_new_progress": "连续多日无新增进展",
        "plan_delayed": "明日计划反复延期",
        "unresolved_problem": "问题长期未解决",
        "disappeared_without_completion": "事项消失但无完成说明",
        "completed": "已完成",
        "waiting_external": "等待外部反馈",
        "normal_continuing": "正常持续事项",
    }
    return labels.get(status, "待复核")


def _latest_decisions(records: DashboardRecords) -> dict[str, ManagerDecisionRecord]:
    latest: dict[str, ManagerDecisionRecord] = {}
    for decision in sorted(
        records.decisions,
        key=lambda item: item.created_at,
    ):
        latest[decision.target_ref] = decision
    return latest


def _decision_label(decision: str) -> str:
    labels = {
        "normal": "正常，无需提醒",
        "waiting_external": "等待外部反馈",
        "followup": "需要跟进",
        "completed": "已完成",
        "system_error": "系统识别错误",
    }
    return labels.get(decision, "待负责人处理")


def _serialize_decision(decision: ManagerDecisionRecord) -> dict[str, Any]:
    return {
        "ref": decision.ref,
        "target_type": decision.target_type,
        "target_ref": decision.target_ref,
        "decision": decision.decision,
        "decision_label": _decision_label(decision.decision),
        "note": decision.note,
        "actor_name": decision.actor_name or "负责人",
        "actor_role": decision.actor_role,
        "created_at": decision.created_at.isoformat(),
    }


def _decision_suppresses_pending(decision: str) -> bool:
    return decision in {"normal", "completed", "system_error"}


def _pending_suggestion_count(records: DashboardRecords) -> int:
    return len(_pending_suggestions(records))


def _pending_suggestions(records: DashboardRecords) -> tuple[Any, ...]:
    latest_decisions = _latest_decisions(records)
    return tuple(
        suggestion
        for suggestion in records.suggestions
        if suggestion.active
        and (
            suggestion.ref not in latest_decisions
            or not _decision_suppresses_pending(
                latest_decisions[suggestion.ref].decision
            )
        )
    )


def _pending_stalled_item_count(records: DashboardRecords) -> int:
    latest_decisions = _latest_decisions(records)
    stalled_statuses = {
        "no_new_progress",
        "plan_delayed",
        "unresolved_problem",
        "disappeared_without_completion",
    }
    return sum(
        1
        for item in records.work_items
        if item.active
        and item.status in stalled_statuses
        and (
            item.ref not in latest_decisions
            or not _decision_resolves_attention(
                latest_decisions[item.ref].decision
            )
        )
    )


def _decision_resolves_attention(decision: str) -> bool:
    return decision in {
        "normal",
        "completed",
        "system_error",
    }


def _work_item_support_needed(status: str) -> str:
    messages = {
        "no_new_progress": "请团队负责人确认是否存在合理阻碍，并明确下一步。",
        "plan_delayed": "请团队负责人确认延期原因和新的完成时间。",
        "unresolved_problem": "请团队负责人判断是否需要协调资源或升级处理。",
        "disappeared_without_completion": "请团队负责人确认事项是否完成或仍需继续跟进。",
    }
    return messages.get(status, "请团队负责人结合原文确认后续安排。")


def _period_team_metrics(
    *,
    records: DashboardRecords,
    team_ref: str,
    team_name: str,
    start_date: date,
    end_date: date,
    now: datetime,
) -> dict[str, Any]:
    members = [
        member for member in records.members if member.team_ref == team_ref
    ]
    obligations = [
        item
        for item in records.obligations
        if item.team_ref == team_ref and item.data_complete
    ]
    reports = [
        item for item in records.reports if item.team_ref == team_ref
    ]
    required_obligations = [item for item in obligations if item.required]
    completed_reports = [
        report for report in reports if report.status == "completed"
    ]
    pending_reports = [
        report
        for report in reports
        if report.status == "pending_confirmation"
    ]
    period_days = (end_date - start_date).days + 1
    responsibility_complete = (
        len(obligations) == len(members) * period_days
        if members
        else True
    )
    report_by_member_date = {
        (report.member_ref, report.report_date): report
        for report in reports
    }
    overdue_count = sum(
        1
        for obligation in required_obligations
        if obligation.deadline_at is not None
        and now >= obligation.deadline_at
        and (
            (obligation.member_ref, obligation.report_date)
            not in report_by_member_date
            or report_by_member_date[
                (obligation.member_ref, obligation.report_date)
            ].status
            not in {"completed", "pending_confirmation"}
        )
    )
    late_submitted_count = sum(
        1
        for obligation in required_obligations
        if obligation.deadline_at is not None
        and (
            report := report_by_member_date.get(
                (obligation.member_ref, obligation.report_date)
            )
        )
        is not None
        and report.status in {"completed", "pending_confirmation"}
        and report.submitted_at is not None
        and report.submitted_at > obligation.deadline_at
    )
    return {
        "team_ref": team_ref,
        "team_name": team_name,
        "responsibility_data_complete": responsibility_complete,
        "expected_count": (
            len(required_obligations) if responsibility_complete else None
        ),
        "expected_known_count": len(required_obligations),
        "submitted_count": len(completed_reports),
        "pending_confirmation_count": len(pending_reports),
        "not_finally_confirmed_count": sum(
            1
            for report in (*completed_reports, *pending_reports)
            if not report.confirmed_by_user
        ),
        "auto_submitted_count": sum(
            1
            for report in completed_reports
            if report.confirmation_type == "auto_submitted_timeout"
        ),
        "late_submitted_count": late_submitted_count,
        "overdue_known_count": overdue_count,
    }


def _unsubmitted_trend(
    *,
    records: DashboardRecords,
    start_date: date,
    end_date: date,
    now: datetime,
) -> list[dict[str, Any]]:
    reports = {
        (report.member_ref, report.report_date): report
        for report in records.reports
    }
    trend = []
    current_date = start_date
    while current_date <= end_date:
        known_obligations = [
            item
            for item in records.obligations
            if item.report_date == current_date
            and item.data_complete
        ]
        obligations = [
            item for item in known_obligations if item.required
        ]
        missing = [
            obligation
            for obligation in obligations
            if (
                (obligation.member_ref, current_date) not in reports
                or reports[
                    (obligation.member_ref, current_date)
                ].status
                not in {"completed", "pending_confirmation"}
            )
        ]
        overdue = sum(
            1
            for obligation in missing
            if obligation.deadline_at is not None
            and now >= obligation.deadline_at
        )
        outstanding = sum(
            1
            for obligation in missing
            if (
                obligation.deadline_at is None
                or now < obligation.deadline_at
            )
        )
        late_submitted = sum(
            1
            for obligation in obligations
            if obligation.deadline_at is not None
            and (
                report := reports.get(
                    (obligation.member_ref, current_date)
                )
            )
            is not None
            and report.status in {"completed", "pending_confirmation"}
            and report.submitted_at is not None
            and report.submitted_at > obligation.deadline_at
        )
        trend.append(
            {
                "date": current_date.isoformat(),
                "submitted_count": sum(
                    1
                    for report in records.reports
                    if report.report_date == current_date
                    and report.status == "completed"
                ),
                "late_submitted_count": late_submitted,
                "outstanding_count": outstanding,
                "overdue_count": overdue,
                "responsibility_known_count": len(obligations),
                "responsibility_data_complete": (
                    len(known_obligations) == len(records.members)
                ),
            }
        )
        current_date += timedelta(days=1)
    return trend


def _reason_type_label(reason_type: str) -> str:
    labels = {
        "missing_object_action_or_result": "缺少具体工作对象、动作或结果",
        "no_new_progress": "连续多日没有新增进展",
        "plan_repeatedly_delayed": "明日计划连续延期",
        "unresolved_problem": "问题长期存在但没有处理结果",
        "missing_section": "栏目缺失",
        "work_plan_disconnect": "工作与下一步计划无法衔接",
        "too_general_to_assess": "内容过于笼统，无法判断实际进展",
        "disappeared_without_completion": "事项消失但没有完成说明",
    }
    return labels.get(reason_type, "建议复核")
