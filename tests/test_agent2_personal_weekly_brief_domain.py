from __future__ import annotations

import json
import traceback
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent2.personal_weekly_brief import (
    Agent2PersonalWeeklyBriefGenerator,
    Agent2PersonalWeeklyBriefReviewer,
    Agent2PersonalWeeklyBriefModelPipeline,
    PersonalWeeklyBriefReviewRejected,
    PersonalWeeklyBriefModelOutputInvalid,
    PersonalWeeklyBriefSnapshot,
    SourceEvidence,
    _critical_literals,
    derive_personal_weekly_brief_window,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
SATURDAY_RUN = datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)


class _FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_json(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)


class _SequenceLLM:
    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    async def complete_json(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return json.dumps(self.payloads.pop(0), ensure_ascii=False)


def _source(
    source_id: str,
    *,
    kind: str,
    on_date: date,
    section: str,
    text: str,
    record_id: str = "record-1",
) -> SourceEvidence:
    return SourceEvidence(
        source_id=source_id,
        source_kind=kind,
        source_record_id=record_id,
        source_date=on_date,
        section=section,
        original_text=text,
    )


def _snapshot(*sources: SourceEvidence) -> PersonalWeeklyBriefSnapshot:
    return PersonalWeeklyBriefSnapshot(
        tenant_id="tenant-a",
        owner_user_id="11111111-1111-4111-8111-111111111111",
        week_start=date(2026, 8, 17),
        week_end=date(2026, 8, 21),
        snapshot_at=SATURDAY_RUN,
        daily_report_dates=tuple(
            sorted(
                {
                    source.source_date
                    for source in sources
                    if source.source_kind == "daily_report"
                }
            )
        ),
        weekly_plan_found=any(
            source.source_kind == "weekly_plan" for source in sources
        ),
        sources=tuple(sources),
    )


def _empty_section(note: str) -> dict:
    return {"empty_note": note, "items": []}


@pytest.mark.asyncio
async def test_generator_accepts_real_model_json_code_fence() -> None:
    raw = """```json
{"intro":"本周没有已保存的数据。","completed":{"empty_note":"没有日报今日工作记录。","items":[]},"plan_progress":{"empty_note":"没有周计划记录。","items":[]},"possible_open_loops":{"empty_note":"没有数据时不推测。","items":[]}}
```"""

    llm = _FakeLLM(raw)
    result = await Agent2PersonalWeeklyBriefGenerator(
        llm,
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert result.completed.items == ()
    assert llm.calls[0]["max_tokens"] == 8000
    assert llm.calls[0]["timeout_seconds"] == 60.0
    assert llm.calls[0]["max_retries"] == 1


@pytest.mark.asyncio
async def test_generator_keeps_large_source_audit_out_of_model_output() -> None:
    source = _source(
        "daily:audit:1",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="完成合同审核。",
    )
    llm = _FakeLLM(
        {
            "intro": "本周工作简报",
            "completed": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "contract-review",
                        "text": "完成合同审核。",
                        "source_ids": ["s001"],
                    }
                ],
            },
            "plan_progress": _empty_section("没有周计划记录。"),
            "possible_open_loops": _empty_section("没有未闭环事项。"),
        }
    )

    result = await Agent2PersonalWeeklyBriefGenerator(
        llm,
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert "不要返回 source_dispositions" in llm.calls[0]["system_prompt"]
    assert '"source_dispositions":' not in llm.calls[0]["system_prompt"]
    model_snapshot = json.loads(llm.calls[0]["user_prompt"])["trusted_snapshot"]
    assert model_snapshot["sources"][0]["source_id"] == "s001"
    assert "source_record_id" not in model_snapshot["sources"][0]
    assert result.completed.items[0].source_ids == (source.source_id,)
    assert [item.disposition for item in result.source_dispositions] == ["cited"]


def test_generator_prompt_keeps_daily_tomorrow_plan_out_of_weekly_progress() -> None:
    from app.agent2.personal_weekly_brief import _SYSTEM_PROMPT

    assert "weekly_plan_found=false" in _SYSTEM_PROMPT
    assert "日报的 tomorrow_plan 不是正式周计划" in _SYSTEM_PROMPT
    assert "plan_progress.items 必须为空" in _SYSTEM_PROMPT
    assert "同一个周计划来源不能拆成多个进展项" in _SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_independent_reviewer_accepts_real_model_json_code_fence() -> None:
    content = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周没有已保存的数据。",
                "completed": _empty_section("没有日报今日工作记录。"),
                "plan_progress": _empty_section("没有周计划记录。"),
                "possible_open_loops": _empty_section("没有数据时不推测。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    review_llm = _FakeLLM(
            "```json\n"
            '{"approved":true,"issues":[]}'
            "\n```"
        )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        review_llm,
        model="agent2-model",
    )

    review = await reviewer.review(snapshot=_snapshot(), content=content)

    assert review["approved"] is True
    assert review["request_count"] == 1
    assert review_llm.calls[0]["max_tokens"] == 8000
    assert review_llm.calls[0]["timeout_seconds"] == 60.0
    assert review_llm.calls[0]["max_retries"] == 1
    review_payload = json.loads(review_llm.calls[0]["user_prompt"])
    assert "source_dispositions" not in review_payload["draft"]
    assert review_payload["server_checks"] == {
        "recognized_amount_date_literals_complete": True,
        "frozen_source_dispositions_complete": True,
        "weekly_plan_sources_exactly_once": True,
        "section_source_bindings_valid": True,
    }


