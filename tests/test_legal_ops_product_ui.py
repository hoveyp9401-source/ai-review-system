from pathlib import Path

from app.legal_ops.business_labels import case_stage_label, source_label, travel_status_label
from app.legal_ops.live_workspace import (
    project_case_workspace,
    project_case_detail,
    project_report_center,
    project_team_center,
    project_travel_center,
)


STATIC = Path("app/legal_ops/static")


def test_localhost_does_not_bypass_live_login_with_a_hardcoded_credential():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    state_block = script.split("const state =", 1)[1].split("};", 1)[0]

    assert "codex-local-legal-ops" not in state_block
    assert "window.location.hostname" not in state_block
    assert 'sessionStorage.getItem("legalOpsCredential")' in script
    assert 'window.location.port === "8765"' in script
    assert "进入本地演示数据" in page


def test_login_uses_product_language_instead_of_http_header_terminology():
    page = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "登录凭证" in page
    assert "X-Legal-Ops-Token" not in page


def test_product_shell_does_not_label_live_postgresql_as_fixture():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "Sandbox fixtures" not in page
    assert "灰测真实数据" in script
    assert "role_ids.includes(\"tenant_admin\")" in script


def test_product_shell_uses_chinese_and_never_renders_raw_transport_identifiers():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "SANDBOX TENANT" not in page
    assert "LEGAL OPERATIONS" not in page
    assert 'sandbox_fixture: "演示数据"' in script
    assert "external_message_id=" not in script
    assert '["外部消息 ID", "external_message_id"]' not in script
    assert "source_message_id" not in script


def test_business_statuses_are_rendered_through_one_closed_chinese_dictionary():
    assert case_stage_label("plaintiff_case", "intended_filing") == "拟诉"
    assert case_stage_label("plaintiff_case", "litigation") == "诉讼中"
    assert case_stage_label("defendant_case", "adjudicated") == "审结"
    assert travel_status_label("sent") == "平台已接受发送请求"
    assert source_label("real_case_workbook") == "真实来源灰测副本"
    assert case_stage_label("plaintiff_case", "future_unknown") == "数据状态异常"


def test_case_workspace_is_paginated_chinese_and_hides_internal_identifiers():
    raw = {
        "tenant_id": "tenant-test",
        "identity_bindings": [
            {"user_id": "owner-secret-uuid", "display_name": "庞浩"}
        ],
        "parties": [
            {"party_id": "party-1", "canonical_name": "南京甲公司"},
            {"party_id": "party-2", "canonical_name": "南京乙公司"},
        ],
        "party_case_roles": [
            {"case_id": "case-1", "party_id": "party-1", "role_type": "defendant"},
            {"case_id": "case-2", "party_id": "party-2", "role_type": "plaintiff"},
        ],
        "case_progress": [
            {
                "progress_id": "progress-internal-id",
                "case_id": "case-1",
                "summary": "今天联系法院推进查控",
                "deleted_at": None,
                "updated_at": "2026-07-13T09:00:00+00:00",
            }
        ],
        "case_lifecycle_states": [
            {"case_id": "case-1", "node": "已立案", "next_actions_json": ["下周联系法院"]}
        ],
        "case_followup_policies": [
            {"case_id": "case-1", "cadence_type": "event_only", "enabled": True}
        ],
        "cases": [
            {
                "case_id": "case-1",
                "case_name": "甲公司合同纠纷案",
                "case_number": "（2026）苏01民初1号",
                "case_type": "plaintiff_case",
                "status": "litigation",
                "owner_user_id": "owner-secret-uuid",
                "source_type": "real_case_workbook",
                "source_json": {"secret": "must-not-leak"},
                "version": 7,
                "updated_at": "2026-07-13T09:00:00+00:00",
            },
            {
                "case_id": "case-2",
                "case_name": "乙公司买卖合同纠纷案",
                "case_number": "（2026）苏01民初2号",
                "case_type": "defendant_case",
                "status": "hearing",
                "owner_user_id": "owner-secret-uuid",
                "source_type": "real_case_workbook",
                "version": 2,
                "updated_at": "2026-07-12T09:00:00+00:00",
            },
        ],
    }

    payload = project_case_workspace(raw, page=1, page_size=1)

    assert payload["pagination"] == {"page": 1, "page_size": 1, "total": 2, "pages": 2}
    assert payload["summary"]["plaintiff"] == 1
    assert payload["summary"]["defendant"] == 1
    assert payload["items"][0]["case_type"] == "原告案件"
    assert payload["items"][0]["stage"] == "诉讼中"
    assert payload["items"][0]["owner_name"] == "庞浩"
    assert payload["items"][0]["counterparties"] == ["南京甲公司"]
    assert payload["items"][0]["latest_progress"] == "今天联系法院推进查控"
    assert payload["items"][0]["node"] == "已立案"
    assert payload["items"][0]["next_plan"] == "下周联系法院"
    assert payload["items"][0]["followup_policy"] == "仅关键节点"
    assert payload["items"][0]["business_facts"] == []
    assert "hearing_date" not in payload["items"][0]
    assert "risk_level" not in payload["items"][0]
    assert "court" not in payload["items"][0]
    assert "cause" not in payload["items"][0]
    assert "owner_user_id" not in payload["items"][0]
    assert "source_json" not in payload["items"][0]
    assert "version" not in payload["items"][0]

    missing = project_case_workspace(raw, page=1, page_size=20, progress_status="missing")
    assert missing["pagination"]["total"] == 1
    assert missing["items"][0]["case_name"] == "乙公司买卖合同纠纷案"


