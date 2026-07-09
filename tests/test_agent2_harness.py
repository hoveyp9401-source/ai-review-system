import json
from pathlib import Path

from app.agent2.harness.case_loader import load_cases
from app.agent2.harness.reporter import write_reports
from app.agent2.harness.runner import run_cases


def test_harness_loads_jsonl_cases_and_judges_results(tmp_path: Path):
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(
        "\n".join(
            [
                (
                    '{"case_id":"daily_pass","text":"\\u4eca\\u5929\\u5b8c\\u6210\\u5408\\u540c\\u5ba1\\u6838",'
                    '"expected":{"primary_workflow":"daily_report","should_enter_daily":true,'
                    '"expected_effects":["add_daily_report_item"],'
                    '"expected_coordination_actions":["daily_entry"]}}'
                ),
                (
                    '{"case_id":"coordination_split","text":"\\u5408\\u540c\\u5ba1\\u6838\\u6d41\\u7a0b\\u68b3\\u7406\\uff0c\\u6ca1\\u5565\\u95ee\\u9898\\uff0c\\u660e\\u5929\\u53ef\\u80fd\\u51fa\\u5dee\\u5357\\u4eac",'
                    '"expected":{"expected_coordination_actions":["daily_entry","travel_event"],'
                    '"forbidden_coordination_actions":["case_progress_entry"],'
                    '"expected_sandbox_candidates":["travel_coordination_candidate"],'
                    '"forbidden_sandbox_candidates":["case_progress_candidate"]}}'
                ),
                (
                    '{"case_id":"intentional_failure","text":"\\u4eca\\u5929\\u5b8c\\u6210\\u5408\\u540c\\u5ba1\\u6838",'
                    '"expected":{"primary_workflow":"monthly_report"}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_cases([case_file])
    results = run_cases(cases)

    assert [case.case_id for case in cases] == ["daily_pass", "coordination_split", "intentional_failure"]
    assert results[0].passed is True
    assert results[1].passed is True
    assert results[2].passed is False
    assert "primary_workflow" in results[2].failures[0]
    assert results[0].actual
    assert results[0].actual.commands[0]["operation"] == "fill"
    assert results[0].actual.legacy_adapter_results[0]["legacy_action"] == "append_daily_items"


def test_harness_reporter_writes_expected_files(tmp_path: Path):
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(
            (
                '{"case_id":"dangerous_clear","text":"\\u6e05\\u7a7a\\u4eca\\u65e5\\u65e5\\u62a5",'
                '"severity":"critical","tags":["daily"],'
                '"expected":{"primary_workflow":"daily_report","should_enter_daily":true,'
                '"need_confirmation":false,"gate_reply_type":"none"}}\n'
            ),
        encoding="utf-8",
    )

    results = run_cases(load_cases([case_file]))
    summary = write_reports(results, tmp_path / "reports")

    assert summary.total_cases == 1
    assert summary.unexpected_failure_cases == 0
    assert (tmp_path / "reports" / "latest_results.jsonl").exists()
    assert (tmp_path / "reports" / "latest_summary.md").exists()
    assert (tmp_path / "reports" / "high_risk_cases.csv").exists()


def test_harness_builds_context_pack_knowledge_from_org_and_daily_history(tmp_path: Path):
    case_file = tmp_path / "knowledge_cases.jsonl"
    cases = [
        {
            "case_id": "org_context",
            "text": "我属于哪个团队？",
            "context": {
                "user_id": "u-1",
                "sender_name": "庞浩",
                "dingtalk_user_id": "dt-1",
                "org_teams": [
                    {"id": "team-2", "name": "法务二部", "department_name": "法务合约中心"},
                ],
                "org_users": [
                    {"id": "u-1", "name": "庞浩", "dingtalk_user_id": "dt-1", "team_id": "team-2", "role": "member"},
                    {"id": "leader-2", "name": "丁益明", "team_id": "team-2", "role": "team_leader"},
                ],
            },
            "expected": {
                "expected_knowledge_status": "available",
                "expected_knowledge_sources": ["org_directory"],
                "should_enter_daily": False,
            },
        },
        {
            "case_id": "daily_history_context",
            "text": "昨天的计划都完成了",
            "context": {
                "user_id": "u-1",
                "dingtalk_user_id": "dt-1",
                "current_date": "2026-07-03",
                "daily_history": [
                    {
                        "id": "r-0702",
                        "user_id": "u-1",
                        "dingtalk_user_id": "dt-1",
                        "date": "2026-07-02",
                        "status": "completed",
                        "tomorrow_plan": ["南京出差盖章", "整理案件材料"],
                    }
                ],
            },
            "expected": {
                "expected_knowledge_status": "available",
                "expected_knowledge_sources": ["daily_report_history"],
            },
        },
    ]
    case_file.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8")

    results = run_cases(load_cases([case_file]))

    assert [result.passed for result in results] == [True, True]
    assert results[0].actual
    assert results[0].actual.knowledge_source_types == ["org_directory"]
    assert results[0].actual.knowledge_facts[0]["team_name"] == "法务二部"
    assert results[1].actual
    assert results[1].actual.knowledge_source_types == ["daily_report_history"]
    assert results[1].actual.knowledge_facts[0]["today_work_candidates"] == ["南京出差盖章", "整理案件材料"]