@pytest.mark.asyncio
async def test_independent_reviewer_repairs_only_invalid_response_shape() -> None:
    content = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周工作简报",
                "completed": _empty_section("没有日报今日工作记录。"),
                "plan_progress": _empty_section("没有周计划记录。"),
                "possible_open_loops": _empty_section("没有未闭环事项。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    llm = _SequenceLLM("not-json", {"approved": True, "issues": []})

    review = await Agent2PersonalWeeklyBriefReviewer(
        llm,
        model="agent2-model",
    ).review(snapshot=_snapshot(), content=content)

    assert review["approved"] is True
    assert review["request_count"] == 2
    repaired_prompt = json.loads(llm.calls[1]["user_prompt"])
    assert "review_response_repair" in repaired_prompt


def test_snapshot_rejects_unbounded_model_input() -> None:
    oversized_text = "甲" * 2001
    with pytest.raises(ValueError, match="source text is too long"):
        _snapshot(
            _source(
                "daily:oversized",
                kind="daily_report",
                on_date=date(2026, 8, 18),
                section="today_work",
                text=oversized_text,
            )
        )

    too_many = tuple(
        _source(
            f"daily:many:{index}",
            kind="daily_report",
            on_date=date(2026, 8, 17 + index % 5),
            section="today_work",
            text=f"脱敏事项{index}",
        )
        for index in range(121)
    )
    with pytest.raises(ValueError, match="too many sources"):
        _snapshot(*too_many)


def test_critical_literal_guard_covers_common_amount_and_date_formats() -> None:
    literals = set(
        _critical_literals(
            "截止2026-09-01复核，9/1再确认；金额100万、￥1,000,000，"
            "另有1,000,000元和12.5%。"
        )
    )

    assert {
        "2026-09-01",
        "9/1",
        "100万",
        "￥1,000,000",
        "1,000,000元",
        "12.5%",
    }.issubset(literals)


def test_snapshot_rejects_excessive_total_source_text() -> None:
    sources = tuple(
        _source(
            f"daily:total:{index}",
            kind="daily_report",
            on_date=date(2026, 8, 17 + index % 5),
            section="today_work",
            text=f"脱敏{index}" + "乙" * 1490,
        )
        for index in range(21)
    )

    with pytest.raises(ValueError, match="total source text is too long"):
        _snapshot(*sources)


@pytest.mark.asyncio
async def test_model_pipeline_repairs_once_after_independent_rejection() -> None:
    source = _source(
        "daily:repair:1",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="跟进甲事项，需在8月20日前回复。",
    )
    first_draft = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "matter-a",
                    "text": "跟进甲事项，需在8月20日前回复。",
                    "source_ids": [source.source_id],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有重复提示。"),
    }
    repaired_draft = {
        **first_draft,
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "matter-a",
                    "text": "跟进甲事项，需在8月20日前回复。",
                    "source_ids": [source.source_id],
                }
            ],
        },
    }
    llm = _SequenceLLM(
        first_draft,
        {
            "approved": False,
            "reviewed_matter_keys": ["matter-a"],
            "issues": [
                {"matter_key": "matter-a", "reason": "遗漏8月20日前的日期条件"}
            ],
        },
        repaired_draft,
        {
            "approved": True,
            "reviewed_matter_keys": ["matter-a"],
            "issues": [],
        },
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 2
    assert outcome.model_calls == 4
    assert "8月20日前" in outcome.content.message_text
    repair_prompt = json.loads(llm.calls[2]["user_prompt"])
    assert repair_prompt["repair_context"]["issues"][0]["matter_key"] == "matter-a"
    assert "previous_draft" in repair_prompt["repair_context"]


@pytest.mark.asyncio
async def test_reviewed_source_issue_uses_compact_id_on_generation_repair() -> None:
    source = _source(
        "daily:repair:compact-source",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="沟通甲方。",
    )
    first = {
        "intro": "本周工作简报",
        "completed": _empty_section("没有需要展示的事项。"),
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "excluded_source_ids": ["s001"],
    }
    second = {
        "intro": "本周工作简报",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "party-communication",
                    "text": "沟通甲方。",
                    "source_ids": ["s001"],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "excluded_source_ids": [],
    }
    rejected = {
        "approved": False,
        "issues": [
            {"matter_key": "s001", "reason": "该来源包含应汇总的工作。"}
        ],
    }
    approved = {"approved": True, "issues": []}
    llm = _SequenceLLM(first, rejected, second, approved)
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 2
    repair_payload = json.loads(llm.calls[2]["user_prompt"])["repair_context"]
    assert repair_payload["issues"][0]["matter_key"] == "s001"
    assert source.source_id not in json.dumps(repair_payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_unknown_review_issue_key_becomes_safe_global_repair_issue() -> None:
    source = _source(
        "daily:review-global:1",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="跟进甲事项，需在8月20日前回复。",
    )
    content = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周简报。",
                "completed": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "matter-a",
                            "text": "跟进甲事项，需在8月20日前回复。",
                            "source_ids": [source.source_id],
                        }
                    ],
                },
                "plan_progress": _empty_section("没有周计划。"),
                "possible_open_loops": _empty_section("没有重复提示。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        _FakeLLM(
            {
                "approved": False,
                "reviewed_matter_keys": ["matter-a"],
                "issues": [
                    {
                        "matter_key": "模型自拟的全局问题键",
                        "reason": "需要重新检查整份简报的事项状态。",
                    }
                ],
            }
        ),
        model="agent2-model",
    )

    with pytest.raises(PersonalWeeklyBriefReviewRejected) as caught:
        await reviewer.review(snapshot=_snapshot(source), content=content)

    assert caught.value.issues == (
        {
            "matter_key": "__brief__",
            "reason": "需要重新检查整份简报的事项状态。",
        },
    )


@pytest.mark.asyncio
async def test_reviewer_ignores_harmless_extra_explanation_field() -> None:
    content = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周没有已保存的数据。",
                "completed": _empty_section("没有日报今日工作记录。"),
                "plan_progress": _empty_section("没有周计划记录。"),
                "possible_open_loops": _empty_section("没有数据时不推测。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        _FakeLLM(
            {
                "approved": True,
                "reviewed_matter_keys": [],
                "issues": [],
                "explanation": "已逐项核对。",
            }
        ),
        model="agent2-model",
    )

    result = await reviewer.review(snapshot=_snapshot(), content=content)

    assert result["approved"] is True


