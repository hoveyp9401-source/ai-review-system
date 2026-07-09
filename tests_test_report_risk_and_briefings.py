from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.services.report_risk import analyze_report_risks, build_report_rows, completion_stats
from app.services.summary_service import build_department_briefing_text, build_team_briefing_text


def _team(name="一团队"):
    return SimpleNamespace(id=uuid4(), name=name, department_name="法务部")


def _user(team, name="张三", role="member"):
    return SimpleNamespace(id=uuid4(), team_id=team.id, team=team, name=name, role=role, dingtalk_user_id=f"dt-{name}")


def _report(user, report_date, **overrides):
    values = {
        "user_id": user.id,
        "team_id": user.team_id,
        "report_date": report_date,
        "today_work": ["审核合同"],
        "problems": ["暂无明显问题"],
        "tomorrow_plan": ["明天继续审核合同"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True, "problems_acknowledged_empty": True},
        "status": "completed",
        "confirmation_type": "user_confirmed",
        "confirmed_by_user": True,
        "quality_warning": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_risk_engine_marks_missing_report():
    team = _team()
    user = _user(team)

    risks = analyze_report_risks(user, None)

    assert risks[0]["label"] == "未提交"
    assert risks[0]["severity"] == "high"


def test_risk_engine_marks_suspicious_and_legal_risks():
    team = _team()
    user = _user(team)
    report = _report(
        user,
        date(2026, 6, 16),
        today_work=["今天吃鸡蛋饼"],
        problems=["诉讼标的约3000万，诉讼费约20万，竣工报告原件无法找到"],
    )

    labels = {risk["label"] for risk in analyze_report_risks(user, report)}

    assert "疑似测试/瞎填" in labels
    assert "实质法务风险" in labels


def test_risk_engine_marks_repeated_progress_and_plan():
    team = _team()
    user = _user(team)
    today = date(2026, 6, 16)
    report = _report(user, today, today_work=["跟进项目"], tomorrow_plan=["继续跟进项目"])
    history = [
        _report(user, today - timedelta(days=1), today_work=["跟进项目"], tomorrow_plan=["继续跟进项目"]),
        _report(user, today - timedelta(days=2), today_work=["跟进项目"], tomorrow_plan=["继续跟进项目"]),
    ]

    labels = {risk["label"] for risk in analyze_report_risks(user, report, history)}

    assert "连续进展无变化" in labels
    assert "连续计划无变化" in labels
    assert "内容笼统" in labels


def test_team_and_department_briefing_text_contains_completion_and_risks():
    team = _team("诉讼组")
    leader = _user(team, name="负责人", role="team_lead")
    member = _user(team, name="李四")
    today = date(2026, 6, 16)
    reports = [_report(leader, today), _report(member, today, status="pending_confirmation", confirmed_by_user=False)]
    rows = build_report_rows([leader, member], reports)

    team_text = build_team_briefing_text(team.name, today, rows)
    department_text = build_department_briefing_text(
        today,
        rows,
        [{"team_name": team.name, "stats": completion_stats(rows)}],
    )

    assert "【诉讼组】晨报总览" in team_text
    assert "⏳待确认 1" in team_text
    assert "**📊 一、填报情况**" in team_text
    assert "**⚠️ 二、风险/问题/卡点**" in team_text
    assert "**🧭 三、明日关键计划**" in team_text
    assert "成员日报明细" not in team_text
    assert "【部门】晨报总览" in department_text
    assert "各团队情况" in department_text
    assert "风险/问题/卡点" in department_text
    assert "明日关键计划" in department_text


def test_briefing_overview_filters_basic_metrics_and_keeps_key_items():
    team = _team("Team 01")
    user = _user(team, name="刘聪")
    today = date(2026, 6, 17)
    report = _report(
        user,
        today,
        today_work=[
            "日常用印资料审核",
            "处理6份函件用印邮寄",
            "使用VPN时ERP访问异常，影响ERP相关工作处理进度",
        ],
        problems=["使用VPN上网导致ERP访问异常，影响ERP相关工作处理进度"],
        tomorrow_plan=[
            "继续今日工作",
            "明天下午三点参加房产会议，需通知律师并确认律师上线情况",
        ],
    )
    rows = build_report_rows([user], [report])

    text = build_team_briefing_text(team.name, today, rows)

    assert "日常用印资料审核" not in text
    assert "处理6份函件用印邮寄" not in text
    assert "使用VPN上网导致ERP访问异常" in text
    assert "明天下午三点参加房产会议" in text
    assert "继续今日工作" not in text
    assert "**⚠️ 二、风险/问题/卡点**" in text
    assert "**🧭 三、明日关键计划**" in text
