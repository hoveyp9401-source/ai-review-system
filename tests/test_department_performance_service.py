from app.services.department_performance_service import (
    REQUIRED_DEPARTMENT_MONTHLY_UNITS,
    ZHU_JIAJIA_METRICS,
    aggregate_department_monthly_reports,
    build_department_monthly_report,
    build_test_department_monthly_sources,
    department_monthly_coverage,
    parse_department_monthly_reports,
    parse_team_template_metrics_from_paragraphs,
    source_from_performance_submission,
    split_markdown_message,
    template_metrics_for_unit,
)


def _source(
    unit_name="法务二部",
    owner_name="庞浩",
    metrics=None,
):
    return {
        "unit_name": unit_name,
        "owner_name": owner_name,
        "period_label": "2026-06",
        "task_title": f"{unit_name}2026年6月团队绩效填报",
        "status": "completed",
        "confirmed_by_user": True,
        "source_type": "test",
        "metrics": metrics or [],
    }


def test_template_metrics_keep_real_department_counts():
    assert [item["name"] for item in template_metrics_for_unit("法务一部")] == [
        "诉讼案件收款（现金）",
        "诉讼利息收入（现金）",
        "终本案件恢复执行到位率",
        "未审定诉讼结算增加额",
        "优先权与时效管理",
        "恒大破产案件申报率",
        "恒大分配案件清偿率",
        "恒大衍生风险闭环率",
    ]
    assert len(template_metrics_for_unit("法务二部")) == 6
    assert len(template_metrics_for_unit("综合管理部")) == 7
    assert [metric["name"] for metric in template_metrics_for_unit("朱佳佳")] == [metric["name"] for metric in ZHU_JIAJIA_METRICS]


def test_template_parser_uses_metric_structure_not_plain_numbering():
    parsed = parse_team_template_metrics_from_paragraphs(
        [
            "法务一部 X月度绩效工作汇报",
            "部门负责人：杨弟桦",
            "1、诉讼案件收款（现金）",
            "（1）本月绩效完成情况",
            "月度目标：______万元，实际完成：______万元，完成率：______%；",
            "行动方案：",
            "1. ________________________________________",
            "2. ________________________________________",
            "2、诉讼利息收入（现金）",
            "（1）本月绩效完成情况",
            "月度目标：______万元，实际完成：______万元，完成率：______%；",
        ]
    )

    assert [item["name"] for item in parsed["法务一部"]] == ["诉讼案件收款（现金）", "诉讼利息收入（现金）"]


def test_test_department_monthly_sources_cover_required_units_and_real_template_counts():
    sources = build_test_department_monthly_sources("2026-06", seed=1)
    coverage = department_monthly_coverage(sources)

    assert coverage.ready is True
    assert coverage.completed_units == REQUIRED_DEPARTMENT_MONTHLY_UNITS
    counts = {source["unit_name"]: len(source["metrics"]) for source in sources}
    assert counts["法务一部"] == 8
    assert counts["法务二部"] == 6
    assert counts["综合管理部"] == 7
    assert counts["朱佳佳"] == 4


def test_source_from_submission_extracts_metric_item_fields():
    source = source_from_performance_submission(
        unit_name="法务二部",
        owner_name="庞浩",
        period_label="2026-06",
        task_title="法务二部2026年6月团队绩效填报",
        metrics=[
            {
                "metric_no": 1,
                "name": "索赔管理",
                "unit": "万元",
                "display_lines": [
                    "月度目标：100万元，实际完成：92万元，完成率：92%，同比上升/下降：-3%，环比上升/下降：5%；",
                    "年度目标：1200万元，累计实际完成：510万元，累计完成率：42.5%，同比上升/下降：-8%；",
                ],
            }
        ],
        responses=[
            {
                "metric_no": 1,
                "reason": "客户审批节点滞后",
                "next_target": "120万元",
                "actions": ["7月10日前列出超期清单", "每周三跟进审批节点"],
            }
        ],
        status="completed",
        confirmed_by_user=True,
    )

    report = parse_department_monthly_reports([source], period_label="2026-06")[0]
    metric = report.metrics[0]

    assert metric.department == "法务二部"
    assert metric.leader == "庞浩"
    assert metric.metric_name == "索赔管理"
    assert metric.metric_type == "amount"
    assert metric.unit == "万元"
    assert metric.month_target == 100
    assert metric.month_actual == 92
    assert metric.month_completion_rate == 92
    assert metric.year_target == 1200
    assert metric.year_actual_cumulative == 510
    assert metric.year_completion_rate == 42.5
    assert metric.yoy_change == -3
    assert metric.mom_change == 5
    assert metric.unfinished_reason == "客户审批节点滞后"
    assert metric.next_month_target == "120万元"
    assert metric.action_plan == ["7月10日前列出超期清单", "每周三跟进审批节点"]
    assert "月度目标：100万元" in metric.raw_text