@pytest.mark.asyncio
async def test_weekly_generator_and_reviewer_accept_bounded_output_budgets() -> None:
    generation_llm = _FakeLLM(
        {
            "intro": "本周没有已保存的数据。",
            "completed": _empty_section("没有日报今日工作记录。"),
            "plan_progress": _empty_section("没有周计划记录。"),
            "possible_open_loops": _empty_section("没有数据时不推测。"),
        }
    )
    content = await Agent2PersonalWeeklyBriefGenerator(
        generation_llm,
        model="agent2-model",
        max_tokens=4000,
    ).generate(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    review_llm = _FakeLLM(
        {"approved": True, "reviewed_matter_keys": [], "issues": []}
    )
    await Agent2PersonalWeeklyBriefReviewer(
        review_llm,
        model="agent2-model",
        max_tokens=2000,
    ).review(snapshot=_snapshot(), content=content)

    assert generation_llm.calls[0]["max_tokens"] == 4000
    assert review_llm.calls[0]["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_server_supplies_safe_note_for_an_empty_section() -> None:
    plan = _source(
        "plan:empty-note",
        kind="weekly_plan",
        on_date=date(2026, 8, 18),
        section="plan_item",
        text="准备甲案件庭审材料。",
    )
    result = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周简报。",
                "completed": _empty_section("本周没有日报今日工作记录。"),
                "plan_progress": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "case-a",
                            "text": "准备甲案件庭审材料。",
                            "status": "暂时没有找到后续记录",
                            "source_ids": [plan.source_id],
                        }
                    ],
                },
                "possible_open_loops": {"empty_note": "", "items": []},
                "source_dispositions": [
                    {
                        "source_id": plan.source_id,
                        "disposition": "cited",
                        "reason": "",
                    }
                ],
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(plan),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert result.possible_open_loops.empty_note == (
        "本周暂时没有找到需要单独提醒的未闭环事项。"
    )


@pytest.mark.asyncio
async def test_cited_source_cannot_drop_exact_amount_or_date_literals() -> None:
    source = _source(
        "daily:critical-literals",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="跟进甲项目120万元争议，对方原定8月20日前回复。",
    )
    payload = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "project-a",
                    "text": "跟进甲项目争议，等待对方回复。",
                    "source_ids": [source.source_id],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有重复提示。"),
        "source_dispositions": [
            {
                "source_id": source.source_id,
                "disposition": "cited",
                "reason": "",
            }
        ],
    }

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(payload),
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(source),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    assert "跟进甲项目120万元争议" in caught.value.repair_detail
    assert "对方原定8月20日前回复" in caught.value.repair_detail
    assert "甲项目" not in str(caught.value)
    assert caught.value.__cause__ is None
    rendered_traceback = "".join(traceback.format_exception(caught.value))
    assert "甲项目" not in rendered_traceback
    assert "120万元" not in rendered_traceback

    excluded_payload = {
        "intro": "本周简报。",
        "completed": _empty_section("没有今日工作事项。"),
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "source_dispositions": [
            {
                "source_id": source.source_id,
                "disposition": "safely_excluded",
                "reason": "模型认为无需展示。",
            }
        ],
    }
    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as excluded:
        await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(excluded_payload),
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(source),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "sources with critical facts" in excluded.value.repair_detail
    assert "甲项目" not in str(excluded.value)


@pytest.mark.asyncio
async def test_model_output_can_preserve_explicit_condition_and_negation() -> None:
    source = _source(
        "daily:critical-semantics",
        kind="daily_report",
        on_date=date(2026, 8, 20),
        section="today_work",
        text="对方没有承诺付款，表示只有付款条件确认后才答复。",
    )
    result = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周简报。",
                "completed": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "payment-followup",
                            "text": (
                                "继续跟进付款事项；对方没有承诺付款，"
                                "表示只有付款条件确认后才答复。"
                            ),
                            "source_ids": [source.source_id],
                        }
                    ],
                },
                "plan_progress": _empty_section("没有周计划。"),
                "possible_open_loops": _empty_section("没有重复提示。"),
                "source_dispositions": [
                    {
                        "source_id": source.source_id,
                        "disposition": "cited",
                        "reason": "",
                    }
                ],
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert "对方没有承诺付款" in result.message_text
    assert "表示只有付款条件确认后才答复" in result.message_text


@pytest.mark.asyncio
async def test_all_missing_critical_sources_are_reported_in_one_repair() -> None:
    first = _source(
        "daily:missing:first",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="甲项目金额100万，9/1前回复。",
    )
    second = _source(
        "daily:missing:second",
        kind="daily_report",
        on_date=date(2026, 8, 19),
        section="today_work",
        text="乙项目金额￥1,000,000，截止2026-09-01。",
    )
    payload = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "project-a",
                    "text": "跟进甲项目。",
                    "source_ids": [first.source_id],
                },
                {
                    "matter_key": "project-b",
                    "text": "跟进乙项目。",
                    "source_ids": [second.source_id],
                },
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有重复提示。"),
        "source_dispositions": [
            {"source_id": first.source_id, "disposition": "cited", "reason": ""},
            {"source_id": second.source_id, "disposition": "cited", "reason": ""},
        ],
    }

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(payload),
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(first, second),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    error = caught.value.repair_detail
    assert first.source_id in error and "甲项目金额100万" in error
    assert second.source_id in error and "乙项目金额￥1,000,000" in error
    assert "甲项目" not in str(caught.value)


@pytest.mark.asyncio
async def test_all_excluded_critical_sources_are_reported_in_one_repair() -> None:
    first = _source(
        "daily:excluded:first",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="甲项目金额100万，9/1前回复。",
    )
    second = _source(
        "daily:excluded:second",
        kind="daily_report",
        on_date=date(2026, 8, 19),
        section="today_work",
        text="乙项目金额￥1,000,000，截止2026-09-01。",
    )
    payload = {
        "intro": "本周简报。",
        "completed": _empty_section("没有需要展示的工作。"),
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "source_dispositions": [
            {
                "source_id": first.source_id,
                "disposition": "safely_excluded",
                "reason": "模型误判无需展示。",
            },
            {
                "source_id": second.source_id,
                "disposition": "safely_excluded",
                "reason": "模型误判无需展示。",
            },
        ],
    }

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(payload),
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(first, second),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    error = caught.value.repair_detail
    assert first.source_id in error and "甲项目金额100万" in error
    assert second.source_id in error and "乙项目金额￥1,000,000" in error
    assert "甲项目" not in str(caught.value)


@pytest.mark.asyncio
async def test_maximum_excluded_critical_sources_fit_within_repair_limit() -> None:
    sources = tuple(
        _source(
            f"daily:{index:03d}:" + "a" * 110,
            kind="daily_report",
            on_date=date(2026, 8, 17 + index % 5),
            section="today_work",
            text=f"事项{index}金额100万" + "补充说明" * 30 + "。",
        )
        for index in range(120)
    )
    payload = {
        "intro": "本周简报。",
        "completed": _empty_section("没有需要展示的工作。"),
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "source_dispositions": [
            {
                "source_id": source.source_id,
                "disposition": "safely_excluded",
                "reason": "模型误判无需展示。",
            }
            for source in sources
        ],
    }

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(payload),
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(*sources),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    error = caught.value.repair_detail
    assert len(error) < 12000
    assert "all_excluded_critical_sources_must_be_reprocessed" in error
    assert '"excluded_source_count":120' in error
    assert "trusted_snapshot" in error


