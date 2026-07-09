from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

import httpx


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def run_smoke(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        health = await client.get("/health")
        assert_true(health.status_code == 200 and health.json() == {"status": "ok"}, "health endpoint is not ok")
        resp = await client.post(
            "/performance/department/monthly-report",
            json={
                "period_label": "2026-06",
                "use_test_data": True,
                "test_seed": 20260630,
                "generated_for": "赵卫中",
                "send_messages": False,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        report = payload["report_text"]
        assert_true(payload["ready"] is True, "test department report should be ready")
        assert_true(payload["source_count"] == 8, "test department report should contain 8 sources")
        assert_true(payload["missing_units"] == [], "test department report should have no missing units")
        assert_true(report.startswith("# 【测试】法务合约中心2026年6月部门绩效月报"), "report title mismatch")
        assert_true("朱佳佳" in report and "国别市场研究及合同示范文本" in report, "Zhu Jiajia source missing")
        assert_true("## 📌 一、总体结论" in report, "conclusion section missing")
        assert_true("## 🎯 二、核心指标完成情况" in report, "core metric section missing")
        assert_true("年度时间进度：50%" in report and "  月度：目标 " in report, "core metric text blocks missing")
        assert_true("| 指标 | 月目标 | 月实际 | 月完成率" not in report, "core metric table should not be used")
        assert_true("## 🚦 三、重点风险指标" in report, "risk section missing")
        assert_true("## 🤝 五、需领导协调事项" in report, "leadership coordination section missing")
        assert_true("涉及部门：" in report and "需协调动作：" in report, "leadership coordination text blocks missing")
        assert_true("| 事项 | 涉及部门 | 影响指标" not in report, "leadership coordination table should not be used")
        assert_true("## ✅ 六、下月重点动作与风险预判" in report, "next-month action section missing")
        assert_true("附录" not in report, "leader report must not include raw appendix")
        assert_true("…" not in report, "leader report must not contain truncation ellipsis")
        assert_true("随机生成" not in report, "leader report must not expose random-data wording")
        assert_true(payload["message_chunks"] >= 1, "message chunk count should be positive")
        assert_true(payload["send_results"] == [], "smoke must not send messages")
        return {"status": "PASS", "message_chunks": payload["message_chunks"]}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    started = time.perf_counter()
    result = await run_smoke(args.base_url)
    summary = {"pass": 1, "fail": 0, "total": 1, "seconds": round(time.perf_counter() - started, 1), **result}
    payload = {"summary": summary}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    print("ONLINE_DEPARTMENT_PERFORMANCE_SMOKE_START")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    if args.output:
        print(f"OUTPUT {args.output}")
    print("ONLINE_DEPARTMENT_PERFORMANCE_SMOKE_END")


if __name__ == "__main__":
    asyncio.run(main())
