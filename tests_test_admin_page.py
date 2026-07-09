from fastapi.testclient import TestClient

from app.main import app


def test_admin_reports_dashboard_loads():
    with TestClient(app) as client:
        response = client.get("/admin/reports")

    assert response.status_code == 200
    assert "AI 每日复盘看板" in response.text
    assert "个人日报列表" in response.text
    assert "问题/风险汇总" in response.text
    assert "明日计划汇总" in response.text
