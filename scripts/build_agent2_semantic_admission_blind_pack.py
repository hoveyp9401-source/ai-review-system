from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from uuid import NAMESPACE_URL, uuid5


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.semantic_admission_blind import (  # noqa: E402
    SemanticAdmissionBlindPack,
)
from app.agent2.evaluation.semantic_admission_scoring import (  # noqa: E402
    SealedSemanticAdmissionLabels,
)


TENANT_ID = "sandbox-agent2-phase2-20260711"
ACTOR_ID = "blind-canary-user-a"
USER_ID = f"{TENANT_ID}:{ACTOR_ID}"
NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
PACK_ID = "agent2-semantic-admission-adversarial-20260714-v1"


def build_artifacts(output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    scenarios = _scenarios()
    cases = [_blind_case(index, scenario) for index, scenario in enumerate(scenarios, start=1)]
    pack = SemanticAdmissionBlindPack.from_mapping(
        {
            "schema_version": "agent2.semantic_admission_blind_input.v1",
            "pack_id": PACK_ID,
            "cases": cases,
        }
    )
    labels = SealedSemanticAdmissionLabels.seal(
        input_pack=pack,
        labels=tuple(
            _label(case["case_id"], scenario)
            for case, scenario in zip(cases, scenarios, strict=True)
        ),
    )
    manifest = {
        "schema_version": "agent2.semantic_admission_blind_manifest.v1",
        "pack_id": pack.pack_id,
        "input_pack_digest": pack.digest,
        "case_count": len(pack.cases),
        "categories": [scenario["category"] for scenario in scenarios],
        "required_coverage": {
            "old_date_daily": True,
            "no_other_risk": True,
            "weekly_context_hijack": True,
            "generic_case_material": True,
            "unique_case": True,
            "ambiguous_case": True,
            "travel": True,
            "cross_domain_sibling": True,
            "negation": True,
            "hypothetical": True,
            "quotation": True,
            "multi_intent": True,
        },
        "label_classification": "machine_candidate",
        "human_review_state": "pending_human_review",
        "acceptance_eligible": False,
        "sealed_label_hash": labels.seal_hash,
    }
    _write_json(output / "blind_input.json", pack.as_mapping())
    _write_json(output / "sealed_labels.json", labels.as_mapping())
    _write_json(output / "manifest.json", manifest)
    return manifest


def _scenarios() -> list[dict[str, Any]]:
    admitted_daily = [_decision("report", "capture_daily_event", "admitted")]
    admitted_case = [_decision("case", "record_case_progress", "admitted")]
    admitted_travel = [_decision("travel", "record_travel_event", "admitted")]
    ambiguous_case = [
        _decision(
            "case",
            "record_case_progress",
            "blocked",
            "case_reference_ambiguous",
        )
    ]
    return [
        _scenario(
            "old_date_daily",
            "补充昨天的日报：联系了法院确认排期",
            [_decision("report", "capture_daily_event", "blocked")],
            goal="daily_report",
        ),
        _scenario("no_other_risk", "没其他风险", [], goal="daily_report"),
        _scenario(
            "weekly_entry_not_chat",
            "我想写周报",
            [_decision("report", "query_periodic_report", "admitted")],
            goal="daily_report",
        ),
        _scenario(
            "weekly_context_hijack",
            "继续刚才的周报",
            [_decision("report", "query_periodic_report", "admitted")],
            goal="daily_report",
        ),
        _scenario(
            "weekly_append",
            "周报记一条：本周完成合同审核",
            [_decision("report", "capture_report_event", "admitted")],
            goal="weekly_report",
        ),
        _scenario(
            "generic_case_material",
            "案件材料已经整理好了",
            [
                _decision(
                    "case",
                    "record_case_progress",
                    "blocked",
                    "case_reference_not_uniquely_authorized",
                )
            ],
        ),
        _scenario("unique_case_alias", "云璟府案今天联系法院推进了", admitted_case),
        _scenario("unique_case_number", "（2026）苏01执100号今天完成财产查控", admitted_case),
        _scenario("ambiguous_case_alias", "云璟府今天补交了证据", ambiguous_case, selection=1),
        _scenario("travel_asserted", "明天去南京出差，处理云璟府项目", admitted_travel),
        _scenario("travel_missing_time", "去南京出差处理项目", [_decision("travel", "record_travel_event", "information_required")], info=1),
        _scenario("travel_missing_destination", "明天出去出差", [_decision("travel", "record_travel_event", "blocked")]),
        _scenario("travel_negated", "明天不去南京出差了", []),
        _scenario("travel_hypothetical", "如果明天去南京出差，就顺便见客户", []),
        _scenario("travel_quotation", "刘聪说‘明天去南京出差’", []),
        _scenario("case_negated", "云璟府案今天没有联系法院", []),
        _scenario("case_hypothetical", "如果法院回复了，云璟府案再记一条进展", []),
        _scenario("case_quotation", "刘聪说‘云璟府案今天联系了法院’", []),
        _scenario(
            "daily_and_travel",
            "日报记：今天完成合同审核；明天去南京出差",
            admitted_daily + admitted_travel,
        ),
        _scenario(
            "daily_and_unique_case",
            "日报记：今天完成合同审核；云璟府案今天联系法院推进了",
            admitted_daily + admitted_case,
        ),
        _scenario(
            "case_and_travel",
            "云璟府案今天联系法院推进了；明天去南京出差",
            admitted_case + admitted_travel,
        ),
        _scenario(
            "three_domain_siblings",
            "日报记：今天完成合同审核；云璟府案今天联系法院推进了；明天去南京出差",
            admitted_daily + admitted_case + admitted_travel,
        ),
        _scenario(
            "ambiguous_sibling_isolated",
            "日报记：今天完成合同审核；云璟府今天补交了证据；明天去南京出差",
            admitted_daily + ambiguous_case + admitted_travel,
            selection=1,
        ),
        _scenario(
            "risk_noop_plus_travel",
            "没其他风险；明天去南京出差",
            admitted_travel,
            goal="daily_report",
        ),
        _scenario(
            "old_date_and_current_daily",
            "昨天联系了法院，补到昨天日报；日报再记：今天完成合同审核",
            [_decision("report", "capture_daily_event", "blocked")] + admitted_daily,
            goal="daily_report",
        ),
        _scenario(
            "weekly_case_interruption",
            "周报记一条：本周完成合同审核；云璟府案今天联系法院推进了",
            [_decision("report", "capture_report_event", "admitted")] + admitted_case,
            goal="weekly_report",
        ),
    ]


def _scenario(
    category: str,
    text: str,
    decisions: list[dict[str, Any]],
    *,
    goal: str = "",
    info: int = 0,
    selection: int = 0,
) -> dict[str, Any]:
    return {
        "category": category,
        "text": text,
        "goal": goal,
        "decisions": decisions,
        "ticket_count": sum(1 for item in decisions if item["status"] == "admitted" and item["operation"] not in {"query_daily_report", "query_periodic_report"}),
        "information_pending_count": info,
        "selection_request_count": selection,
    }


def _decision(
    domain: str,
    operation: str,
    status: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    result = {"domain": domain, "operation": operation, "status": status}
    if reason_code:
        result["reason_code"] = reason_code
    return result


def _blind_case(index: int, scenario: dict[str, Any]) -> dict[str, Any]:
    category = str(scenario["category"])
    conversation_id = f"blind-admission-{index:02d}-{category}"
    report_id = str(uuid5(NAMESPACE_URL, f"semantic-admission-report:{category}"))
    weekly_id = str(uuid5(NAMESPACE_URL, f"semantic-admission-weekly:{category}"))
    current = {
        "report_id": report_id,
        "report_date": "2026-07-14",
        "version": 3,
        "status": "collecting",
        "items": [],
    }
    previous = {
        "report_id": str(uuid5(NAMESPACE_URL, f"semantic-admission-yesterday:{category}")),
        "report_date": "2026-07-13",
        "version": 2,
        "status": "collecting",
        "items": [],
    }
    resources = {
        "timezone": "Asia/Shanghai",
        "daily_policy": {
            "current_report_date": "2026-07-14",
            "historical_mutation_allowed": False,
        },
        "daily_draft": {key: value for key, value in current.items() if key != "report_date"},
        "daily_reports": [current, previous],
        "active_tasks": [
            {
                "workflow": "daily_report",
                "task_id": report_id,
                "status": "collecting",
                "metadata": {"report_date": "2026-07-14"},
            },
            {
                "workflow": "weekly_report",
                "task_id": weekly_id,
                "status": "collecting",
                "metadata": {"report_type": "weekly", "period_key": "2026-W29"},
            },
        ],
        "periodic_report": {
            "report_id": weekly_id,
            "owner_user_id": ACTOR_ID,
            "report_type": "weekly",
            "period_key": "2026-W29",
            "version": 4,
            "status": "collecting",
            "sections": {
                "accomplishments": [],
                "risks": [],
                "next_plan": [],
                "metrics": [],
            },
            "item_ids": {
                "accomplishments": [],
                "risks": [],
                "next_plan": [],
                "metrics": [],
            },
        },
        "visible_cases": _visible_cases(),
    }
    state = None
    if scenario["goal"]:
        state = {
            "user_id": USER_ID,
            "conversation_id": conversation_id,
            "version": 7,
            "current_goal": {
                "intent": scenario["goal"],
                "entity_ids": [],
                "source_context_id": f"context-{index:02d}",
            },
            "goal_stack": [],
            "current_entities": [],
            "recent_context": [],
            "pending": [],
            "selection_pending": [],
            "user_constraints": {},
        }
    return {
        "case_id": f"sa-blind-{index:02d}-{category}",
        "scope": {
            "tenant_id": TENANT_ID,
            "user_id": USER_ID,
            "actor_user_id": ACTOR_ID,
            "conversation_id": conversation_id,
            "message_id": f"blind-message-{index:02d}",
            "occurred_at": NOW.isoformat(),
            "channel": "blind_replay",
        },
        "raw_text": scenario["text"],
        "state": state,
        "resources": resources,
    }


def _visible_cases() -> list[dict[str, Any]]:
    return [
        {
            "case_id": str(uuid5(NAMESPACE_URL, "semantic-admission-case-alpha")),
            "case_number": "（2026）苏01执100号",
            "case_name": "云璟府物业服务合同执行案",
            "external_case_id": "BGGL-2026-0100",
            "confirmed_aliases": ["云璟府案", "云璟府"],
            "version": 7,
        },
        {
            "case_id": str(uuid5(NAMESPACE_URL, "semantic-admission-case-beta")),
            "case_number": "（2026）苏01民初200号",
            "case_name": "云璟府建设工程争议案",
            "external_case_id": "BGGL-2026-0200",
            "confirmed_aliases": ["云璟府"],
            "version": 5,
        },
    ]


def _label(case_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "expected_decisions": scenario["decisions"],
        "expected_ticket_count": scenario["ticket_count"],
        "expected_information_pending_count": scenario["information_pending_count"],
        "expected_selection_request_count": scenario["selection_request_count"],
        "annotation_class": "machine_candidate",
        "review_status": "pending_human_review",
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build physically separated Agent2 Semantic Admission blind inputs and sealed machine-candidate labels."
    )
    parser.add_argument(
        "--output-dir",
        default="evals/agent2/semantic_admission",
    )
    manifest = build_artifacts(parser.parse_args().output_dir)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