def test_shared_case_scope_distinguishes_assigned_and_collaboration_cases():
    raw = {
        "identity_bindings": [
            {"user_id": "pang-id", "display_name": "庞浩"},
            {"user_id": "liu-id", "display_name": "刘聪"},
        ],
        "cases": [
            {
                "case_id": "case-owned",
                "case_name": "本人负责案件",
                "case_type": "plaintiff_case",
                "status": "litigation",
                "owner_user_id": "pang-id",
                "source_type": "real_case_workbook",
            },
            {
                "case_id": "case-shared",
                "case_name": "团队协作案件",
                "case_type": "defendant_case",
                "status": "hearing",
                "owner_user_id": "liu-id",
                "source_type": "real_case_workbook",
            },
        ],
    }

    payload = project_case_workspace(
        raw,
        principal_user_id="pang-id",
        writable_case_ids=("case-owned", "case-shared"),
        permission_mode="explicit_shared_scope",
    )

    assert payload["summary"]["assigned_to_me"] == 1
    assert payload["summary"]["shared_with_me"] == 1
    assert payload["summary"]["writable"] == 2
    assert payload["summary"]["access_label"] == "团队共享协作"
    assert {item["assignment"] for item in payload["items"]} == {
        "本人负责",
        "团队协作",
    }
    assert all(item["can_add_progress"] for item in payload["items"])


def test_live_navigation_matches_the_simplified_product_information_architecture():
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    for key, label in (
        ("overview", "工作总览"),
        ("reports", "报告中心"),
        ("cases", "案件工作台"),
        ("travel", "出差协同"),
        ("team", "团队"),
        ("audit", "审计证据"),
    ):
        assert f'["{key}", "{label}"' in script
    assert '["parties", "主体知识库"' not in script
    assert 'api("workspace/cases' in script


def test_report_center_unifies_daily_weekly_monthly_and_keeps_full_user_visible_content():
    daily = [
        {
            "id": "daily-internal-id",
            "user_id": "pang-user",
            "report_date": "2026-07-13",
            "today_work": ["优化法务中台网页端"],
            "problems": ["暂无"],
            "tomorrow_plan": ["继续联调案件页"],
            "status": "collecting",
            "source": "dingtalk_text",
            "updated_at": "2026-07-13T06:00:00+00:00",
            "llm_payload": {"private": "must-not-leak"},
        }
    ]
    periodic = [
        {
            "report_id": "weekly-internal-id",
            "owner_user_id": "pang-user",
            "report_type": "weekly",
            "period_key": "2026-W29",
            "sections_json": {"accomplishments": ["完成案件分配"], "next_plan": ["验证出差协同"]},
            "status": "completed",
            "source_channel": "server_natural_language_greytest",
            "updated_at": "2026-07-12T06:00:00+00:00",
            "version": 4,
        }
    ]

    payload = project_report_center(
        daily, periodic, identity_names={"pang-user": "庞浩"}
    )

    assert payload["summary"] == {"total": 2, "daily": 1, "weekly": 1, "monthly": 0, "collecting": 1}
    assert payload["items"][0]["report_type"] == "日报"
    assert payload["items"][0]["owner_name"] == "庞浩"
    assert payload["items"][0]["sections"]["今日工作"] == ["优化法务中台网页端"]
    assert payload["items"][1]["report_type"] == "周报"
    assert payload["items"][1]["status"] == "已提交"
    assert payload["items"][1]["sections"]["本期完成"] == ["完成案件分配"]
    assert "llm_payload" not in payload["items"][0]
    assert "version" not in payload["items"][1]

    daily[0]["source"] = "legal_ops_ui"
    ui_payload = project_report_center(
        daily, periodic, identity_names={"pang-user": "庞浩"}
    )
    assert ui_payload["items"][0]["source"] == "法务业务中台"


