from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent2.personal_weekly_brief import (
    Agent2PersonalWeeklyBriefGenerator,
    Agent2PersonalWeeklyBriefReviewer,
    PersonalWeeklyBriefSnapshot,
    SourceEvidence,
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
        return json.dumps(self.payload, ensure_ascii=False)


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
        "戊项目补充协议暂时没有找到后续记录。",
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
    assert "不等于未完成" in result.message_text
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

    with pytest.raises(ValueError, match="plan status requires later daily evidence"):
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(plan),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )


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

    with pytest.raises(ValueError, match="completed item requires today_work evidence"):
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(problem),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )


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
                        "text": "复核甲项目合同，暂时没有找到后续记录。",
                        "status": "暂时没有找到后续记录",
                        "source_ids": [first.source_id],
                    }
                ],
            },
            "possible_open_loops": _empty_section("没有重复提示。"),
        }
    )

    with pytest.raises(ValueError, match="must cover every weekly plan source once"):
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(first, omitted),
            recipient_name="测试用户",
            personal_memory={"entries": []},
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
                        "text": f"脱敏周计划事项{index}暂时没有找到后续记录。",
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

    with pytest.raises(ValueError, match="duplicate matter across sections"):
        await Agent2PersonalWeeklyBriefGenerator(
            llm,
            model="agent2-model",
        ).generate(
            snapshot=_snapshot(plan),
            recipient_name="测试用户",
            personal_memory={"entries": []},
        )


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
    assert result.trace_payload()["snapshot_fingerprint"] == _snapshot().fingerprint


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

    with pytest.raises(ValueError, match="did not cover every matter"):
        await reviewer.review(snapshot=_snapshot(source), content=generated)