@pytest.mark.asyncio
async def test_excluded_critical_source_is_cited_on_second_model_attempt() -> None:
    sources = tuple(
        _source(
            f"daily:{index:03d}:" + "b" * 110,
            kind="daily_report",
            on_date=date(2026, 8, 17 + index % 5),
            section="today_work",
            text=f"事项{index}金额100万。",
        )
        for index in range(120)
    )
    first_invalid = {
        "intro": "本周简报。",
        "completed": _empty_section("没有需要展示的工作。"),
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "source_dispositions": [
            {
                "source_id": source.source_id,
                "disposition": "safely_excluded",
                "reason": "模型误判无需展示。",
            }
            for source in sources
        ],
    }
    source_groups = tuple(
        sources[start : start + 30] for start in range(0, len(sources), 30)
    )
    second_valid = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": f"project-{index}",
                    "text": f"第{index + 1}组事项金额100万。",
                    "source_ids": [source.source_id for source in group],
                }
                for index, group in enumerate(source_groups)
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有未闭环事项。"),
        "source_dispositions": [
            {
                "source_id": source.source_id,
                "disposition": "cited",
                "reason": "",
            }
            for source in sources
        ],
    }
    review_approved = {
        "approved": True,
        "issues": [],
    }
    llm = _SequenceLLM(
        first_invalid,
        second_valid,
        *(review_approved for _ in source_groups),
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(*sources),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 2
    assert len(outcome.content.source_dispositions) == 120
    assert all(
        disposition.disposition == "cited"
        for disposition in outcome.content.source_dispositions
    )
    second_prompt = json.loads(llm.calls[1]["user_prompt"])
    issue_reason = second_prompt["repair_context"]["issues"][0]["reason"]
    assert "逐项检查 trusted_snapshot" in issue_reason
    assert "不得标为 safely_excluded" in issue_reason
    assert "上一版 source_dispositions" not in issue_reason


def test_reviewer_prompt_does_not_duplicate_source_trace() -> None:
    source = Path("app/agent2/personal_weekly_brief.py").read_text(encoding="utf-8")
    review_body = source.split("class Agent2PersonalWeeklyBriefReviewer", 1)[1].split(
        "class PersonalWeeklyBriefReviewRejected", 1
    )[0]

    assert "model_snapshot = snapshot.model_payload()" in source
    assert "model_draft = _model_content_payload(content, snapshot=snapshot)" in source
    assert '"trace": content.trace_payload()' not in review_body
    assert "禁止把“通过、符合规则、未发现问题”的检查过程写入 issues" in source
    assert "server_checks 是服务器在调用你之前已经完成的确定性核对" in source


@pytest.mark.asyncio
async def test_model_pipeline_stops_after_one_semantic_repair() -> None:
    source = _source(
        "daily:repair:stop",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="跟进甲事项，需在8月20日前回复。",
    )
    draft = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "matter-a",
                    "text": "跟进甲事项，需在8月20日前回复。",
                    "source_ids": [source.source_id],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有重复提示。"),
    }
    rejection = {
        "approved": False,
        "reviewed_matter_keys": ["matter-a"],
        "issues": [{"matter_key": "matter-a", "reason": "仍然遗漏日期条件"}],
    }
    llm = _SequenceLLM(draft, rejection, draft, rejection)
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    with pytest.raises(PersonalWeeklyBriefReviewRejected):
        await pipeline.generate_and_review(
            snapshot=_snapshot(source),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    assert len(llm.calls) == 4


@pytest.mark.asyncio
async def test_model_pipeline_can_use_two_bounded_repairs_for_weekly_batch() -> None:
    valid = {
        "intro": "本周没有已保存的数据。",
        "completed": _empty_section("没有日报今日工作记录。"),
        "plan_progress": _empty_section("没有周计划记录。"),
        "possible_open_loops": _empty_section("没有数据时不推测。"),
    }
    llm = _SequenceLLM(
        "not-json-1",
        "not-json-2",
        valid,
        {"approved": True, "reviewed_matter_keys": [], "issues": []},
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
        max_semantic_attempts=3,
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 3
    assert outcome.model_calls == 4


@pytest.mark.asyncio
async def test_three_review_votes_prevent_one_false_rejection() -> None:
    valid = {
        "intro": "本周没有已保存的数据。",
        "completed": _empty_section("没有日报今日工作记录。"),
        "plan_progress": _empty_section("没有周计划记录。"),
        "possible_open_loops": _empty_section("没有数据时不推测。"),
    }
    rejected = {
        "approved": False,
        "reviewed_matter_keys": [],
        "issues": [{"matter_key": "__brief__", "reason": "误判为存在遗漏。"}],
    }
    approved = {"approved": True, "reviewed_matter_keys": [], "issues": []}
    llm = _SequenceLLM(valid, rejected, approved, approved)
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
        review_votes=3,
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.model_calls == 4
    assert outcome.review["review_consensus"] == {
        "required": 2,
        "approved": 2,
        "rejected": 1,
        "invalid": 0,
        "votes_cast": 3,
        "dispute_adjudicated": True,
    }
    adjudication_prompt = json.loads(llm.calls[3]["user_prompt"])
    assert adjudication_prompt["disputed_issues"] == [
        {"matter_key": "__brief__", "reason": "误判为存在遗漏。"}
    ]


@pytest.mark.asyncio
async def test_three_review_votes_repair_after_majority_rejection() -> None:
    valid = {
        "intro": "本周没有已保存的数据。",
        "completed": _empty_section("没有日报今日工作记录。"),
        "plan_progress": _empty_section("没有周计划记录。"),
        "possible_open_loops": _empty_section("没有数据时不推测。"),
    }
    rejected = {
        "approved": False,
        "reviewed_matter_keys": [],
        "issues": [{"matter_key": "__brief__", "reason": "需要重做。"}],
    }
    approved = {"approved": True, "reviewed_matter_keys": [], "issues": []}
    llm = _SequenceLLM(
        valid,
        rejected,
        rejected,
        valid,
        approved,
        approved,
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
        review_votes=3,
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 2
    assert outcome.model_calls == 6


@pytest.mark.asyncio
async def test_focused_critical_review_repairs_lost_condition_result() -> None:
    source = _source(
        "daily:condition-review",
        kind="daily_report",
        on_date=date(2026, 8, 20),
        section="today_work",
        text="对方表示只有付款条件确认后才答复。",
    )
    incomplete = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "payment-condition",
                    "text": "对方表示需先确认付款条件。",
                    "source_ids": [source.source_id],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有重复提示。"),
    }
    repaired = {
        **incomplete,
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "payment-condition",
                    "text": "对方表示只有付款条件确认后才答复。",
                    "source_ids": [source.source_id],
                }
            ],
        },
    }
    approved = {
        "approved": True,
        "reviewed_matter_keys": ["payment-condition"],
        "issues": [],
    }
    critical_rejected = {
        "approved": False,
        "reviewed_matter_keys": ["payment-condition"],
        "issues": [
            {
                "matter_key": "payment-condition",
                "reason": "只保留条件前提，遗漏确认后才答复的结果。",
            }
        ],
    }
    llm = _SequenceLLM(
        incomplete,
        approved,
        approved,
        critical_rejected,
        repaired,
        approved,
        approved,
        approved,
    )
    general_reviewer = Agent2PersonalWeeklyBriefReviewer(
        llm,
        model="agent2-model",
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=general_reviewer,
        critical_reviewer=Agent2PersonalWeeklyBriefReviewer(
            llm,
            model="agent2-model",
            review_mode="critical_facts",
        ),
        review_votes=3,
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 2
    assert outcome.model_calls == 8
    assert "确认后才答复" in outcome.content.message_text


@pytest.mark.asyncio
async def test_model_pipeline_retries_one_invalid_generation_then_reviews() -> None:
    valid = {
        "intro": "本周没有已保存的数据。",
        "completed": _empty_section("没有日报今日工作记录。"),
        "plan_progress": _empty_section("没有周计划记录。"),
        "possible_open_loops": _empty_section("没有数据时不推测。"),
    }
    llm = _SequenceLLM(
        "not-a-json-object",
        valid,
        {
            "approved": True,
            "reviewed_matter_keys": [],
            "issues": [],
        },
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.semantic_attempts == 2
    assert outcome.model_calls == 3
    assert len(outcome.generation_seconds) == 2
    assert len(outcome.review_seconds) == 1


@pytest.mark.asyncio
async def test_model_pipeline_stops_after_two_invalid_generations() -> None:
    llm = _SequenceLLM("not-json-one", "not-json-two")
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid):
        await pipeline.generate_and_review(
            snapshot=_snapshot(),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_model_pipeline_retries_after_one_invalid_review_output() -> None:
    valid = {
        "intro": "本周没有已保存的数据。",
        "completed": _empty_section("没有日报今日工作记录。"),
        "plan_progress": _empty_section("没有周计划记录。"),
        "possible_open_loops": _empty_section("没有数据时不推测。"),
    }
    llm = _SequenceLLM(
        valid,
        "review-not-json",
        {
            "approved": True,
            "issues": [],
        },
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.model_calls == 2
    assert outcome.semantic_attempts == 1
    assert len(outcome.review_seconds) == 1
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_model_pipeline_stops_after_second_invalid_review_output() -> None:
    valid = {
        "intro": "本周没有已保存的数据。",
        "completed": _empty_section("没有日报今日工作记录。"),
        "plan_progress": _empty_section("没有周计划记录。"),
        "possible_open_loops": _empty_section("没有数据时不推测。"),
    }
    llm = _SequenceLLM(
        valid,
        "bad-review-one",
        "bad-review-two",
        valid,
        "bad-review-three",
        "bad-review-four",
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    with pytest.raises(ValueError, match="review returned invalid JSON"):
        await pipeline.generate_and_review(
            snapshot=_snapshot(),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )

    assert len(llm.calls) == 6


@pytest.mark.asyncio
async def test_model_pipeline_repairs_an_overlong_rendered_message_once() -> None:
    sources = tuple(
        _source(
            f"daily:long-render:{index}",
            kind="daily_report",
            on_date=date(2026, 8, 17 + index),
            section="today_work",
            text=f"脱敏事项{index}",
        )
        for index in range(4)
    )
    too_long = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": f"long-{index}",
                    "text": "甲" * 1000,
                    "source_ids": [source.source_id],
                }
                for index, source in enumerate(sources)
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有重复提示。"),
    }
    repaired = {
        **too_long,
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": f"long-{index}",
                    "text": f"脱敏事项{index}。",
                    "source_ids": [source.source_id],
                }
                for index, source in enumerate(sources)
            ],
        },
    }
    llm = _SequenceLLM(
        too_long,
        repaired,
        {
            "approved": True,
            "reviewed_matter_keys": [f"long-{index}" for index in range(4)],
            "issues": [],
        },
    )
    pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=Agent2PersonalWeeklyBriefGenerator(llm, model="agent2-model"),
        reviewer=Agent2PersonalWeeklyBriefReviewer(llm, model="agent2-model"),
    )

    outcome = await pipeline.generate_and_review(
        snapshot=_snapshot(*sources),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert outcome.model_calls == 3
    assert len(outcome.content.message_text) < 3600


def test_window_is_saturday_generation_with_monday_to_friday_report_dates() -> None:
    window = derive_personal_weekly_brief_window(
        SATURDAY_RUN,
        timezone_name="Asia/Shanghai",
    )

    assert window.week_start == date(2026, 8, 17)
    assert window.week_end == date(2026, 8, 21)
    assert window.report_dates == tuple(
        date(2026, 8, day) for day in range(17, 22)
    )
    assert window.snapshot_at == SATURDAY_RUN


@pytest.mark.asyncio
async def test_cross_day_items_are_merged_once_without_losing_key_facts() -> None:
    monday = _source(
        "daily:monday:today:1",
        kind="daily_report",
        on_date=date(2026, 8, 17),
        section="today_work",
        text="跟进甲公司合同争议，涉案金额120万元，等待对方8月20日前回复。",
    )
    thursday = _source(
        "daily:thursday:today:1",
        kind="daily_report",
        on_date=date(2026, 8, 20),
        section="today_work",
        text="与甲公司沟通合同争议，对方表示需在付款条件确认后再答复。",
    )
    llm = _FakeLLM(
        {
            "intro": "这是本周简报。",
            "completed": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "matter-contract-a",
                        "text": (
                            "跟进并沟通甲公司合同争议，涉案金额120万元；"
                            "对方原定8月20日前回复，后表示需在付款条件确认后再答复。"
                        ),
                        "source_ids": [monday.source_id, thursday.source_id],
                    }
                ],
            },
            "plan_progress": _empty_section("本周没有周计划记录。"),
            "possible_open_loops": _empty_section("暂未发现需要提示的事项。"),
        }
    )
    generator = Agent2PersonalWeeklyBriefGenerator(
        llm,
        model="agent2-model",
        thinking_enabled=True,
    )

    result = await generator.generate(
        snapshot=_snapshot(monday, thursday),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert len(result.completed.items) == 1
    assert "120万元" in result.completed.items[0].text
    assert "8月20日" in result.completed.items[0].text
    assert "付款条件确认后" in result.completed.items[0].text
    assert result.completed.items[0].source_ids == (
        monday.source_id,
        thursday.source_id,
    )
    assert result.trace_payload()["completed"][0]["sources"][0][
        "original_text"
    ] == monday.original_text


@pytest.mark.asyncio
async def test_five_plan_states_require_traceable_plan_and_progress_evidence() -> None:
    plan_texts = (
        "完成甲项目合同定稿。",
        "持续推进乙案件证据整理。",
        "周三参加丙案件开庭。",
        "与丁公司沟通付款节点。",
        "复核戊项目补充协议。",
    )
    update_texts = (
        "甲项目合同定稿已完成并发给经办人。",
        "乙案件证据整理持续推进，目前仍在补充送达材料。",
        "丙案件原定周三开庭，因法院改期调整到周五。",
        "已与丁公司沟通付款节点，后续安排下周一再确认发票条件。",
    )
    progress_texts = (
        "甲项目合同定稿已完成并发给经办人。",
        "乙案件证据整理持续推进，目前仍在补充送达材料。",
        "丙案件开庭由周三调整到周五。",
        "丁公司付款节点已沟通，后续安排下周一再确认发票条件。",
        "戊项目补充协议。",
    )
    plans = [
        _source(
            f"plan:{index}",
            kind="weekly_plan",
            on_date=date(2026, 8, 17 + index),
            section="plan_item",
            text=plan_texts[index],
            record_id="plan-1",
        )
        for index in range(5)
    ]
    updates = [
        _source(
            f"daily:update:{index}",
            kind="daily_report",
            on_date=date(2026, 8, 18 + index),
            section="today_work",
            text=update_texts[index],
            record_id=f"report-{index}",
        )
        for index in range(4)
    ]
    statuses = (
        "已完成",
        "持续推进",
        "安排调整",
        "后续安排",
        "暂时没有找到后续记录",
    )
    items = []
    for index, status in enumerate(statuses):
        source_ids = [plans[index].source_id]
        if index < 4:
            source_ids.append(updates[index].source_id)
        items.append(
            {
                "matter_key": f"matter-{index}",
                "text": progress_texts[index],
                "status": status,
                "source_ids": source_ids,
            }
        )
    llm = _FakeLLM(
        {
            "intro": "这是本周计划进展。",
            "completed": _empty_section("本周日报没有今日工作记录。"),
            "plan_progress": {"empty_note": "", "items": items},
            "possible_open_loops": _empty_section("没有重复提示。"),
        }
    )

    result = await Agent2PersonalWeeklyBriefGenerator(
        llm,
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(*plans, *updates),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert tuple(item.status for item in result.plan_progress.items) == statuses
    assert result.plan_progress.items[-1].source_ids == (plans[-1].source_id,)
    assert "仅表示现有记录中没有找到明确对应内容" in result.message_text
    review = await Agent2PersonalWeeklyBriefReviewer(
        _FakeLLM(
            {
                "approved": True,
                "reviewed_matter_keys": [f"matter-{index}" for index in range(5)],
                "issues": [],
            }
        ),
        model="agent2-model",
    ).review(snapshot=_snapshot(*plans, *updates), content=result)
    assert review["approved"] is True


@pytest.mark.asyncio
async def test_rendered_brief_is_clean_and_does_not_repeat_system_language() -> None:
    plan = _source(
        "plan:clean-display",
        kind="weekly_plan",
        on_date=date(2026, 8, 17),
        section="plan_item",
        text="合同审核。",
    )
    result = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "围绕合同、案件和系统优化开展了多项工作。",
                "completed": _empty_section("本周没有完成事项。"),
                "plan_progress": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "contract-review",
                            "text": "合同审核",
                            "status": "暂时没有找到后续记录",
                            "source_ids": [plan.source_id],
                        }
                    ],
                },
                "possible_open_loops": _empty_section("没有其他需要留意的事项。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(plan),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    message = result.message_text
    assert message.startswith("这是你本周的工作简报，方便回顾进展和安排后续。")
    assert "1. 暂无后续记录｜合同审核" in message
    assert "【" not in message
    assert "数据范围：" not in message
    assert "2026-08-17" not in message
    assert "以上根据8月17日至8月21日已保存的周计划整理。" in message
    assert "仅表示现有记录中没有找到明确对应内容" in message
    assert "不等于未完成" not in message


@pytest.mark.asyncio
async def test_no_followup_display_uses_trusted_plan_text_once() -> None:
    plan = _source(
        "plan:no-followup-copy",
        kind="weekly_plan",
        on_date=date(2026, 8, 17),
        section="plan_item",
        text="日常用印审核。",
    )
    result = await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(
                {
                    "intro": "本周工作简报",
                    "completed": _empty_section("没有完成事项。"),
                    "plan_progress": {
                        "empty_note": "",
                        "items": [
                            {
                                "matter_key": "seal-review",
                                "text": "日常用印审核：日报中未找到对应记录。",
                                "status": "暂时没有找到后续记录",
                                "source_ids": [plan.source_id],
                            }
                        ],
                    },
                    "possible_open_loops": _empty_section("没有其他事项。"),
                }
            ),
            model="agent2-model",
        ).generate(
        snapshot=_snapshot(plan),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert "1. 暂无后续记录｜日常用印审核" in result.message_text
    assert "日报中未找到对应记录" not in result.message_text


def test_critical_reviewer_requires_same_specific_plan_object() -> None:
    source = Path("app/agent2/personal_weekly_brief.py").read_text(encoding="utf-8")

    assert "只有“合同”或“案件材料”等泛词相同不算同一事项" in source
    assert "“合同审核”和“合同评审技能网页化”不是同一具体工作" in source


@pytest.mark.asyncio
async def test_open_loop_display_does_not_repeat_attention_prompt() -> None:
    source = _source(
        "daily:open-loop-display",
        kind="daily_report",
        on_date=date(2026, 8, 20),
        section="tomorrow_plan",
        text="计划通报新增被告案件。",
    )
    result = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "本周工作简报",
                "completed": _empty_section("没有完成事项。"),
                "plan_progress": _empty_section("没有周计划。"),
                "possible_open_loops": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "defendant-case-notice",
                            "text": "计划通报新增被告案件，后续需留意是否完成。",
                            "source_ids": [source.source_id],
                        }
                    ],
                },
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert "三、可能未闭环事项" in result.message_text
    assert "计划通报新增被告案件" in result.message_text
    assert "后续需留意" not in result.message_text