def test_report_center_exposes_scoped_write_contract_without_rendering_internal_fields():
    daily = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "user_id": "pang-user",
            "report_date": "2026-07-13",
            "today_work": ["完成案件材料整理"],
            "problems": ["暂无"],
            "tomorrow_plan": ["联系法院"],
            "status": "collecting",
            "section_status": {
                "_agent2_report_version": 3,
                "_draft_item_ids": {
                    "today_work": ["daily-item-1"],
                    "problems": ["daily-item-2"],
                    "tomorrow_plan": ["daily-item-3"],
                },
            },
            "updated_at": "2026-07-13T06:00:00+00:00",
        }
    ]
    periodic = [
        {
            "report_id": "22222222-2222-2222-2222-222222222222",
            "owner_user_id": "pang-user",
            "report_type": "weekly",
            "period_key": "2026-W29",
            "sections_json": {"accomplishments": ["完成案件分配"]},
            "item_ids_json": {"accomplishments": ["weekly-item-1"]},
            "status": "collecting",
            "version": 2,
            "updated_at": "2026-07-13T06:00:00+00:00",
        }
    ]

    payload = project_report_center(
        daily,
        periodic,
        identity_names={"pang-user": "庞浩"},
        editable_owner_user_id="pang-user",
        current_date="2026-07-13",
    )

    by_type = {item["report_type_code"]: item for item in payload["items"]}
    assert by_type["daily"]["actions"]["can_edit"] is True
    assert by_type["daily"]["actions"]["expected_version"] == 3
    assert by_type["daily"]["actions"]["items"]["today_work"] == [
        {"item_ref": "daily-item-1", "value": "完成案件材料整理"}
    ]
    assert by_type["weekly"]["actions"]["items"]["accomplishments"] == [
        {"item_ref": "weekly-item-1", "value": "完成案件分配"}
    ]


def test_team_center_uses_real_bindings_assignments_and_reports_without_user_ids():
    bindings = [
        {"user_id": "pang-id", "display_name": "庞浩", "team_id": "sandbox-agent2-team"},
        {"user_id": "liu-id", "display_name": "刘聪", "team_id": "sandbox-agent2-team"},
    ]
    cases = [
        {"owner_user_id": "pang-id", "case_type": "plaintiff_case"},
        {"owner_user_id": "pang-id", "case_type": "defendant_case"},
        {"owner_user_id": "liu-id", "case_type": "plaintiff_case"},
    ]
    daily = [
        {"user_id": "pang-id", "report_date": "2026-07-13", "status": "collecting"},
        {"user_id": "liu-id", "report_date": "2026-07-12", "status": "completed"},
    ]
    periodic = [{"owner_user_id": "pang-id", "report_type": "weekly", "status": "completed"}]
    travel = [{"user_id": "liu-id", "status": "planned"}]

    payload = project_team_center(bindings, cases, daily, periodic, travel)

    assert payload["team_name"] == "当前授权团队"
    assert payload["identity_mapping"] == {
        "source": "Agent2 身份绑定表",
        "scope": "当前租户与当前团队",
        "status": "服务器实时映射",
    }
    assert payload["summary"] == {"members": 2, "cases": 3, "active_travel": 1}
    pang = next(item for item in payload["members"] if item["name"] == "庞浩")
    liu = next(item for item in payload["members"] if item["name"] == "刘聪")
    assert pang["assigned_cases"] == 2
    assert pang["plaintiff_cases"] == 1
    assert pang["defendant_cases"] == 1
    assert pang["latest_daily_status"] == "收集中"
    assert pang["periodic_reports"] == 1
    assert liu["active_travel"] == 1
    assert all("user_id" not in item for item in payload["members"])