def test_metric_validation_marks_missing_data_bad_amount_rate_and_vague_actions():
    reports = parse_department_monthly_reports(
        [
            _source(
                metrics=[
                    {
                        "metric_no": 1,
                        "metric_name": "索赔管理",
                        "unit": "万元",
                        "display_lines": [
                            "月度目标：100万元，实际完成：70万元，完成率：90%；",
                            "年度目标：1200万元，累计实际完成：300万元，累计完成率：25%；",
                        ],
                        "reason": "",
                        "next_target": "______万元",
                        "actions": ["持续跟进"],
                    }
                ]
            )
        ],
        period_label="2026-06",
    )

    flags = set(reports[0].metrics[0].validation_flags)
    assert "未填写字段" in flags
    assert "金额完成率不一致" in flags
    assert "完成率低于100但缺少未完成原因" in flags
    assert "行动方案不具体" in flags


def test_zero_target_with_actual_marks_completion_rate_as_none_not_missing():
    source = _source(
        unit_name="法务一部",
        owner_name="杨弟桦",
        metrics=[
            {
                "metric_no": 2,
                "metric_name": "诉讼利息收入（现金）",
                "unit": "万元",
                "display_lines": [
                    "月度目标：0万元，实际完成：2.75万元，完成率：999%；",
                    "年度目标：500万元，累计实际完成：2.8万元，累计完成率：0.56%；",
                ],
                "reason": "本月无目标，实际回款为额外到账",
                "next_target": "0万元",
                "actions": ["7月10日前核对额外到账明细"],
            }
        ],
    )
    reports = parse_department_monthly_reports([source], period_label="2026-06")
    metric = reports[0].metrics[0]

    assert metric.month_target == 0
    assert metric.month_actual == 2.75
    assert metric.month_completion_rate is None
    assert "关键数据缺失" not in metric.validation_flags

    rendered = build_department_monthly_report([source], period_label="2026-06", expected_units=["法务一部"])
    assert "月度：目标 0万元，实际 2.75万元，完成率 无。" in rendered


def test_amount_aggregation_and_rate_metric_not_summed_without_denominator():
    reports = parse_department_monthly_reports(
        [
            _source(
                unit_name="法务二部",
                metrics=[
                    {
                        "metric_no": 1,
                        "metric_name": "索赔管理",
                        "unit": "万元",
                        "display_lines": [
                            "月度目标：100万元，实际完成：90万元，完成率：90%；",
                            "年度目标：1200万元，累计实际完成：600万元，累计完成率：50%；",
                        ],
                        "reason": "客户审批慢",
                        "next_target": "130万元",
                        "actions": ["7月5日前完成客户审批清单"],
                    },
                    {
                        "metric_no": 6,
                        "metric_name": "被告存量/新增案件数量下降率",
                        "unit": "%",
                        "display_lines": [
                            "月度目标：10%，实际完成：7%，完成率：70%，同比上升/下降：-2百分点，环比上升/下降：1百分点；",
                            "年度目标：10%，累计实际完成：7%，累计完成率：70%，同比上升/下降：-2百分点；",
                        ],
                        "reason": "新增案件波动",
                        "next_target": "10%",
                        "actions": ["7月15日前按案件类型拆分压降清单"],
                    },
                ],
            ),
            _source(
                unit_name="法务三部",
                owner_name="刘波",
                metrics=[
                    {
                        "metric_no": 1,
                        "metric_name": "索赔管理",
                        "unit": "万元",
                        "display_lines": [
                            "月度目标：200万元，实际完成：180万元，完成率：90%；",
                            "年度目标：2400万元，累计实际完成：960万元，累计完成率：40%；",
                        ],
                        "reason": "资料回收慢",
                        "next_target": "210万元",
                        "actions": ["7月8日前补齐缺失资料"],
                    }
                ],
            ),
        ],
        period_label="2026-06",
    )
    summary = aggregate_department_monthly_reports(reports, period_label="2026-06")

    amount = next(item for item in summary.core_metrics if item.metric_name == "索赔管理")
    assert amount.metric_type == "amount"
    assert amount.month_target == 300
    assert amount.month_actual == 270
    assert amount.month_completion_rate == 90
    assert amount.year_target == 3600
    assert amount.year_actual_cumulative == 1560
    assert round(amount.year_completion_rate, 1) == 43.3
    assert "被告存量/新增案件数量下降率" in summary.non_aggregatable_rate_metrics


