"""Real-Flash weekly-plan cases derived from recent Daily input shapes.

The wording is fictionalized: project and person names are synthetic, while
list size, recurrence, date pressure, conditions, cross-turn confirmations,
and correction structure mirror recent production Daily reports and failures.
The imported matrix uses an in-memory no-op runtime, so no database or message
provider is touched.
"""

from __future__ import annotations

import sys

import run_agent2_tri_domain_full_adapter_matrix_live as matrix


def _binding(
    dates: tuple[str, ...],
    operations: tuple[str, ...] = ("add",),
    *,
    plan_id=matrix._FRIDAY_PLAN_ID,
    version: int = matrix._FRIDAY_PLAN_VERSION,
):
    return matrix._binding(plan_id, version, dates, operations)


DERIVED_CASES = (
    matrix.MatrixCase(
        "derived_explicit_calendar_dates",
        "daily_derived_dates",
        (
            "补充下周工作计划：2026年8月17日周一整理甲项目证据；"
            "8月18日周二跟进乙项目付款；8月22日周六暂无安排。"
        ),
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(
                ("2026-08-17", "2026-08-18", "2026-08-22"),
                ("add", "set_day_empty"),
            ),
        ),
        expected_plan_additions=(
            ("2026-08-17", "整理甲项目证据"),
            ("2026-08-18", "跟进乙项目付款"),
        ),
        note="Mirrors dated Daily templates that previously failed before the model.",
    ),
    matrix.MatrixCase(
        "derived_monday_dual_current_week_exact_date",
        "daily_derived_dates",
        "补一下本周工作计划：8月18日周二整理甲项目卷宗。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                ("2026-08-18",),
                plan_id=matrix._MONDAY_CURRENT_PLAN_ID,
                version=matrix._MONDAY_CURRENT_VERSION,
            ),
        ),
        expected_plan_additions=(("2026-08-18", "整理甲项目卷宗"),),
        note="Mirrors '补一下昨天的' while two exact week targets coexist.",
    ),
    matrix.MatrixCase(
        "derived_monday_dual_next_week_exact_date",
        "daily_derived_dates",
        "填写下周工作计划：8月25日周二整理乙项目卷宗。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                ("2026-08-25",),
                plan_id=matrix._MONDAY_NEXT_PLAN_ID,
                version=matrix._MONDAY_NEXT_VERSION,
            ),
        ),
        expected_plan_additions=(("2026-08-25", "整理乙项目卷宗"),),
    ),
    matrix.MatrixCase(
        "derived_monday_dual_tomorrow",
        "daily_derived_dates",
        "明天安排：准备甲案件开庭材料。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                ("2026-08-18",),
                plan_id=matrix._MONDAY_CURRENT_PLAN_ID,
                version=matrix._MONDAY_CURRENT_VERSION,
            ),
        ),
        expected_plan_additions=(("2026-08-18", "准备甲案件开庭材料"),),
        recent_messages=(
            (
                "assistant",
                "现在正在填写本周工作计划（8月17日至22日），请继续说具体日期和事项。",
            ),
        ),
        note="Relative date must select the current-week plan, not natural next week.",
    ),
    matrix.MatrixCase(
        "derived_bulk_eight_monday_items",
        "daily_derived_bulk",
        (
            "下周一安排：复核甲项目合同；整理乙项目证据；跟进丙项目付款；"
            "准备丁案件开庭；核对戊项目台账；沟通己项目和解；"
            "办理庚项目用印；归档辛项目资料。"
        ),
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(_binding(("2026-08-17",)),),
        expected_plan_additions=tuple(
            ("2026-08-17", content)
            for content in (
                "复核甲项目合同",
                "整理乙项目证据",
                "跟进丙项目付款",
                "准备丁案件开庭",
                "核对戊项目台账",
                "沟通己项目和解",
                "办理庚项目用印",
                "归档辛项目资料",
            )
        ),
        note="Derived from a 52-item Daily report with duplicated matter groups.",
    ),
    matrix.MatrixCase(
        "derived_two_recurring_workday_matters",
        "daily_derived_recurrence",
        "下周一到周五每天做档案催收和日常用印审核。",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(tuple(f"2026-08-{day:02d}" for day in range(17, 22))),
        ),
        expected_plan_terms_by_date=tuple(
            (
                f"2026-08-{day:02d}",
                ("档案催收", "日常用印审核"),
            )
            for day in range(17, 22)
        ),
        note="Derived from one matter repeated 17 times across recent Daily reports.",
    ),
    matrix.MatrixCase(
        "derived_recurring_plus_oneoff",
        "daily_derived_recurrence",
        "下周每天做日常用印审核，另外周四准备甲案件开庭材料。",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(tuple(f"2026-08-{day:02d}" for day in range(17, 23))),
        ),
        expected_plan_terms_by_date=tuple(
            (
                f"2026-08-{day:02d}",
                (
                    ("日常用印审核", "准备甲案件开庭材料")
                    if day == 20
                    else ("日常用印审核",)
                ),
            )
            for day in range(17, 23)
        ),
        note="Recurring routine work and one dated exception must coexist atomically.",
    ),
    matrix.MatrixCase(
        "derived_long_conditional_matter",
        "daily_derived_long",
        (
            "下周三处理星河项目：只有收到补充材料后才发正式函；"
            "若金额超过180万元，先内部汇报，不直接承诺付款。"
        ),
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(_binding(("2026-08-19",)),),
        expected_plan_terms_by_date=(
            (
                "2026-08-19",
                (
                    "处理星河项目",
                    "收到补充材料后",
                    "才发",
                    "正式函",
                    "金额超过180万元",
                    "先内部汇报",
                    "不直接承诺付款",
                ),
            ),
        ),
        note="Preserves amount, condition, negation, and dependent actions from long Daily risks.",
    ),
    matrix.MatrixCase(
        "derived_six_day_multi_project",
        "daily_derived_bulk",
        (
            "下周计划：周一核对甲项目债权；周二准备乙案件开庭；"
            "周三跟进丙项目付款并确认发票；周四审阅丁项目补充协议；"
            "周五向负责人汇报戊项目风险；周六整理本周未归档合同。"
        ),
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(tuple(f"2026-08-{day:02d}" for day in range(17, 23))),
        ),
        expected_plan_additions=(
            ("2026-08-17", "核对甲项目债权"),
            ("2026-08-18", "准备乙案件开庭"),
            ("2026-08-19", "跟进丙项目付款并确认发票"),
            ("2026-08-20", "审阅丁项目补充协议"),
            ("2026-08-21", "向负责人汇报戊项目风险"),
            ("2026-08-22", "整理本周未归档合同"),
        ),
    ),
    matrix.MatrixCase(
        "derived_short_submit_after_preview",
        "daily_derived_followup",
        "提交",
        frozenset({"submit_next_weekly_plan"}),
        context_variant="plan_ready",
        expected_plan_submit=(
            str(matrix._FRIDAY_PLAN_ID),
            matrix._FRIDAY_PLAN_VERSION,
        ),
        recent_messages=(
            (
                "assistant",
                "这是下周工作计划的完整预览，确认无误后可以提交。",
            ),
        ),
        note="Mirrors the most frequent recent failed Daily input.",
    ),
    matrix.MatrixCase(
        "derived_short_confirm_after_preview",
        "daily_derived_followup",
        "确认",
        frozenset({"submit_next_weekly_plan"}),
        context_variant="plan_ready",
        expected_plan_submit=(
            str(matrix._FRIDAY_PLAN_ID),
            matrix._FRIDAY_PLAN_VERSION,
        ),
        recent_messages=(
            (
                "assistant",
                "下周计划已填写完整，要现在确认提交吗？",
            ),
        ),
    ),
    matrix.MatrixCase(
        "derived_add_but_do_not_submit",
        "daily_derived_followup",
        "周三再增加一条：准备星河案证据清单，先别提交。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="plan_ready",
        expected_plan_bindings=(_binding(("2026-08-19",)),),
        expected_plan_additions=(("2026-08-19", "准备星河案证据清单"),),
        response_kind="no_submit",
    ),
    matrix.MatrixCase(
        "derived_edit_then_submit",
        "daily_derived_followup",
        "把周三那条改成准备星河案证据清单，然后提交下周计划。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="plan_ready",
        expected_plan_bindings=(
            _binding((), ("edit",)),
        ),
        response_kind="no_submit",
        note="Mirrors '按照这个提交，上面那个取消' correction chains.",
    ),
    matrix.MatrixCase(
        "derived_saturday_empty_only",
        "daily_derived_dates",
        "下周六没安排，先留空。",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(("2026-08-22",), ("set_day_empty",)),
        ),
    ),
)


matrix.CASES = matrix.CASES + DERIVED_CASES


if __name__ == "__main__":
    if "--self-check-only" not in sys.argv and "--case-id" not in sys.argv:
        for case in DERIVED_CASES:
            sys.argv.extend(("--case-id", case.case_id))
    raise SystemExit(matrix.main())