def test_travel_center_distinguishes_provider_acceptance_from_delivery_and_hides_transport_ids():
    raw = {
        "identity_bindings": [
            {"user_id": "pang-id", "display_name": "庞浩"},
            {"user_id": "liu-id", "display_name": "刘聪"},
        ],
        "travel_intents": [
            {
                "travel_intent_id": "travel-id",
                "user_id": "pang-id",
                "destination_normalized": "南京",
                "start_at": "2026-07-14T00:00:00+00:00",
                "end_at": "2026-07-14T12:00:00+00:00",
                "purpose_summary": "推进案件",
                "status": "planned",
                "source_channel": "dingtalk_stream",
                "source_message_id": "secret-message-id",
            },
            {
                "travel_intent_id": "smoke-travel-id",
                "user_id": "pang-id",
                "destination_normalized": "测试城市",
                "status": "planned",
                "source_channel": "server_acceptance_smoke",
                "data_origin": "server_acceptance_smoke",
            },
        ],
        "collaboration_candidates": [
            {
                "candidate_id": "candidate-id",
                "destination": "南京",
                "participant_ids": ["pang-id", "liu-id"],
                "status": "accepted_by_one",
                "responses_json": {"pang-id": "accept", "liu-id": "waiting_for_reply"},
                "overlap_start": "2026-07-14T00:00:00+00:00",
                "overlap_end": "2026-07-14T12:00:00+00:00",
            },
            {
                "candidate_id": "smoke-candidate-id",
                "destination": "测试城市",
                "participant_ids": ["pang-id", "liu-id"],
                "status": "pending",
                "data_origin": "server_acceptance_smoke",
            },
        ],
        "notifications": [
            {
                "notification_id": "notification-id",
                "recipient_user_id": "liu-id",
                "status": "sent",
                "external_message_id": "provider-secret-id",
                "sent_at": "2026-07-13T08:00:00+00:00",
                "message_type": "travel_collaboration_question",
            },
            {
                "notification_id": "case-followup-notification-id",
                "recipient_user_id": "pang-id",
                "status": "sent",
                "message_type": "case_progress_followup",
                "data_origin": "robot_followup",
            },
        ],
    }

    payload = project_travel_center(raw)

    assert payload["travels"][0]["traveler_name"] == "庞浩"
    assert payload["travels"][0]["status"] == "已登记"
    assert payload["candidates"][0]["status"] == "一方已接受"
    assert payload["candidates"][0]["participants"] == ["庞浩", "刘聪"]
    assert payload["candidates"][0]["responses"][0]["status"] == "已接受"
    assert payload["notifications"][0]["status"] == "平台已接受发送请求"
    assert payload["notifications"][0]["delivery_claim"] == "未确认送达"
    assert payload["summary"] == {
        "travels": 1,
        "candidates": 1,
        "notifications": 1,
        "waiting_for_reply": 1,
    }
    serialized = repr(payload)
    assert "provider-secret-id" not in serialized
    assert "secret-message-id" not in serialized
    assert "smoke-travel-id" not in serialized
    assert "smoke-candidate-id" not in serialized
    assert "case-followup-notification-id" not in serialized