def test_red_yellow_green_status_uses_month_and_year_progress():
    reports = parse_department_monthly_reports(
        [
            _source(
                metrics=[
                    {
                        "metric_no": 1,
                        "metric_name": "绿色指标",
                        "unit": "万元",
                        "display_lines": ["月度目标：100万元，实际完成：110万元，完成率：110%；", "年度目标：1200万元，累计实际完成：600万元，累计完成率：50%；"],
                        "reason": "无",
                        "next_target": "120万元",
                        "actions": ["7月10日前锁定重点项目"],
                    },
                    {
                        "metric_no": 2,
                        "metric_name": "黄色指标",
                        "unit": "万元",
                        "display_lines": ["月度目标：100万元，实际完成：90万元，完成率：90%；", "年度目标：1200万元，累计实际完成：540万元，累计完成率：45%；"],
                        "reason": "资料回收慢",
                        "next_target": "110万元",
                        "actions": ["7月12日前补齐资料"],
                    },
                    {
                        "metric_no": 3,
                        "metric_name": "红色指标",
                        "unit": "万元",
                        "display_lines": ["月度目标：100万元，实际完成：110万元，完成率：110%；", "年度目标：1200万元，累计实际完成：300万元，累计完成率：25%；"],
                        "reason": "历史项目推进滞后",
                        "next_target": "130万元",
                        "actions": ["7月20日前形成专项清单"],
                    },
                ]
            )
        ],
        period_label="2026-06",
    )

    statuses = {metric.metric_name: metric.status for metric in reports[0].metrics}
    assert statuses == {"绿色指标": "green", "黄色指标": "yellow", "红色指标": "red"}


def test_leadership_coordination_items_are_actionable_and_filtered():
    reports = parse_department_monthly_reports(
        [
            _source(
                metrics=[
                    {
                        "metric_no": 1,
                        "metric_name": "印章管理智能化建设",
                        "unit": "%",
                        "display_lines": ["月度目标：100%，实际完成：65%，完成率：65%；", "年度目标：100%，累计实际完成：35%，累计完成率：35%；"],
                        "reason": "供应商接口联调延期，需信息化和采购共同压实排期",
                        "next_target": "80%",
                        "actions": ["7月10日前由信息化、采购、供应商确认联调排期"],
                    },
                    {
                        "metric_no": 2,
                        "metric_name": "内部复盘",
                        "unit": "项",
                        "display_lines": ["月度目标：10项，实际完成：9项，完成率：90%；", "年度目标：100项，累计实际完成：50项，累计完成率：50%；"],
                        "reason": "部门内部复盘颗粒度不足",
                        "next_target": "12项",
                        "actions": ["7月5日前完成内部复盘模板"],
                    },
                ]
            )
        ],
        period_label="2026-06",
    )
    summary = aggregate_department_monthly_reports(reports, period_label="2026-06")

    assert len(summary.leadership_items) == 1
    item = summary.leadership_items[0]
    assert item.involved_departments == "法务二部、信息化/采购、供应商"
    assert item.affected_metric == "印章管理智能化建设"
    assert "供应商接口联调延期" in item.current_bottleneck
    assert "确认联调排期" in item.coordination_action
    assert item.suggested_deadline == "7月10日前"


def test_leader_renderer_uses_readable_text_blocks_and_no_raw_detail_dump():
    sources = build_test_department_monthly_sources("2026-06", seed=20260630)
    report = build_department_monthly_report(sources, period_label="2026-06", test_mode=True)

    assert report.startswith("# 【测试】法务合约中心2026年6月部门绩效月报")
    assert "## 📌 一、总体结论" in report
    assert "## 🎯 二、核心指标完成情况" in report
    assert "年度时间进度：50%" in report
    assert "**🔴 重点风险**" in report
    assert "  月度：目标 " in report
    assert "  年度：目标 " in report
    assert "  同比/环比：" in report
    assert "| 指标 | 月目标 | 月实际 | 月完成率" not in report
    assert "## 🚦 三、重点风险指标" in report
    assert "## 四、各部门表现" in report
    assert "## 🤝 五、需领导协调事项" in report
    assert "涉及部门：" in report
    assert "影响指标：" in report
    assert "当前卡点：" in report
    assert "需协调动作：" in report
    assert "建议期限：" in report
    assert "| 事项 | 涉及部门 | 影响指标" not in report
    assert "## ✅ 六、下月重点动作与风险预判" in report
    assert "共47个指标" not in report
    assert "141条" not in report
    assert "附录" not in report
    assert "随机生成" not in report
    assert "…" not in report
    assert "推动跨部门…" not in report


def test_department_monthly_coverage_reports_missing_units():
    sources = build_test_department_monthly_sources("2026-06", seed=3)[:3]
    coverage = department_monthly_coverage(sources)

    assert coverage.ready is False
    assert "法务四部" in coverage.missing_units
    report = build_department_monthly_report(sources, period_label="2026-06")
    assert "待补齐：法务四部" in report


def test_split_markdown_message_keeps_all_content():
    text = "\n".join(f"第{i}行内容" for i in range(100))
    chunks = split_markdown_message(text, max_chars=120)

    assert len(chunks) > 1
    assert "\n".join(chunks).replace("\n\n", "\n") == text