@pytest.mark.asyncio
async def test_strong_plan_status_without_daily_evidence_is_rejected() -> None:
    plan = _source(
        "plan:only",
        kind="weekly_plan",
        on_date=date(2026, 8, 17),
        section="plan_item",
        text="准备某案件材料",
    )
    llm = _FakeLLM(
        {
            "intro": "本周简报。",
            "completed": _empty_section("无。"),
            "plan_progress": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "case-a",
                        "text": "某案件材料已经完成。",
                        "status": "已完成",
                        "source_ids": [plan.source_id],
                    }
                ],
            },
            "possible_open_loops": _empty_section("无。"),
        }
    )

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(plan),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "plan status requires later daily evidence" in caught.value.repair_detail


@pytest.mark.asyncio
async def test_completed_item_must_be_anchored_in_today_work() -> None:
    problem = _source(
        "daily:problem:1",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="problems",
        text="付款材料尚未齐全。",
    )
    llm = _FakeLLM(
        {
            "intro": "本周简报。",
            "completed": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "payment-material",
                        "text": "付款材料尚未齐全。",
                        "source_ids": [problem.source_id],
                    }
                ],
            },
            "plan_progress": _empty_section("没有周计划。"),
            "possible_open_loops": _empty_section("暂不推测。"),
        }
    )

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(problem),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "completed item requires today_work evidence" in caught.value.repair_detail


