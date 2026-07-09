from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.agent import executor as agent_executor
from app.config import Settings
from app.llm.client import LLMClient
from app.llm.extractor import DailyReportExtractor
from app.services import report_service
from app.services.report_service import DailyReportService
from app.services.state_machine import (
    CONFIRMATION_NONE,
    STATUS_COLLECTING,
    STATUS_COMPLETED,
    STATUS_PENDING_CONFIRMATION,
)


STATUS_MAP = {
    "collecting": STATUS_COLLECTING,
    "completed": STATUS_COMPLETED,
    "pending_confirmation": STATUS_PENDING_CONFIRMATION,
}


class CountingLLMClient(LLMClient):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, **kwargs):
        started = time.perf_counter()
        try:
            result = await super().complete_json(**kwargs)
            self.calls.append(
                {
                    "ok": True,
                    "model": kwargs.get("model") or self.model,
                    "seconds": round(time.perf_counter() - started, 3),
                }
            )
            return result
        except Exception as exc:
            self.calls.append(
                {
                    "ok": False,
                    "model": kwargs.get("model") or self.model,
                    "seconds": round(time.perf_counter() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise


class FakeSession:
    async def flush(self):
        return None


def make_user(name: str = "问题台账模拟用户"):
    return SimpleNamespace(id=uuid4(), team_id=uuid4(), timezone="Asia/Shanghai", name=name, role="member", active=True)


def make_report(report_date: date, user: Any, **overrides):
    values = {
        "id": uuid4(),
        "user_id": user.id,
        "team_id": user.team_id,
        "report_date": report_date,
        "today_work": [],
        "problems": [],
        "tomorrow_plan": [],
        "section_status": {},
        "status": STATUS_COLLECTING,
        "confirmation_type": CONFIRMATION_NONE,
        "confirmed_by_user": False,
        "quality_warning": None,
        "emotion": "",
        "completeness_score": 0.0,
        "input_fragments": [],
        "pending_confirmation_at": None,
        "auto_submit_at": None,
        "last_modified_by_user": False,
        "last_modified_at": None,
        "submitted_at": None,
        "llm_model": "issue-ledger-simulation",
        "llm_payload": {},
        "source": "issue_ledger_simulation",
    }
    values.update(overrides)
    values["status"] = STATUS_MAP.get(values["status"], values["status"])
    return SimpleNamespace(**values)


def report_from_spec(spec: dict[str, Any] | None, report_date: date, user: Any):
    if not spec:
        return None
    return make_report(
        report_date=report_date,
        user=user,
        today_work=list(spec.get("today_work") or []),
        problems=list(spec.get("problems") or []),
        tomorrow_plan=list(spec.get("tomorrow_plan") or []),
        section_status=dict(spec.get("section_status") or {}),
        status=spec.get("status", STATUS_COLLECTING),
        completeness_score=float(spec.get("completeness_score", 0.0)),
        confirmed_by_user=bool(spec.get("confirmed_by_user", False)),
    )


class MemoryStore:
    def __init__(self, *, user: Any, now: datetime, initial: dict[str, Any] | None, reports: list[dict[str, Any]]):
        self.user = user
        self.now = now
        self.reports: dict[date, Any] = {}
        current = report_from_spec(initial, now.date(), user)
        if current is not None:
            self.reports[current.report_date] = current
        for item in reports or []:
            report_date = date.fromisoformat(item["report_date"])
            self.reports[report_date] = report_from_spec(item, report_date, user)
        self.original_snapshots = {
            key.isoformat(): {
                "today_work": list(value.today_work or []),
                "problems": list(value.problems or []),
                "tomorrow_plan": list(value.tomorrow_plan or []),
                "status": value.status,
            }
            for key, value in self.reports.items()
        }
        self.saved_count = 0
        self.originals: dict[tuple[Any, str], Any] = {}

    async def get_report(self, session, user_id, report_date):
        return self.reports.get(report_date)

    async def upsert(self, session, **kwargs):
        old = self.reports.get(kwargs["report_date"])
        fragments = list(getattr(old, "input_fragments", []) or []) if old else []
        fragments.append({"raw_input": kwargs["raw_input"], "structured": kwargs["llm_payload"]})
        self.saved_count += 1
        report = make_report(
            kwargs["report_date"],
            kwargs["user"],
            id=getattr(old, "id", uuid4()),
            today_work=list(kwargs["today_work"] or []),
            problems=list(kwargs["problems"] or []),
            tomorrow_plan=list(kwargs["tomorrow_plan"] or []),
            section_status=dict(kwargs["section_status"] or {}),
            status=kwargs["status"],
            confirmation_type=kwargs["confirmation_type"],
            confirmed_by_user=kwargs["confirmed_by_user"],
            quality_warning=kwargs["quality_warning"],
            completeness_score=kwargs["completeness_score"],
            input_fragments=fragments,
            pending_confirmation_at=kwargs["pending_confirmation_at"],
            auto_submit_at=kwargs["auto_submit_at"],
            last_modified_by_user=kwargs["last_modified_by_user"],
            last_modified_at=kwargs["last_modified_at"],
            submitted_at=kwargs["received_at"] if kwargs["status"] == STATUS_COMPLETED else None,
            llm_model=kwargs["llm_model"],
            llm_payload=kwargs["llm_payload"],
            source=kwargs["source"],
        )
        self.reports[report.report_date] = report
        return report

    async def lock(self, session, user_id, report_date):
        return None

    async def set_pending_interaction(self, session, report, pending_interaction):
        section_status = dict(report.section_status or {})
        if pending_interaction:
            section_status["_pending_interaction"] = pending_interaction
        else:
            section_status.pop("_pending_interaction", None)
        report.section_status = section_status
        self.reports[report.report_date] = report
        return report

    def install(self):
        targets = [
            (report_service, "get_report"),
            (report_service, "upsert_daily_report"),
            (report_service, "_acquire_report_processing_lock"),
            (report_service, "_set_pending_interaction"),
            (report_service, "today_in_timezone"),
            (report_service, "now_in_timezone"),
            (agent_executor, "get_report"),
            (agent_executor, "upsert_daily_report"),
        ]
        for module, name in targets:
            self.originals[(module, name)] = getattr(module, name)
        report_service.get_report = self.get_report
        report_service.upsert_daily_report = self.upsert
        report_service._acquire_report_processing_lock = self.lock
        report_service._set_pending_interaction = self.set_pending_interaction
        report_service.today_in_timezone = lambda timezone: self.now.date()
        report_service.now_in_timezone = lambda timezone: self.now
        agent_executor.get_report = self.get_report
        agent_executor.upsert_daily_report = self.upsert

    def restore(self):
        for (module, name), value in self.originals.items():
            setattr(module, name, value)


def compact(text: str) -> str:
    return re.sub(r"[\s，。；;：:、,.!?！？（）()\[\]【】\"'“”‘’]+", "", str(text or "")).lower()


def values_text(values: list[str]) -> str:
    return compact(" ".join(values or []))


def contains_all(values: list[str], tokens: list[str]) -> bool:
    text = values_text(values)
    return all(compact(token) in text for token in tokens)


def contains_none(values: list[str], tokens: list[str]) -> bool:
    text = values_text(values)
    return all(compact(token) not in text for token in tokens)


def check_expect(case: dict[str, Any], result: Any, store: MemoryStore) -> list[str]:
    expect = case.get("expect", {})
    errors: list[str] = []

    def report_date_matches_expect(expected_report_date: str) -> bool:
        expected_date = date.fromisoformat(expected_report_date)
        if result.report_date == expected_date:
            return True
        if case.get("expect_date_shift_base") != "online_default":
            return False
        case_now = str(case.get("now") or "")
        if not case_now:
            return False
        try:
            case_current_date = datetime.fromisoformat(case_now).date()
        except ValueError:
            return False
        if expected_date != case_current_date:
            return False
        return result.report_date == datetime.now(ZoneInfo("Asia/Shanghai")).date()

    def report_for_expect_date(report_date: str):
        expected_date = date.fromisoformat(report_date)
        current = store.reports.get(expected_date)
        if current is not None:
            return expected_date, current
        shift_base = case.get("expect_date_shift_base")
        if shift_base:
            base_date = date(2026, 6, 26) if str(shift_base) == "online_default" else date.fromisoformat(str(shift_base))
            shifted_date = expected_date + (result.report_date - base_date)
            current = store.reports.get(shifted_date)
            if current is not None:
                return shifted_date, current
        return expected_date, None
    today = list(result.today_work or [])
    problems = list(result.problems or [])
    plan = list(result.tomorrow_plan or [])
    all_fields = today + problems + plan
    message = compact(result.message or "")

    if "saved" in expect and bool(result.report_saved) != bool(expect["saved"]):
        errors.append(f"saved expected {expect['saved']} got {result.report_saved}")
    if "status" in expect and result.status != STATUS_MAP.get(expect["status"], expect["status"]):
        errors.append(f"status expected {expect['status']} got {result.status}")
    if "status_in" in expect and result.status not in {STATUS_MAP.get(v, v) for v in expect["status_in"]}:
        errors.append(f"status expected one of {expect['status_in']} got {result.status}")
    if "report_date" in expect and not report_date_matches_expect(expect["report_date"]):
        errors.append(f"report_date expected {expect['report_date']} got {result.report_date.isoformat()}")
    if "reply_kind" in expect and result.reply_kind != expect["reply_kind"]:
        errors.append(f"reply_kind expected {expect['reply_kind']} got {result.reply_kind}")
    if "reply_kind_not" in expect and result.reply_kind in set(expect["reply_kind_not"]):
        errors.append(f"reply_kind should not be {result.reply_kind}")
    if "confirmed_by_user" in expect and bool(result.confirmed_by_user) != bool(expect["confirmed_by_user"]):
        errors.append(f"confirmed_by_user expected {expect['confirmed_by_user']} got {result.confirmed_by_user}")

    field_checks = [
        ("today_contains", today, contains_all),
        ("problem_contains", problems, contains_all),
        ("plan_contains", plan, contains_all),
        ("all_contains", all_fields, contains_all),
        ("today_absent", today, contains_none),
        ("problem_absent", problems, contains_none),
        ("plan_absent", plan, contains_none),
        ("all_absent", all_fields, contains_none),
    ]
    for key, values, fn in field_checks:
        if key in expect and not fn(values, expect[key]):
            errors.append(f"{key} failed tokens={expect[key]} values={values}")

    for key, values in (("today_count", today), ("problem_count", problems), ("plan_count", plan)):
        if key in expect and len(values) != int(expect[key]):
            errors.append(f"{key} expected {expect[key]} got {len(values)} values={values}")
    for key, values in (("today_count_max", today), ("problem_count_max", problems), ("plan_count_max", plan)):
        if key in expect and len(values) > int(expect[key]):
            errors.append(f"{key} expected <= {expect[key]} got {len(values)} values={values}")
    for key, values in (("today_exact", today), ("problem_exact", problems), ("plan_exact", plan)):
        if key in expect and values != list(expect[key]):
            errors.append(f"{key} expected {expect[key]} got {values}")
    for key, values in (("today_item_contains_all", today), ("problem_item_contains_all", problems), ("plan_item_contains_all", plan)):
        for tokens in expect.get(key, []):
            if not any(contains_all([item], list(tokens)) for item in values):
                errors.append(f"{key} failed tokens={tokens} values={values}")
    for token in expect.get("message_contains", []):
        if compact(token) not in message:
            errors.append(f"message missing {token!r}: {result.message[:200]}")
    for token in expect.get("message_absent", []):
        if compact(token) in message:
            errors.append(f"message should not contain {token!r}: {result.message[:200]}")
    for report_date, snapshot in (expect.get("source_unchanged") or {}).items():
        actual_date, current = report_for_expect_date(report_date)
        if current is None:
            errors.append(f"source report {report_date} missing")
            continue
        for field, expected in snapshot.items():
            if list(getattr(current, field) or []) != expected:
                errors.append(f"source {actual_date.isoformat()}.{field} changed to {getattr(current, field)}")
    for report_date, snapshot in (expect.get("target_reports") or {}).items():
        actual_date, current = report_for_expect_date(report_date)
        if current is None:
            errors.append(f"target report {report_date} missing")
            continue
        for field, expected in snapshot.items():
            actual = getattr(current, field, None)
            if field in {"today_work", "problems", "tomorrow_plan"}:
                actual = list(actual or [])
                expected = list(expected or [])
            if actual != expected:
                errors.append(f"target {actual_date.isoformat()}.{field} expected {expected} got {actual}")
    return errors


def group(issue_id: str, base: dict[str, Any], variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    for index, variant in enumerate(variants, start=1):
        merged = copy.deepcopy(base)
        merged.update(copy.deepcopy(variant))
        merged.setdefault("mode", "service")
        merged["issue_id"] = issue_id
        merged["name"] = f"{issue_id}-{index:02d}-{variant.get('name', 'variant')}"
        cases.append(merged)
    return cases


def issue_cases() -> list[dict[str, Any]]:
    full_pending = {
        "today_work": ["审核3份合同", "整理用印材料", "沟通服务器方案"],
        "problems": ["暂无明显问题"],
        "tomorrow_plan": ["明天继续推进"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True, "problems_acknowledged_empty": True},
        "status": "pending_confirmation",
        "completeness_score": 1.0,
    }
    collecting_full = {**full_pending, "status": "collecting"}
    completed_full = {**full_pending, "status": "completed"}
    today_only = {"today_work": ["审核3份合同"], "section_status": {"today_work": True}, "status": "collecting", "completeness_score": 0.33}
    today_problem = {
        "today_work": ["审核3份合同"],
        "problems": ["暂无明显问题"],
        "section_status": {"today_work": True, "problems": True, "problems_acknowledged_empty": True},
        "status": "collecting",
        "completeness_score": 0.67,
    }
    numbered = {
        "today_work": ["处理部门费用", "更新函件管理办法", "整理归档材料", "沟通服务器方案", "完善日报系统"],
        "problems": ["暂无明显问题"],
        "tomorrow_plan": ["明天继续推进"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True},
        "status": "pending_confirmation",
        "completeness_score": 1.0,
    }
    previous_plan = {
        "report_date": "2026-06-25",
        "today_work": ["日常用印流程审批"],
        "problems": ["暂无明显问题"],
        "tomorrow_plan": ["日常用印流程审批", "现场用印审核", "电子章用印", "未归档合同催收", "用印事宜咨询答复", "合同归档整理移交资料室"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True},
        "status": "completed",
        "completeness_score": 1.0,
    }
    previous_plan_with_future_shell = {
        "report_date": "2026-06-25",
        "today_work": ["mcp \u65b9\u6848\u8bbe\u8ba1"],
        "problems": ["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        "tomorrow_plan": ["\u660e\u5929\u7ee7\u7eed\u5b8c\u5584mcp\u5e76\u5f00\u59cb\u6d4b\u8bd5", "\u660e\u5929\u5f00\u59cb\u505a\u5468\u62a5agent\u7684outbox"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True},
        "status": "completed",
        "completeness_score": 1.0,
    }
    copied_source = {
        "report_date": "2026-06-24",
        "today_work": ["日报系统优化    整理被告数据    完成合同线上确认"],
        "problems": ["没碰到什么问题"],
        "tomorrow_plan": ["明天等额度刷新后开始做日报系统 outbox"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True},
        "status": "completed",
        "completeness_score": 1.0,
    }

    cases: list[dict[str, Any]] = []
    cases += group("DR-001", {"expect": {"status": "pending_confirmation", "today_contains": ["合同"], "plan_contains": ["归档"]}}, [
        {"name": "fragmented-basic", "messages": ["今天审核合同三份。", "没有风险。", "明天继续跟归档。"]},
        {"name": "fragmented-colloquial", "messages": ["上午主要看了几个合同", "问题这块暂时没有", "明天还是盯资料归档"]},
        {"name": "fragmented-reordered", "messages": ["明天先跟进归档", "今天把合同审核完了", "风险暂无"]},
    ])
    cases += group("DR-002", {"initial": full_pending, "expect": {"status": "completed", "confirmed_by_user": True}}, [
        {"name": "confirm-with-thanks", "messages": ["确认提交，辛苦"]},
        {"name": "confirm-ok", "messages": ["没问题，就按这个提交"]},
        {"name": "confirm-short", "messages": ["可以，提交吧"]},
    ])
    cases += group("DR-003", {"initial": collecting_full, "expect": {"saved": True, "today_count": 0, "problem_count": 0, "plan_count": 0}}, [
        {"name": "clear-current", "messages": ["把当前草稿清掉"]},
        {"name": "clear-start-over", "messages": ["这份不要了，重新来"]},
        {"name": "clear-all-report", "messages": ["今天日报内容全部清空"], "expect": {"saved": True, "today_count": 0, "problem_count": 0, "plan_count": 0, "message_contains": ["清空"]}},
    ])
    cases += group("DR-004", {"initial": completed_full, "expect": {"status": "completed", "message_contains": ["提交"]}}, [
        {"name": "completed-edit-work", "messages": ["把今日工作第一条改成审核5份合同"]},
        {"name": "completed-rewrite", "messages": ["这份已提交日报重写一下，今天处理诉讼材料"]},
        {"name": "completed-delete", "messages": ["已提交那份日报删掉第二条"]},
    ])
    cases += group("DR-005", {"initial": today_only, "expect": {"saved": False, "all_absent": ["谢谢", "哈哈", "测试"]}}, [
        {"name": "thanks", "messages": ["谢谢你"]},
        {"name": "joke", "messages": ["哈哈先不填了"]},
        {"name": "test-noise", "messages": ["测试一下别写进去"]},
    ])
    cases += group("DR-006", {"expect": {"saved": True}}, [
        {"name": "no-problem-slot", "initial": today_only, "messages": ["没有碰到问题"], "expect": {"problem_contains": ["暂无"]}},
        {"name": "normal-problem-slot", "initial": today_only, "messages": ["正常推进，没风险"], "expect": {"problem_contains": ["暂无"]}},
        {"name": "no-plan-slot", "initial": today_problem, "messages": ["明天暂时无安排"], "expect": {"plan_contains": ["无"]}},
    ])
    cases += group("DR-007", {"initial": full_pending, "expect": {"problem_contains": ["客户", "材料"]}}, [
        {"name": "append-problem-flow", "messages": ["我想补充一下", "问题", "客户材料还没到"]},
        {"name": "append-risk-flow", "messages": ["加一条内容", "风险那块", "供应商资料没有发全"], "expect": {"problem_contains": ["供应商", "资料"]}},
        {"name": "append-plan-flow", "messages": ["补充", "明日计划", "继续催客户补材料"], "expect": {"plan_contains": ["客户", "材料"]}},
    ])
    cases += group("DR-008", {"expect": {"message_contains": ["具体"]}}, [
        {"name": "vague-project", "messages": ["今天处理了项目，没问题，明天继续。"]},
        {"name": "vague-system", "messages": ["今天优化了系统，风险暂无，明天接着弄。"]},
        {"name": "vague-skill", "messages": ["今天恢复了技能，没啥问题，明天再看。"]},
    ])
    cases += group("DR-009", {"initial": {"today_work": ["线上签署1份补充合同"], "problems": ["暂无明显问题"], "tomorrow_plan": ["明天继续推进"], "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True}, "status": "pending_confirmation"}, "expect": {"today_contains": ["增补合同"], "today_absent": ["补充合同"]}}, [
        {"name": "replace-contract", "messages": ["补充合同改成增补合同"]},
        {"name": "replace-wording", "messages": ["刚才那个补充合同不是补充，是增补"]},
        {"name": "replace-local", "messages": ["今日工作里的补充合同换成增补合同"]},
    ])
    cases += group("DR-010", {"initial": numbered, "expect": {"today_absent": ["更新函件管理办法"]}}, [
        {"name": "delete-second", "messages": ["删除今日工作第2条"]},
        {"name": "delete-text", "messages": ["更新函件管理办法这条删掉"]},
        {"name": "delete-range", "messages": ["第2条不要了"]},
    ])
    cases += group("DR-011", {"initial": numbered, "expect": {"today_contains": ["更新函件管理办法"]}}, [
        {"name": "undo-delete", "messages": ["删掉第二条", "恢复刚才删掉的"]},
        {"name": "undo-remove", "messages": ["更新函件管理办法删除", "撤销刚才那个删除"]},
        {"name": "undo-back", "messages": ["第二条先删了", "不对，加回来"]},
    ])
    cases += group("DR-012", {"initial": numbered, "expect": {"today_count_max": 4, "today_contains": ["更新函件管理办法", "整理归档材料"]}}, [
        {"name": "merge-range", "messages": ["今日工作第2到第3条是一件事"]},
        {"name": "merge-colloquial", "messages": ["2、3这两条合成一条吧"]},
        {"name": "merge-typo", "messages": ["和并今日工作的2，3"]},
    ])
    cases += group("DR-013", {"initial": numbered, "expect": {"today_count_max": 4, "message_absent": ["没理解稳妥"]}}, [
        {"name": "no-write-merge-safe", "messages": ["这几条是一回事，2到3合并"]},
        {"name": "no-write-delete-safe", "messages": ["今日工作的第二条删掉"]},
        {"name": "no-fake-success", "messages": ["把函件改成邮件"], "expect": {"today_contains": ["邮件"], "message_absent": ["没理解稳妥"]}},
    ])
    cases += group("DR-014", {"initial": completed_full, "expect": {"status": "collecting", "message_contains": ["撤回", "日报"]}}, [
        {"name": "unsubmit", "messages": ["撤回这份日报"]},
        {"name": "withdraw", "messages": ["刚提交的日报先撤回"]},
        {"name": "unsubmit-then-edit", "messages": ["我要撤回日报改一下"]},
    ])
    cases += group("DR-015", {"reports": [{"report_date": "2026-06-25", **full_pending}], "expect": {"message_contains": ["2026-06-25"]}}, [
        {"name": "query-yesterday", "messages": ["昨天日报发我看下"]},
        {"name": "query-plan", "messages": ["看下昨天计划"]},
        {"name": "query-followup", "messages": ["昨天那份日报给我"]},
    ])
    cases += group("DR-016", {"reports": [previous_plan], "expect": {"today_contains": ["日常用印", "现场用印", "电子章", "未归档", "咨询"], "today_absent": ["合同归档整理移交资料室"]}}, [
        {"name": "range-1-5", "messages": ["昨天计划1到5项都完成了，第6项没做"]},
        {"name": "except-six", "messages": ["昨日明日计划除了第六条，其他都已完成"]},
        {"name": "front-five", "messages": ["昨天安排的前五项今天正常完成，最后一项没有"]},
    ])
    cases += group("DR-017", {"reports": [previous_plan], "expect": {"plan_absent": ["合同归档整理移交资料室"]}}, [
        {"name": "unfinished-not-rollover", "messages": ["昨天除了合同归档整理移交资料室没做，其他完成"]},
        {"name": "six-not-done", "messages": ["第6项未完成，其余昨日计划完成"]},
        {"name": "last-not-done", "messages": ["昨天计划最后一项没来得及，其余做完了"]},
    ])
    cases += group("DR-042", {"reports": [previous_plan], "expect": {"today_contains": ["\u65e5\u5e38\u7528\u5370", "\u73b0\u573a\u7528\u5370", "\u7535\u5b50\u7ae0", "\u672a\u5f52\u6863"], "problem_contains": ["\u82cf\u5efa\u9662", "\u501f\u7ae0"], "plan_contains": ["\u65e5\u5e38\u7528\u5370", "\u73b0\u573a\u7528\u5370"], "plan_absent": ["\u82cf\u5efa\u9662", "\u95ee\u9898"]}}, [
        {"name": "previous-plan-same-tomorrow-problem", "messages": ["\u4eca\u5929\u5b8c\u6210\u4e86\u6628\u5929\u7684\u8ba1\u5212\uff0c\u7136\u540e\u660e\u5929\u7684\u8ba1\u5212\u7167\u7740\u6628\u5929\u7684\u8ba1\u5212\u6765\u3002\u4eca\u65e5\u82cf\u5efa\u9662\u501f\u7ae0\u6d41\u7a0b\u8d70\u5230\u5f20\u65b0\u7ea2\u603b\u88ab\u9000\u56de\uff0c\u5f15\u7533\u51fa\u4f53\u5916\u516c\u53f8\u7684\u501f\u7ae0\u6d41\u7a0b\u95ee\u9898\u3002"]},
    ])
    cases += group("DR-043", {"reports": [previous_plan_with_future_shell], "expect": {"today_contains": ["\u5b8c\u5584mcp", "\u5468\u62a5agent"], "today_absent": ["\u660e\u5929\u7ee7\u7eed", "\u660e\u5929\u5f00\u59cb"], "plan_absent": ["\u660e\u5929\u7ee7\u7eed", "\u660e\u5929\u5f00\u59cb"]}}, [
        {"name": "previous-plan-complete-strip-future-shell", "messages": ["\u6628\u5929\u7684\u8ba1\u5212\u5168\u90e8\u5b8c\u6210"]},
    ])
    cases += group("DR-018", {"reports": [{"report_date": "2026-06-25", **full_pending}], "expect": {"report_date": "2026-06-26", "today_contains": ["合同"]}}, [
        {"name": "reference-yesterday", "now": "2026-06-26T10:30:00", "messages": ["参考昨天日报，今天完成合同审核，问题暂无，明天继续推进"]},
        {"name": "reference-date", "now": "2026-06-26T10:30:00", "messages": ["按6月25日那份作参考，今天处理合同审核，没风险，明天跟进"]},
        {"name": "mention-not-edit", "now": "2026-06-26T10:30:00", "messages": ["昨天日报不用动，今天就写合同审核，问题没有，明天推进"]},
    ])
    cases += group("DR-019", {"reports": [copied_source], "expect": {"reply_kind": "recent_report_copy_to_today", "today_contains": ["日报系统优化", "整理被告数据"], "message_contains": ["已整篇复制"]}}, [
        {"name": "copy-date", "messages": ["把6月24日日报整篇复制到今天"]},
        {"name": "copy-foreday", "messages": ["复制前天日报"]},
        {"name": "copy-date-short", "messages": ["6月24日那份日报复制过来"]},
    ])
    cases += group("DR-020", {"reports": [copied_source], "expect": {"today_count": 3, "message_contains": ["1.", "2.", "3."]}}, [
        {"name": "copy-split-spaces", "messages": ["复制6月24日日报"]},
        {"name": "whole-copy-split", "messages": ["整篇复制6月24日日报到今天"]},
        {"name": "copy-source-numbered-preview", "messages": ["把前天日报带到今天"]},
    ])
    cases += group("DR-021", {"initial": full_pending, "reports": [{**copied_source, "report_date": "2026-06-25", "today_work": ["昨天工作A"], "tomorrow_plan": ["今天计划B"]}], "expect": {"reply_kind": "recent_report_copy_to_today", "today_contains": ["昨天工作A"], "message_absent": ["选择性复制", "全部覆盖"]}}, [
        {"name": "query-then-cover-all", "messages": ["昨天日报发我下", "全部覆盖"]},
        {"name": "query-then-cover", "messages": ["昨天那份日报看看", "覆盖"]},
        {"name": "query-then-whole", "messages": ["昨天日报展示一下", "整篇复制"]},
    ])
    cases += group("DR-022", {"reports": [{**copied_source, "report_date": "2026-06-25", "today_work": ["昨天工作A"]}], "expect": {"reply_kind": "recent_report_copy_to_today", "today_contains": ["昨天工作A"]}}, [
        {"name": "copy-yesterday-de", "messages": ["复制昨天的日报"]},
        {"name": "copy-yesterday-de-to-today", "messages": ["把昨天的日报复制到今天"]},
        {"name": "use-yesterday-de", "messages": ["用昨天的日报作为今天草稿"]},
    ])
    cases += group("DR-023", {"expect": {"today_contains": ["日常用印流程审批", "现场用印审核"], "today_absent": ["过去今日工作"]}}, [
        {"name": "paste-reference-then-complete", "messages": ["当前填报日期：2026-06-25\n今日工作：过去今日工作\n问题/风险：暂无\n明日计划：1. 日常用印流程审批\n2. 现场用印审核\n3. 电子章用印", "昨天计划前两项完成"]},
        {"name": "paste-reference-all", "messages": ["昨日计划：1. 日常用印流程审批 2. 现场用印审核 3. 电子章用印", "这些前两条都完成了"]},
        {"name": "paste-reference-text", "messages": ["昨天日报里明日计划有日常用印流程审批、现场用印审核、电子章用印", "前两个今天做完"]},
    ])
    cases += group("DR-024", {"expect": {"today_count_max": 4}}, [
        {"name": "granularity-system-plan", "messages": ["今天制定日报系统下一步计划：与底表结合、构建案件系统、自动关联日报中提及案件、固定节点询问进展。问题暂无，明天继续。"]},
        {"name": "granularity-one-project", "messages": ["今天推进案件进展系统方案，包括底表、关联、节点提醒和负责人摘要，没问题，明天继续细化。"]},
        {"name": "granularity-not-too-split", "messages": ["今天做了日报系统案件协同方案，里面包含进展抽取、出差协同和outbox，暂无风险，明天继续开发。"]},
    ])
    cases += group("DR-025", {"expect": {"status": "pending_confirmation"}}, [
        {"name": "long-legal-risk", "messages": ["今天走访项目并梳理合同关系，发现签证材料不足、工期索赔口径不清，存在经营风险。明天继续整理会议材料。"], "expect": {"today_contains": ["项目"], "problem_contains": ["签证", "工期"]}},
        {"name": "long-wage-dispute", "messages": ["今天处理工人讨薪纠纷，核对升降机资料和签字文件。风险是对方可能扩大解释我方人员签字，明天补充证据清单。"], "expect": {"today_contains": ["讨薪", "升降机"], "problem_contains": ["签字"]}},
        {"name": "long-lawsuit", "messages": ["今天开庭并整理代理意见，法官态度偏向被告且关键签证不足，败诉风险较高，明天完善补充材料。"], "expect": {"today_contains": ["开庭", "代理意见"], "problem_contains": ["败诉"]}},
    ])
    cases += group("DR-026", {"expect": {"status": "pending_confirmation"}}, [
        {"name": "same-sentence", "messages": ["今天处理合同审核，问题是资料缺失，明天补材料。"]},
        {"name": "same-sentence-risk", "messages": ["完成用印审批，但风险是客户材料没齐，明天继续催。"], "expect": {"today_contains": ["用印"], "problem_contains": ["客户", "材料"], "plan_contains": ["催"]}},
        {"name": "same-sentence-plan", "messages": ["今天整理归档，暂无明显问题，明天把缺的材料补齐。"], "expect": {"today_contains": ["归档"], "problem_contains": ["暂无"], "plan_contains": ["材料"]}},
    ])
    cases += group("DR-027", {"now": "2026-06-26T10:30:00", "reports": [{"report_date": "2026-06-25", **full_pending}], "expect": {"message_contains": ["2026-06-25"]}}, [
        {"name": "after-cutoff-query", "messages": ["昨天日报发我看下"]},
        {"name": "after-cutoff-plan-query", "messages": ["昨天计划是什么"]},
        {"name": "after-cutoff-display", "messages": ["查看6月25日日报"]},
    ])
    cases += group("DR-028", {"now": "2026-06-26T15:00:00", "expect": {"report_date": "2026-06-26", "today_contains": ["合同"]}}, [
        {"name": "same-day-after-nine", "messages": ["今天审核合同，问题暂无，明天继续"]},
        {"name": "afternoon-fill", "messages": ["下午补一下今天日报：处理用印审核，无风险，明天归档"], "expect": {"report_date": "2026-06-26", "today_contains": ["用印"]}},
        {"name": "today-explicit", "messages": ["写今天的，完成合同确认，没问题，明天推进"]},
    ])
    cases += group("DR-037", {"now": "2026-06-26T10:30:00", "reports": [{"report_date": "2026-06-25", **full_pending}], "expect": {"saved": False, "message_contains": ["不能"]}}, [
        {"name": "delete-yesterday", "messages": ["删除昨天日报"]},
        {"name": "clear-yesterday", "messages": ["把昨天那份日报清空"], "expect": {"message_contains": ["日报"]}},
        {"name": "do-not-want-yesterday", "messages": ["昨天日报不要了，删掉"]},
    ])
    cases += group("DR-038", {"initial": {"section_status": {"_recent_report_context": {"kind": "viewed_report", "owner": "other", "can_copy_to_today": False, "report_date": "2026-06-25", "field": "all"}}, "status": "collecting"}, "reports": [{"report_date": "2026-06-25", **full_pending}], "expect": {"saved": False, "message_contains": ["只能查看"]}}, [
        {"name": "other-copy-whole", "messages": ["整篇复制"]},
        {"name": "other-copy-this", "messages": ["把这个复制到今天"]},
        {"name": "other-use-it", "messages": ["就用这份"]},
    ])
    split_pending = {
        "today_work": ["日常用印资料审核"],
        "problems": [],
        "tomorrow_plan": ["明日计划来函进展继续跟进闭环，帮助律师归还借阅资料"],
        "section_status": {
            "_pending_interaction": {
                "type": "awaiting_clarification",
                "operation": "split_or_append",
                "target_field": "tomorrow_plan",
                "context": {
                    "current_item_text": "明日计划来函进展继续跟进闭环，帮助律师归还借阅资料",
                    "current_item_index": 1,
                    "split_suggestion": ["来函进展继续跟进闭环", "帮助律师归还借阅资料"],
                },
            }
        },
        "status": "collecting",
    }
    local_delete_initial = {
        "today_work": ["上海机载（机器的机载重的载）项目评审"],
        "problems": ["自然人身份证材料待补充"],
        "tomorrow_plan": ["上石项目木石面扣款汇报"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True},
        "status": "pending_confirmation",
    }
    cases += group("DR-039", {}, [
        {
            "name": "split-confirmation-from-pending",
            "initial": split_pending,
            "messages": ["对的"],
            "expect": {
                "saved": True,
                "plan_count": 2,
                "plan_contains": ["来函进展继续跟进闭环", "帮助律师归还借阅资料"],
                "message_absent": ["还缺拆分后的具体内容", "没理解稳妥"],
            },
        },
        {
            "name": "local-delete-content-not-clear",
            "initial": local_delete_initial,
            "messages": ["上海记载是对的，括号的内容删掉。第二个就是上实实是实在的实"],
            "expect": {
                "saved": True,
                "message_absent": ["确认清空", "清空当前日报", "清空草稿"],
                "today_contains": ["上海"],
                "problem_contains": ["自然人", "身份证"],
                "plan_contains": ["上实", "木石面"],
            },
        },
    ])

    report_630_problem = {
        "today_work": ["整理供应商退款诉讼材料"],
        "problems": ["供应商不退回预付款，我方起诉能否要求供应商退还预付款且不承担违约责任。实际上，业主和供应商之间成立了事实的合同关系，金螳螂仅为受托方"],
        "tomorrow_plan": ["名苗项目起诉材料的准备", "机载项目评审", "南通强一项目评审", "协助资金飞速收款"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True},
        "status": "pending_confirmation",
        "completeness_score": 1.0,
    }
    cases += group("DR-040", {}, [
        {
            "name": "numbered-shell-no-risk-continue",
            "messages": ["今日工作：1. 下周的智能化测试会议，暂时没有时间去推进技能的迭代\n问题/风险：风险暂无\n明日计划：1. 明天工作计划今天一样的内容"],
            "expect": {
                "saved": True,
                "today_exact": ["下周的智能化测试会议，暂时没有时间去推进技能的迭代"],
                "problem_exact": ["暂无明显问题"],
                "plan_exact": ["继续今日工作"],
                "all_absent": ["1.", "风险暂无", "明天工作计划今天一样的内容"],
            },
        },
        {
            "name": "standalone-risk-empty",
            "initial": today_only,
            "messages": ["风险暂无"],
            "expect": {
                "saved": True,
                "problem_exact": ["暂无明显问题"],
                "problem_absent": ["风险暂无"],
            },
        },
        {
            "name": "delete-semantic-entrusted-party",
            "initial": report_630_problem,
            "messages": ["问题与风险里面金螳螂仅为受委托方这几个字删掉"],
            "expect": {
                "saved": True,
                "problem_absent": ["金螳螂仅为受托方", "金螳螂仅为受委托方"],
                "message_absent": ["没找到", "哪一条", "没理解稳妥"],
            },
        },
        {
            "name": "spelled-non-litigation-correction",
            "initial": report_630_problem,
            "messages": ["第四个是协助资金飞速收款，飞是是非的非，诉是诉讼的诉，改一下"],
            "expect": {
                "saved": True,
                "plan_contains": ["协助资金非诉收款"],
                "plan_absent": ["飞速"],
                "message_absent": ["没找到", "哪一条", "没理解稳妥"],
            },
        },
        {
            "name": "shared-predicate-entity-series",
            "messages": ["今天沟通了淮南，滨体，督军府等案件的收款，参加中南集团一债会，沟通分公司账户冻结问题。问题暂无。明天继续跟进收款。"],
            "expect": {
                "saved": True,
                "status": "pending_confirmation",
                "today_count_max": 3,
                "today_item_contains_all": [["淮南", "滨体", "督军府", "收款"]],
                "problem_exact": ["暂无明显问题"],
            },
        },
    ])

    panghao_old_630 = {
        "report_date": "2026-06-30",
        "today_work": ["完成基础 SkillHub 上 MCP 的搭建", "沟通法务待完成需求"],
        "problems": ["暂无明显问题"],
        "tomorrow_plan": ["完成周报填写的发送", "分公司被告案件数据统计", "SkillHub 上线", "修复 6 月 30 日日报系统出现的问题"],
        "section_status": {"today_work": True, "problems": True, "tomorrow_plan": True, "problems_acknowledged_empty": True},
        "status": "completed",
        "completeness_score": 1.0,
        "confirmed_by_user": True,
    }
    panghao_replacement_text = "今日工作：\n1. mcp开始测试\n2.周报内容测试\n问题/风险：\n暂无明显问题\n\n明日计划：\n1.周报发布填写\n2.被告数据统计"
    panghao_replacement_target = {
        "today_work": ["mcp开始测试", "周报内容测试"],
        "problems": ["暂无明显问题"],
        "tomorrow_plan": ["周报发布填写", "被告数据统计"],
        "status": STATUS_COMPLETED,
    }
    panghao_current_701 = {
        "today_work": [],
        "problems": [],
        "tomorrow_plan": [],
        "section_status": {},
        "status": "collecting",
        "completeness_score": 0.0,
    }
    cases += group("DR-041", {"now": "2026-07-01T00:58:00", "expect_date_shift_base": "online_default", "initial": panghao_current_701, "reports": [panghao_old_630]}, [
        {
            "name": "edit-entry",
            "messages": ["修改6月30日的日报"],
            "expect": {
                "saved": True,
                "report_date": "2026-07-01",
                "message_contains": ["2026-06-30"],
                "source_unchanged": {"2026-06-30": {"today_work": panghao_old_630["today_work"], "problems": panghao_old_630["problems"], "tomorrow_plan": panghao_old_630["tomorrow_plan"]}},
            },
        },
        {
            "name": "replace-confirm",
            "messages": ["修改6月30日的日报", panghao_replacement_text],
            "expect": {
                "report_date": "2026-07-01",
                "message_contains": ["已整体替换"],
                "target_reports": {"2026-06-30": panghao_replacement_target},
            },
        },
        {
            "name": "template-replace",
            "messages": ["修改6月30日的日报", "当前填报日期：2026-06-30\n日报（2026-06-30）：\n今日工作：\n1. mcp开始测试\n2.周报内容测试\n问题/风险：\n暂无明显问题\n\n明日计划：\n1.周报发布填写\n2.被告数据统计"],
            "expect": {
                "report_date": "2026-07-01",
                "message_contains": ["已整体替换"],
                "target_reports": {"2026-06-30": panghao_replacement_target},
            },
        },
        {
            "name": "content-date-current",
            "messages": ["今日工作改成 今天完成了基础skillhub上mcp的搭建 沟通了法务待完成需求 明天计划完成周报填写的发送 分公司被告案件数据的统计 skillhub上线 修复6月30日日报系统出现的问题"],
            "expect": {
                "saved": True,
                "report_date": "2026-07-01",
                "source_unchanged": {"2026-06-30": {"today_work": panghao_old_630["today_work"], "problems": panghao_old_630["problems"], "tomorrow_plan": panghao_old_630["tomorrow_plan"]}},
            },
        },
        {
            "name": "dated-clear-confirm",
            "messages": ["清空6月30日的日报"],
            "expect": {
                "saved": False,
                "message_contains": ["已清空"],
                "message_absent": ["历史日报不能删除"],
                "target_reports": {"2026-06-30": {"today_work": [], "problems": [], "tomorrow_plan": [], "status": STATUS_COLLECTING}},
            },
        },
    ])

    documented = {
        "DR-029": ["同一用户同时发三段日报", "重复 message_id 再投递一次", "确认和修改同一秒到达"],
        "DR-030": ["70 个用户同时填报", "两个用户同一句话并发提交", "团队晨报同时读取多用户草稿"],
        "DR-031": ["手工接口同一 idempotency_key 连续请求", "manual report payload 带 report_date", "重复外部消息返回已有结果"],
        "DR-032": ["无 token 打开 admin learning", "错误 token 请求 habit 更新", "健康检查不需要 admin 鉴权"],
        "DR-033": ["第一次提醒后不重复发", "待确认日报发确认提醒", "非白名单禁止真实提醒发送"],
        "DR-034": ["outbox 写失败不影响日报回复", "worker 重复消费同一事件", "stale processing lock 恢复 pending"],
        "DR-035": ["发现候选习惯但不自动生效", "禁用某用户习惯后不进入 prompt", "ASR 纠错习惯只影响本用户"],
        "DR-036": ["草稿未确认进入晨报风险", "多轮 no_change 标为卡住", "未填用户进入缺失清单"],
    }
    for issue_id, variants in documented.items():
        cases += group(issue_id, {"mode": "documented"}, [{"name": f"documented-{i}", "messages": [text]} for i, text in enumerate(variants, 1)])
    return cases


async def run_service_case(service: DailyReportService, case: dict[str, Any]) -> dict[str, Any]:
    now = datetime.fromisoformat(case.get("now") or "2026-06-26T10:30:00")
    user = make_user()
    store = MemoryStore(user=user, now=now, initial=case.get("initial"), reports=case.get("reports") or [])
    store.install()
    result = None
    try:
        for message in case["messages"]:
            result = await service.submit_text(FakeSession(), user=user, raw_input=message, source="issue_ledger_simulation")
        if result is None:
            raise AssertionError("no messages were executed")
        errors = check_expect(case, result, store)
        return {
            "name": case["name"],
            "issue_id": case["issue_id"],
            "mode": "service",
            "status": "PASS" if not errors else "FAIL",
            "errors": errors,
            "messages": case["messages"],
            "reply_kind": result.reply_kind,
            "report_status": result.status,
            "saved": result.report_saved,
            "report_date": result.report_date.isoformat(),
            "today_work": result.today_work,
            "problems": result.problems,
            "tomorrow_plan": result.tomorrow_plan,
            "message": result.message,
            "timings": {
                key: result.timings.get(key)
                for key in (
                    "entered_report_agent",
                    "report_agent_intent",
                    "report_agent_output_action",
                    "state_resolver_decision",
                    "reject_reason",
                    "semantic_router_used",
                )
            },
        }
    except Exception as exc:
        return {
            "name": case["name"],
            "issue_id": case["issue_id"],
            "mode": "service",
            "status": "ERROR",
            "errors": [f"{type(exc).__name__}: {exc}"],
            "messages": case.get("messages") or [],
            "last_result": None
            if result is None
            else {
                "reply_kind": result.reply_kind,
                "report_status": result.status,
                "saved": result.report_saved,
                "today_work": result.today_work,
                "problems": result.problems,
                "tomorrow_plan": result.tomorrow_plan,
                "message": result.message,
            },
        }
    finally:
        store.restore()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", default="")
    parser.add_argument("--dump-cases", default="")
    args = parser.parse_args()

    cases = issue_cases()
    if args.filter:
        cases = [case for case in cases if args.filter in case["name"] or args.filter in case["issue_id"]]
    if args.limit:
        cases = cases[: args.limit]
    if args.dump_cases:
        Path(args.dump_cases).write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    settings = Settings()
    client = CountingLLMClient(settings)
    service = DailyReportService(settings, DailyReportExtractor(client))
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(cases, start=1):
            if case.get("mode") == "documented":
                result = {
                    "name": case["name"],
                    "issue_id": case["issue_id"],
                    "mode": "documented",
                    "status": "SKIP",
                    "messages": case.get("messages") or [],
                    "reason": "非钉钉日报对话主链路问题，已生成交互/操作方式，但不在本 runner 中执行。",
                }
            else:
                result = await run_service_case(service, case)
            results.append(result)
            print(f"PROGRESS {index}/{len(cases)} {result['status']} {result['name']}", flush=True)
    finally:
        await client.close()

    failed = [item for item in results if item["status"] in {"FAIL", "ERROR"}]
    skipped = [item for item in results if item["status"] == "SKIP"]
    passed = [item for item in results if item["status"] == "PASS"]
    summary = {
        "pass": len(passed),
        "fail": len(failed),
        "skip": len(skipped),
        "total": len(results),
        "service_total": len(results) - len(skipped),
        "llm_calls": len(client.calls),
        "seconds": round(time.perf_counter() - started, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload = {"summary": summary, "llm_calls": client.calls, "results": results}
    output = Path(args.output) if args.output else Path("outputs") / f"issue_ledger_simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ISSUE_LEDGER_SIMULATION_RESULTS_START", flush=True)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"OUTPUT {output}", flush=True)
    print("FAILURES_START", flush=True)
    for item in failed:
        print(json.dumps(item, ensure_ascii=False), flush=True)
    print("FAILURES_END", flush=True)
    print("ISSUE_LEDGER_SIMULATION_RESULTS_END", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