def test_single_case_detail_builds_truthful_lifecycle_and_hides_receipt_fields():
    detail = {
        "case": {
            "case_id": "case-id",
            "case_name": "甲公司合同纠纷案",
            "case_number": "（2026）苏01民初1号",
            "case_type": "plaintiff_case",
            "status": "litigation",
            "owner_user_id": "pang-id",
            "source_type": "real_case_workbook",
            "version": 6,
        },
        "parties": [
            {"party": {"canonical_name": "南京甲公司"}, "role": {"role_type": "defendant"}}
        ],
        "business_clues": [],
        "lifecycle": {
            "internal_progress_nodes": [
                {
                    "progress_id": "33333333-3333-3333-3333-333333333333",
                    "title": "今天联系法院推进查控",
                    "occurred_at": "2026-07-13T09:00:00+00:00",
                    "content_origin": "human_record",
                    "source_message_id": "secret-message-id",
                    "reporter_id": "pang-id",
                    "version": 3,
                    "deleted": False,
                },
                {
                    "progress_id": "44444444-4444-4444-4444-444444444444",
                    "title": "已经撤销的验收临时记录",
                    "occurred_at": "2026-07-13T08:00:00+00:00",
                    "content_origin": "human_record",
                    "version": 2,
                    "deleted": True,
                },
            ]
        },
        "audits": [
            {
                "audit_id": "audit-secret-id",
                "receipt_id": "receipt-secret-id",
                "actor_user_id": "pang-id",
                "source_message_id": "message-secret-id",
                "source_channel": "legal_ops_ui",
                "command_type": "create_case_progress",
                "resource_type": "case_progress",
                "resource_id": "33333333-3333-3333-3333-333333333333",
                "before_json": {},
                "after_json": {"summary": "今天联系法院推进查控"},
                "created_at": "2026-07-13T09:01:00+00:00",
            }
        ],
        "report_projections": [
            {
                "projection_id": "projection-secret-id",
                "case_progress_id": "33333333-3333-3333-3333-333333333333",
                "report_id": "report-secret-id",
                "report_item_id": "item-secret-id",
                "report_type": "daily",
                "projection_type": "today_work",
                "status": "active",
                "created_at": "2026-07-13T09:02:00+00:00",
            }
        ],
    }
    followup = {
        "case": {"case_id": "case-id", "owner_user_id": "pang-id"},
        "policy": {"cadence_type": "event_only", "enabled": True, "version": 2},
        "lifecycle_state": None,
        "latest_status": {"waiting_for_reply": False, "last_message_status": None},
        "pending": None,
        "history": [],
    }

    payload = project_case_detail(
        detail,
        followup,
        identity_names={"pang-id": "庞浩", "liu-id": "刘聪"},
        can_manage_followup=False,
        editable_actor_user_id="pang-id",
        writable_case_ids=("case-id",),
        permission_mode="explicit_shared_scope",
        followup_enabled=True,
        followup_send_enabled=False,
        report_projection_enabled=False,
    )

    assert payload["case_type"] == "原告案件"
    assert payload["stage"] == "诉讼中"
    assert [item["label"] for item in payload["lifecycle"]] == ["拟诉", "诉讼中", "执行中", "已结案"]
    assert [item["state"] for item in payload["lifecycle"]] == ["completed", "current", "future", "future"]
    assert payload["owner_name"] == "庞浩"
    assert payload["assignment"] == "本人负责"
    assert payload["can_add_progress"] is True
    assert payload["parties"] == [{"name": "南京甲公司", "role": "被告"}]
    assert payload["progress"][0]["content"] == "今天联系法院推进查控"
    assert len(payload["progress"]) == 1
    assert "已经撤销的验收临时记录" not in repr(payload)
    assert payload["progress"][0]["actions"] == {
        "progress_ref": "33333333-3333-3333-3333-333333333333",
        "expected_version": 3,
        "can_edit": True,
    }
    assert payload["audit"] == [
        {
            "actor_name": "庞浩",
            "action": "新增案件进展",
            "result": "已写入",
            "source": "法务业务中台",
            "created_at": "2026-07-13T09:01:00+00:00",
        }
    ]
    assert payload["report_projection"] == [
        {
            "report_type": "日报",
            "section": "今日工作",
            "status": "已关联",
            "created_at": "2026-07-13T09:02:00+00:00",
        }
    ]
    assert payload["can_manage_followup"] is False
    assert payload["followup"]["capability"] == {
        "task_generation": "已开启",
        "message_delivery": "未开启",
        "report_projection": "未开启",
        "can_trigger_task": False,
    }
    serialized = repr(payload)
    assert "secret-message-id" not in serialized
    assert "source_message_id" not in serialized
    assert "receipt_id" not in serialized
    assert "audit-secret-id" not in serialized
    assert "receipt-secret-id" not in serialized
    assert "message-secret-id" not in serialized
    assert "projection-secret-id" not in serialized
    assert "report-secret-id" not in serialized
    assert "item-secret-id" not in serialized

    collaborator_payload = project_case_detail(
        detail,
        followup,
        identity_names={"pang-id": "庞浩", "liu-id": "刘聪"},
        can_manage_followup=False,
        editable_actor_user_id="liu-id",
        writable_case_ids=("case-id",),
        permission_mode="explicit_shared_scope",
    )
    assert collaborator_payload["assignment"] == "团队协作"
    assert collaborator_payload["can_add_progress"] is True
    assert collaborator_payload["progress"][0]["actions"]["can_edit"] is False


def test_followup_ui_labels_shadow_task_creation_without_claiming_a_message_will_be_sent():
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "创建追问任务（暂不发送）" in script
    assert "当前仅生成追问任务，钉钉发送未开启" in script
    assert ">立即追问一次<" not in script


def test_live_product_ui_wires_case_progress_and_report_mutations_to_workspace_endpoints():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "data-add-case-progress" in script
    assert "本人负责" in script
    assert "团队协作" in script
    assert "data.can_add_progress" in script
    assert "当前凭证对该案件仅有查看权限" in script
    assert "data-start-case-progress-edit" in script
    assert "data-edit-case-progress-form" in script
    assert "data-delete-case-progress" in script
    assert "data-report-append" in script
    assert "data-start-report-edit" in script
    assert "data-report-edit-form" in script
    assert "data-report-delete" in script
    assert "data-report-submit" in script
    assert "workspace/cases/${encodeURIComponent(caseId)}/progress" in script
    assert "workspace/reports/${encodeURIComponent(" in script
    assert ")}/commands" in script
    assert "window.prompt" not in script
    assert "window.confirm" not in script
    assert "只读 BFF" not in script
    assert "reconcileCaseMutation" in script
    assert "操作记录" in script
    assert "日报投影" in script
    assert "data-case-section-target" in script
    assert "activateCaseSection" in script
    assert '<span>概览</span>' not in script
    assert "<title>Legal Ops 法务业务中台</title>" in page