@pytest.mark.asyncio
async def test_every_weekly_plan_source_must_appear_once_in_plan_progress() -> None:
    first = _source(
        "plan:first",
        kind="weekly_plan",
        on_date=date(2026, 8, 17),
        section="plan_item",
        text="复核甲项目合同。",
    )
    omitted = _source(
        "plan:omitted",
        kind="weekly_plan",
        on_date=date(2026, 8, 18),
        section="plan_item",
        text="整理乙案件证据。",
    )
    llm = _FakeLLM(
        {
            "intro": "本周简报。",
            "completed": _empty_section("没有日报今日工作记录。"),
            "plan_progress": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "matter-first",
                        "text": "复核甲项目合同。",
                        "status": "暂时没有找到后续记录",
                        "source_ids": [first.source_id],
                    }
                ],
            },
            "possible_open_loops": _empty_section("没有重复提示。"),
        }
    )

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(first, omitted),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "must cover every weekly plan source once" in caught.value.repair_detail


@pytest.mark.asyncio
async def test_frozen_source_universe_cannot_be_silently_omitted() -> None:
    first = _source(
        "daily:universe:first",
        kind="daily_report",
        on_date=date(2026, 8, 17),
        section="today_work",
        text="完成甲事项。",
    )
    omitted = _source(
        "daily:universe:omitted",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="problems",
        text="乙事项仍待补充授权文件。",
    )
    payload = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "matter-first",
                    "text": "完成甲事项。",
                    "source_ids": [first.source_id],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有需要提示的事项。"),
    }

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            _FakeLLM(payload),
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(first, omitted),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "frozen source universe is incomplete" in caught.value.repair_detail


@pytest.mark.asyncio
async def test_uncited_source_requires_one_explicit_safe_exclusion_reason() -> None:
    first = _source(
        "daily:disposition:first",
        kind="daily_report",
        on_date=date(2026, 8, 17),
        section="today_work",
        text="完成甲事项。",
    )
    duplicate = _source(
        "daily:disposition:duplicate",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="甲事项与前一日记录完全重复，没有新增事实。",
    )
    payload = {
        "intro": "本周简报。",
        "completed": {
            "empty_note": "",
            "items": [
                {
                    "matter_key": "matter-first",
                    "text": "完成甲事项。",
                    "source_ids": [first.source_id],
                }
            ],
        },
        "plan_progress": _empty_section("没有周计划。"),
        "possible_open_loops": _empty_section("没有需要提示的事项。"),
        "source_dispositions": [
            {
                "source_id": first.source_id,
                "disposition": "cited",
                "reason": "",
            },
            {
                "source_id": duplicate.source_id,
                "disposition": "safely_excluded",
                "reason": "与已引用的甲事项完全重复且没有新增事实。",
            },
        ],
    }

    result = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(payload),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(first, duplicate),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert result.source_dispositions[1].disposition == "safely_excluded"
    assert result.trace_payload()["source_dispositions"][1]["source_id"] == (
        duplicate.source_id
    )


@pytest.mark.asyncio
async def test_more_than_twelve_distinct_plan_items_can_be_reported_one_by_one() -> None:
    plans = tuple(
        _source(
            f"plan:item:{index}",
            kind="weekly_plan",
            on_date=date(2026, 8, 17 + index % 5),
            section="plan_item",
            text=f"脱敏周计划事项{index}。",
            record_id="plan-many",
        )
        for index in range(13)
    )
    llm = _FakeLLM(
        {
            "intro": "本周简报。",
            "completed": _empty_section("没有日报今日工作记录。"),
            "plan_progress": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": f"matter-{index}",
                        "text": f"脱敏周计划事项{index}。",
                        "status": "暂时没有找到后续记录",
                        "source_ids": [plan.source_id],
                    }
                    for index, plan in enumerate(plans)
                ],
            },
            "possible_open_loops": _empty_section("没有重复提示。"),
        }
    )

    result = await Agent2PersonalWeeklyBriefGenerator(
        llm,
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(*plans),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert len(result.plan_progress.items) == 13
    assert {
        source_id
        for item in result.plan_progress.items
        for source_id in item.source_ids
    } == {plan.source_id for plan in plans}


@pytest.mark.asyncio
async def test_plan_progress_and_open_loops_cannot_repeat_the_same_matter() -> None:
    plan = _source(
        "plan:a",
        kind="weekly_plan",
        on_date=date(2026, 8, 17),
        section="plan_item",
        text="推进脱敏事项",
    )
    llm = _FakeLLM(
        {
            "intro": "本周简报。",
            "completed": _empty_section("无。"),
            "plan_progress": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "same-matter",
                        "text": "推进脱敏事项。",
                        "status": "暂时没有找到后续记录",
                        "source_ids": [plan.source_id],
                    }
                ],
            },
            "possible_open_loops": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "same-matter",
                        "text": "脱敏事项可能未闭环。",
                        "source_ids": [plan.source_id],
                    }
                ],
            },
        }
    )

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(plan),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "duplicate matter across sections" in caught.value.repair_detail


@pytest.mark.asyncio
async def test_plan_progress_and_open_loops_cannot_reuse_the_same_daily_source() -> None:
    plan = _source(
        "plan:invoice",
        kind="weekly_plan",
        on_date=date(2026, 8, 20),
        section="plan_item",
        text="确认丁公司发票条件。",
    )
    later_plan = _source(
        "daily:invoice:later",
        kind="daily_report",
        on_date=date(2026, 8, 21),
        section="tomorrow_plan",
        text="后续安排下周一确认丁公司发票条件。",
    )
    llm = _FakeLLM(
        {
            "intro": "本周简报。",
            "completed": _empty_section("没有日报今日工作记录。"),
            "plan_progress": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "invoice-plan",
                        "text": "丁公司发票条件后续安排下周一确认。",
                        "status": "后续安排",
                        "source_ids": [plan.source_id, later_plan.source_id],
                    }
                ],
            },
            "possible_open_loops": {
                "empty_note": "",
                "items": [
                    {
                        "matter_key": "invoice-open-loop-renamed",
                        "text": "丁公司发票条件仍待确认。",
                        "source_ids": [later_plan.source_id],
                    }
                ],
            },
        }
    )

    with pytest.raises(PersonalWeeklyBriefModelOutputInvalid) as caught:
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(plan, later_plan),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )
    assert "source cannot repeat in open loops" in caught.value.repair_detail


@pytest.mark.asyncio
async def test_no_data_is_expressed_without_inventing_sources() -> None:
    llm = _FakeLLM(
        {
            "intro": "本周暂时没有找到已保存的日报或周计划。",
            "completed": _empty_section("暂时没有找到本周完成事项记录。"),
            "plan_progress": _empty_section("暂时没有找到本周周计划。"),
            "possible_open_loops": _empty_section("没有数据时不推测未闭环事项。"),
        }
    )

    result = await Agent2PersonalWeeklyBriefGenerator(
        llm,
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )

    assert result.completed.items == ()
    assert result.plan_progress.items == ()
    assert result.possible_open_loops.items == ()
    assert "没有数据时不推测" in result.message_text
    trace = result.trace_payload()
    assert trace["snapshot_fingerprint"] == _snapshot().fingerprint
    assert trace["system_explanations"]["intro"] == {
        "classification": "system_explanation",
        "business_conclusion": False,
        "basis": {
            "snapshot_fingerprint": _snapshot().fingerprint,
            "source_count": 0,
            "daily_report_dates": [],
            "weekly_plan_found": False,
        },
    }
    assert set(trace["system_explanations"]["empty_notes"]) == {
        "completed",
        "plan_progress",
        "possible_open_loops",
    }


@pytest.mark.asyncio
async def test_independent_model_review_rejects_changed_fact_or_missing_source() -> None:
    source = _source(
        "daily:fact:1",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="沟通甲方，金额120万元，尚未完成。",
    )
    generated = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "这是本周简报。",
                "completed": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "matter-a",
                            "text": "沟通甲方，金额120万元，尚未完成。",
                            "source_ids": [source.source_id],
                        }
                    ],
                },
                "plan_progress": _empty_section("没有周计划。"),
                "possible_open_loops": _empty_section("暂不推测。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        _FakeLLM(
            {
                "approved": False,
                "reviewed_matter_keys": ["matter-a"],
                "issues": [
                    {
                        "matter_key": "matter-a",
                        "reason": "金额或完成状态与来源不一致",
                    }
                ],
            }
        ),
        model="agent2-model",
    )

    with pytest.raises(ValueError, match="independent model review rejected"):
        await reviewer.review(snapshot=_snapshot(source), content=generated)


@pytest.mark.asyncio
async def test_independent_model_review_must_cover_every_generated_matter() -> None:
    source = _source(
        "daily:fact:1",
        kind="daily_report",
        on_date=date(2026, 8, 18),
        section="today_work",
        text="沟通甲方。",
    )
    generated = await Agent2PersonalWeeklyBriefGenerator(
        _FakeLLM(
            {
                "intro": "这是本周简报。",
                "completed": {
                    "empty_note": "",
                    "items": [
                        {
                            "matter_key": "matter-a",
                            "text": "沟通甲方。",
                            "source_ids": [source.source_id],
                        }
                    ],
                },
                "plan_progress": _empty_section("没有周计划。"),
                "possible_open_loops": _empty_section("暂不推测。"),
            }
        ),
        model="agent2-model",
    ).generate(
        snapshot=_snapshot(source),
        recipient_name="测试用户",
        personal_memory={"entries": []},
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        _FakeLLM(
            {
                "approved": True,
                "reviewed_matter_keys": [],
                "issues": [],
            }
        ),
        model="agent2-model",
    )

    with pytest.raises(ValueError, match="review returned invalid JSON"):
        await reviewer.review(snapshot=_snapshot(source), content=generated)
    assert len(reviewer._llm_client.calls) == 2
