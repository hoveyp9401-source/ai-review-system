from app.services.performance_service import (
    PERFORMANCE_COMPLETED,
    PERFORMANCE_COLLECTING,
    PERFORMANCE_PENDING_CONFIRMATION,
    apply_performance_reply,
    build_complete_performance_report,
    build_performance_overview_markdown,
    build_performance_reply_prompt,
    initial_responses,
    is_performance_reply_candidate,
    looks_like_performance_reply_template,
    normalize_metrics,
)


def _metrics():
    return normalize_metrics(
        [
            {"metric_no": 1, "name": "诉讼案件收款", "unit": "万元"},
            {"metric_no": 2, "name": "终本案件恢复执行到位率", "unit": "%"},
            {"metric_no": 3, "name": "未审定诉讼结算增加额", "unit": "万元"},
        ]
    )


def _law_second_metrics():
    return normalize_metrics(
        [
            {
                "metric_no": 1,
                "name": "索赔管理",
                "unit": "万元",
                "display_lines": [
                    "周目标：NA#万元，实际完成：NA#万元，完成率：NA#%；",
                    "月度目标：NA#万元，实际完成：NA#万元，完成率：NA#%，同比上升/下降：NA#%，环比上升/下降：NA#%；",
                    "年度目标：NA#万元，累计实际完成：NA#万元，累计完成率：NA#%，同比上升/下降：NA#%；",
                ],
            },
            {
                "metric_no": 6,
                "name": "被告存量/新增案件数量下降率",
                "unit": "%",
                "display_lines": [
                    "周目标：NA#%，实际完成：NA#%，完成率：NA#%；",
                    "月度目标：NA#%，实际完成：NA#%，完成率：NA#%，同比上升/下降：NA#百分点，环比上升/下降：NA#百分点；",
                    "年度目标：NA#%，累计实际完成：NA#%，累计完成率：NA#，同比上升/下降：NA#百分点；",
                ],
            },
        ]
    )


def _law_team_seven_metrics():
    return normalize_metrics(
        [
            {"metric_no": 1, "name": "索赔管理", "unit": "万元"},
            {"metric_no": 2, "name": "非诉收款", "unit": "万元"},
            {"metric_no": 3, "name": "上下游履约资料闭环率", "unit": "%"},
            {"metric_no": 4, "name": "诉讼案件收款（现金）", "unit": "万元"},
            {"metric_no": 5, "name": "诉讼利息收入（现金）", "unit": "万元"},
            {"metric_no": 6, "name": "未审定诉讼结算增加额", "unit": ""},
            {"metric_no": 7, "name": "被告存量/新增案件数量下降率", "unit": "%"},
        ]
    )


def _comprehensive_metrics():
    return normalize_metrics(
        [
            {
                "metric_no": 1,
                "name": "基础综合事务标准化保障",
                "unit": "",
                "display_lines": [
                    "周目标：NA#，实际完成：NA#，完成率：NA#%；",
                    "月度目标：NA#，实际完成：NA#，完成率：NA#%；",
                    "年度目标：NA#，累计实际完成：NA#；",
                ],
            },
            {
                "metric_no": 7,
                "name": "AI场景赋能提效",
                "unit": "",
                "display_lines": [
                    "周目标：NA#，实际完成：NA#，完成率：NA#%；",
                    "月度目标：NA#，实际完成：NA#，完成率：NA#%；",
                    "年度目标：NA#，累计实际完成：NA#；",
                ],
            },
        ]
    )


def _zhu_jiajia_metrics():
    return normalize_metrics(
        [
            {"metric_no": 1, "name": "国别市场研究及合同示范文本", "unit": "项"},
            {"metric_no": 2, "name": "海外风控及履约风险管理指引", "unit": "项"},
            {"metric_no": 3, "name": "评审效率", "unit": "%"},
            {"metric_no": 4, "name": "能力建设", "unit": "项"},
        ]
    )


def test_overview_markdown_only_contains_completion_context():
    overview = build_performance_overview_markdown(_law_second_metrics())

    assert "【指标完成情况概览】" in overview
    assert "**索赔管理**" in overview
    assert "**被告存量/新增案件数量下降率**" in overview
    assert "1. **索赔管理**" not in overview
    assert '周目标：<font color="#003A8C">NA#</font>万元' in overview
    assert '周目标：<font color="#003A8C">NA#</font>%' in overview
    assert "未完成原因" not in overview
    assert "行动方案" not in overview


def test_reply_prompt_only_asks_for_collecting_fields():
    prompt = build_performance_reply_prompt(_law_second_metrics())

    assert prompt.startswith("【请回复】本次需要填写的指标：")
    assert "1. 【索赔管理】" in prompt
    assert "未完成原因/存在问题：" in prompt
    assert "下月目标（万元）：" in prompt
    assert "6. 【被告存量/新增案件数量下降率】" in prompt
    assert "下月目标（%）：" in prompt
    assert "周目标" not in prompt
    assert "月度目标" not in prompt
    assert "年度目标" not in prompt


def test_complete_report_combines_overview_and_user_reply():
    metrics = _law_second_metrics()
    responses = [
        {
            "metric_no": 1,
            "metric_name": "索赔管理",
            "unit": "万元",
            "reason": "客户资料回收较慢",
            "next_target": "100万元",
            "actions": ["梳理重点清单", "每周跟进"],
        },
        {
            "metric_no": 6,
            "metric_name": "被告存量/新增案件数量下降率",
            "unit": "%",
            "reason": "新增案件基数波动",
            "next_target": "10%",
            "actions": ["按案件类型分层压降"],
        },
    ]

    report = build_complete_performance_report(metrics, responses)

    assert "【完整绩效汇报预览】" in report
    assert "1. 索赔管理" in report
    assert "周目标：NA#万元，实际完成：NA#万元，完成率：NA#%；" in report
    assert "（2）未完成原因/存在问题：" in report
    assert "客户资料回收较慢" in report
    assert "下月目标：100万元" in report
    assert "1. 梳理重点清单" in report
    assert "6. 被告存量/新增案件数量下降率" in report
    assert "下月目标：10%" in report


def test_candidate_router_ignores_ordinary_daily_report_text():
    metrics = _law_second_metrics()

    assert (
        is_performance_reply_candidate(
            metrics=metrics,
            responses=initial_responses(metrics),
            raw_input="今天处理合同评审，没什么问题，明天继续开庭。",
        )
        is False
    )


def test_candidate_router_accepts_performance_template_reply():
    metrics = _law_second_metrics()
    raw_input = """
【请回复】本次需要填写的指标：

1. 索赔管理
未完成原因/存在问题：已完成
下月目标（万元）：100
行动方案：继续努力

6. 被告存量/新增案件数量下降率
存在问题：已完成
下月目标（%）：10%
行动方案：加油努力
"""

    assert is_performance_reply_candidate(metrics=metrics, responses=initial_responses(metrics), raw_input=raw_input) is True


def test_candidate_router_rejects_other_task_metric_template():
    metrics = _zhu_jiajia_metrics()
    raw_input = """
1. 【基础综合事务标准化保障】
未完成原因/存在问题：
下月目标：
行动方案：

4. 【AI智能化场景核心转化（数量）】
未完成原因/存在问题：
下月目标：
行动方案：
"""

    assert (
        is_performance_reply_candidate(
            metrics=metrics,
            responses=initial_responses(metrics),
            raw_input=raw_input,
            status=PERFORMANCE_COLLECTING,
        )
        is False
    )
    assert looks_like_performance_reply_template(raw_input) is True


def test_markdown_heading_reply_stays_in_performance_context():
    metrics = _zhu_jiajia_metrics()
    raw_input = """
### 1. 国别市场研究及合同示范文本
未完成原因/存在问题：近期合同评审工作量集中，调研时间被挤占。
下月目标（项）：3项
行动方案：每周固定安排2个工作日开展国别资料整理。

### 2. 海外风控及履约风险管理指引
未完成原因/存在问题：多项目并行推进，案例素材归集不齐全。
下月目标（项）：1项
行动方案：梳理近半年海外项目履约纠纷案例。

### 3. 评审效率
未完成原因/存在问题：部分业务资料提交滞后。
下月目标（%）：
行动方案：前置业务交底，明确报审资料清单。

### 4. 能力建设
未完成原因/存在问题：
下月目标（项）：
行动方案：
"""

    responses = initial_responses(metrics)

    assert (
        is_performance_reply_candidate(
            metrics=metrics,
            responses=responses,
            raw_input=raw_input,
            status=PERFORMANCE_COLLECTING,
        )
        is True
    )

    result = apply_performance_reply(metrics=metrics, responses=responses, raw_input=raw_input)

    assert result.status == PERFORMANCE_COLLECTING
    assert result.touched_metrics == [1, 2, 3]
    assert result.responses[0]["reason"] == "近期合同评审工作量集中，调研时间被挤占"
    assert result.responses[0]["next_target"] == "3项"
    assert result.responses[0]["actions"] == ["每周固定安排2个工作日开展国别资料整理"]
    assert result.responses[2]["reason"] == "部分业务资料提交滞后"
    assert result.responses[2]["next_target"] == ""
    assert result.responses[2]["actions"] == ["前置业务交底，明确报审资料清单"]
    assert 4 in result.missing


def test_one_shot_reply_can_fill_multiple_metrics():
    metrics = _metrics()
    raw_input = """
1、诉讼案件收款
未完成原因：重点客户付款审批延后。
下月目标：7000万元
行动方案：
1. 每周跟踪重点客户付款节点
2. 对逾期回款逐案列清单

2、终本案件恢复执行到位率
原因：部分恢复执行线索仍在核验。
下月目标：0.8%
措施：1. 梳理可恢复案件 2. 推动执行法院沟通
"""

    result = apply_performance_reply(metrics=metrics, responses=initial_responses(metrics), raw_input=raw_input)

    assert result.status == PERFORMANCE_COLLECTING
    assert result.touched_metrics == [1, 2]
    assert result.responses[0]["reason"] == "重点客户付款审批延后"
    assert result.responses[0]["next_target"] == "7000万元"
    assert result.responses[0]["actions"] == ["每周跟踪重点客户付款节点", "对逾期回款逐案列清单"]
    assert result.responses[1]["next_target"] == "0.8%"
    assert 3 in result.missing


def test_law_team_seven_metric_one_shot_reply_is_fully_recognized():
    metrics = _law_team_seven_metrics()
    raw_input = """
1. 索赔管理
未完成原因/存在问题：客户付款审批周期较长。
下月目标（万元）：500万元
行动方案：
1. 7月5日前梳理重点客户清单
2. 每周跟进审批节点

2. 非诉收款
未完成原因/存在问题：部分资料回收慢。
下月目标（万元）：800万元
行动方案：1. 明确资料责任人 2. 每周通报回款进度

3. 上下游履约资料闭环率
未完成原因/存在问题：项目部资料提交不完整。
下月目标（%）：覆盖率100%，闭环率80%
行动方案：1. 建立缺失资料清单 2. 每周推动闭环

4. 诉讼案件收款（现金）
未完成原因/存在问题：客户付款节点滞后。
下月目标（万元）：1500万元
行动方案：1. 锁定重点回款案件 2. 对超期节点提级协调

5. 诉讼利息收入（现金）
未完成原因/存在问题：利息回款确认滞后。
下月目标（万元）：20万元
行动方案：1. 核对利息明细 2. 推进付款确认

6. 未审定诉讼结算增加额
未完成原因/存在问题：结算资料补充慢。
下月目标：500
行动方案：1. 对接项目部补资料 2. 每周复盘推进情况

7. 被告存量/新增案件数量下降率
未完成原因/存在问题：新增案件基数波动。
下月目标（%）：存量下降10%，新增下降10%
行动方案：1. 按案件类型分层压降 2. 每周更新新增案件清单
"""

    assert is_performance_reply_candidate(metrics=metrics, responses=initial_responses(metrics), raw_input=raw_input) is True
    result = apply_performance_reply(metrics=metrics, responses=initial_responses(metrics), raw_input=raw_input)

    assert result.status == PERFORMANCE_PENDING_CONFIRMATION
    assert result.missing == {}
    assert result.touched_metrics == [1, 2, 3, 4, 5, 6, 7]
    assert result.responses[2]["next_target"] == "覆盖率100%，闭环率80%"
    assert result.responses[2]["actions"] == ["建立缺失资料清单", "每周推动闭环"]
    assert result.responses[6]["next_target"] == "存量下降10%，新增下降10%"
    assert result.responses[6]["actions"] == ["按案件类型分层压降", "每周更新新增案件清单"]
    assert "确认提交" in result.message


def test_law_team_seven_metric_common_reply_variants_are_recognized():
    metrics = _law_team_seven_metrics()
    raw_input = """
第1项 索赔管理
存在问题：客户审批慢
目标：500万元
措施：1、客户清单 2、周跟进

第2项 非诉收款
存在问题：资料回收慢
目标：800万元
措施：1、明确责任人

第3项 上下游履约资料闭环率
存在问题：资料闭环口径需统一
目标：覆盖率100%，闭环率80%
措施：1、建立缺失清单

第4项 诉讼案件收款（现金）
存在问题：付款节点滞后
目标：1500万元
措施：1、锁定重点案件

第5项 诉讼利息收入（现金）
存在问题：利息确认滞后
目标：20万元
措施：1、核对明细

第6项 未审定诉讼结算增加额
存在问题：结算资料补充慢
目标：500
措施：1、对接项目部

第7项 被告存量/新增案件数量下降率
存在问题：新增案件波动
目标：存量下降10%，新增下降10%
措施：1、分层压降 2、每周更新清单
"""

    result = apply_performance_reply(metrics=metrics, responses=initial_responses(metrics), raw_input=raw_input)

    assert result.status == PERFORMANCE_PENDING_CONFIRMATION
    assert result.missing == {}
    assert result.responses[0]["reason"] == "客户审批慢"
    assert result.responses[0]["next_target"] == "500万元"
    assert result.responses[0]["actions"] == ["客户清单", "周跟进"]
    assert result.responses[6]["next_target"] == "存量下降10%，新增下降10%"
    assert result.responses[6]["actions"] == ["分层压降", "每周更新清单"]


def test_law_team_seven_metric_incremental_replies_are_merged():
    metrics = _law_team_seven_metrics()
    first = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：客户审批慢。下月目标：500万元。行动方案：1. 客户清单 2. 周跟进
2 未完成原因：资料回收慢。下月目标：800万元。行动方案：1. 明确责任人
3 未完成原因：资料闭环口径需统一。下月目标：覆盖率100%，闭环率80%。行动方案：1. 建立缺失清单
""",
    )

    assert first.status == PERFORMANCE_COLLECTING
    assert first.touched_metrics == [1, 2, 3]
    assert "当前已完成了【索赔管理】【非诉收款】【上下游履约资料闭环率】的填写" in first.message
    assert "请继续填写【诉讼案件收款（现金）】【诉讼利息收入（现金）】【未审定诉讼结算增加额】【被告存量/新增案件数量下降率】" in first.message

    second = apply_performance_reply(
        metrics=metrics,
        responses=first.responses,
        raw_input="""
4 未完成原因：付款节点滞后。下月目标：1500万元。行动方案：1. 锁定重点案件
5 未完成原因：利息确认滞后。下月目标：20万元。行动方案：1. 核对明细
6 未完成原因：结算资料补充慢。下月目标：500。行动方案：1. 对接项目部
7 未完成原因：新增案件波动。下月目标：存量下降10%，新增下降10%。行动方案：1. 分层压降 2. 每周更新清单
""",
        status=first.status,
    )

    assert second.status == PERFORMANCE_PENDING_CONFIRMATION
    assert second.missing == {}
    assert second.responses[0]["reason"] == "客户审批慢"
    assert second.responses[2]["next_target"] == "覆盖率100%，闭环率80%"
    assert second.responses[6]["reason"] == "新增案件波动"
    assert second.responses[6]["actions"] == ["分层压降", "每周更新清单"]


def test_single_remaining_law_team_metric_can_be_filled_without_number():
    metrics = _law_team_seven_metrics()
    responses = initial_responses(metrics)
    for metric_no in range(1, 7):
        responses[metric_no - 1].update(
            {
                "reason": f"原因{metric_no}",
                "next_target": f"目标{metric_no}",
                "actions": [f"行动{metric_no}"],
            }
        )

    result = apply_performance_reply(
        metrics=metrics,
        responses=responses,
        raw_input="未完成原因/存在问题：新增案件波动。下月目标：存量下降10%，新增下降10%。行动方案：1. 分层压降",
    )

    assert result.status == PERFORMANCE_PENDING_CONFIRMATION
    assert result.touched_metrics == [7]
    assert result.responses[6]["reason"] == "新增案件波动"
    assert result.responses[6]["next_target"] == "存量下降10%，新增下降10%"


def test_law_team_metric_target_can_be_modified_by_number_after_preview():
    metrics = _law_team_seven_metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="\n".join(
            f"{metric_no} 未完成原因：原因{metric_no}。下月目标：目标{metric_no}。行动方案：1. 行动{metric_no}"
            for metric_no in range(1, 8)
        ),
    )

    updated = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="把第7项下月目标改为：存量下降12%，新增下降8%",
        status=filled.status,
    )

    assert updated.status == PERFORMANCE_PENDING_CONFIRMATION
    assert updated.touched_metrics == [7]
    assert updated.responses[6]["next_target"] == "存量下降12%，新增下降8%"
    assert updated.responses[5]["next_target"] == "目标6"


def test_incremental_replies_merge_by_metric_without_overwriting():
    metrics = _metrics()
    first = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="1 未完成原因：客户付款节点滞后。下月目标：7000万元。行动方案：1. 列重点清单 2. 周跟踪",
    )
    second = apply_performance_reply(
        metrics=metrics,
        responses=first.responses,
        raw_input="2 未完成原因：执行线索少。下月目标：0.8%。行动方案：1. 梳理终本线索",
        status=first.status,
    )
    third = apply_performance_reply(
        metrics=metrics,
        responses=second.responses,
        raw_input="3 未完成原因：结算资料补充慢。下月目标：800万元。行动方案：1. 对接项目部补资料",
        status=second.status,
    )

    assert third.status == PERFORMANCE_PENDING_CONFIRMATION
    assert third.missing == {}
    assert third.responses[0]["reason"] == "客户付款节点滞后"
    assert third.responses[1]["reason"] == "执行线索少"
    assert third.responses[2]["next_target"] == "800万元"
    assert "确认提交" in third.message


def test_partial_reply_reports_completed_and_remaining_metric_names():
    metrics = _metrics()

    result = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="1 未完成原因：客户付款节点滞后。下月目标：7000万元。行动方案：1. 列重点清单",
    )

    assert result.status == PERFORMANCE_COLLECTING
    assert "当前已完成了【诉讼案件收款】的填写" in result.message
    assert "请继续填写【终本案件恢复执行到位率】【未审定诉讼结算增加额】" in result.message
    assert "还缺" not in result.message


def test_later_metric_update_changes_only_that_field():
    metrics = _metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：A。下月目标：100万元。行动方案：1. A1
2 未完成原因：B。下月目标：10%。行动方案：1. B1
3 未完成原因：C。下月目标：300万元。行动方案：1. C1
""",
    )

    updated = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="第2项下月目标：12%",
        status=filled.status,
    )

    assert updated.status == PERFORMANCE_PENDING_CONFIRMATION
    assert updated.responses[0]["next_target"] == "100万元"
    assert updated.responses[1]["next_target"] == "12%"
    assert updated.responses[1]["reason"] == "B"
    assert updated.responses[2]["next_target"] == "300万元"


def test_metric_field_can_be_updated_with_edit_phrase():
    metrics = _metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：A。下月目标：100万元。行动方案：1. A1
2 未完成原因：B。下月目标：10%。行动方案：1. B1
3 未完成原因：C。下月目标：300万元。行动方案：1. C1
""",
    )

    updated = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="把第2项下月目标改成12%",
        status=filled.status,
    )

    assert updated.status == PERFORMANCE_PENDING_CONFIRMATION
    assert updated.touched_metrics == [2]
    assert updated.responses[1]["next_target"] == "12%"
    assert updated.responses[1]["reason"] == "B"


def test_metric_whole_paragraph_can_be_rewritten_after_preview():
    metrics = _metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：A。下月目标：100万元。行动方案：1. A1
2 未完成原因：B。下月目标：10%。行动方案：1. B1
3 未完成原因：C。下月目标：300万元。行动方案：1. C1
""",
    )

    updated = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="第2项改成：未完成原因/存在问题：审批加快。下月目标：12%。行动方案：1. 每日跟进 2. 每周复盘",
        status=filled.status,
    )

    assert updated.status == PERFORMANCE_PENDING_CONFIRMATION
    assert updated.responses[1]["reason"] == "审批加快"
    assert updated.responses[1]["next_target"] == "12%"
    assert updated.responses[1]["actions"] == ["每日跟进", "每周复盘"]
    assert updated.responses[0]["reason"] == "A"


def test_generic_replacement_updates_existing_performance_draft_only():
    metrics = _metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：A。下月目标：100万元。行动方案：1. 周跟踪
2 未完成原因：B。下月目标：10%。行动方案：1. B1
3 未完成原因：C。下月目标：300万元。行动方案：1. C1
""",
    )

    assert (
        is_performance_reply_candidate(
            metrics=metrics,
            responses=filled.responses,
            raw_input="把周跟踪改成每日跟踪",
            status=filled.status,
        )
        is True
    )
    assert (
        is_performance_reply_candidate(
            metrics=metrics,
            responses=filled.responses,
            raw_input="把合同改成协议",
            status=filled.status,
        )
        is False
    )

    updated = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="把周跟踪改成每日跟踪",
        status=filled.status,
    )

    assert updated.status == PERFORMANCE_PENDING_CONFIRMATION
    assert updated.touched_metrics == [1]
    assert updated.responses[0]["actions"] == ["每日跟踪"]
    assert "已修改第1项" in updated.message
    assert "完整绩效汇报预览" in updated.message


def test_full_preview_paste_does_not_parse_completion_lines_as_next_target():
    metrics = _comprehensive_metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1. 基础综合事务标准化保障
未完成原因/存在问题；无
下月目标；7月准备分范围进行团队建设沟通
行动方案：划分范围，人员归类，制定计划，定期执行

7. AI场景赋能提效
未完成原因/存在问题；使用程度低，转化差，使用意愿不明显
下月目标；半年度测评召开专题会议，通过对个人目标有落地意识主动分解
行动方案：梳理完成情况、会议、分解
""",
    )
    preview = build_complete_performance_report(metrics, filled.responses)

    replayed = apply_performance_reply(metrics=metrics, responses=filled.responses, raw_input=preview, status=filled.status)

    assert replayed.status == PERFORMANCE_PENDING_CONFIRMATION
    assert replayed.responses[0]["next_target"] == "7月准备分范围进行团队建设沟通"
    assert replayed.responses[1]["next_target"] == "半年度测评召开专题会议，通过对个人目标有落地意识主动分解"
    assert "周目标" not in replayed.responses[1]["next_target"]
    assert "（2）未完成原因" not in replayed.responses[1]["next_target"]


def test_bracketed_plain_text_reply_template_is_performance_shaped():
    text = """
1. 【基础综合事务标准化保障】
未完成原因/存在问题：
下月目标：
行动方案：
"""

    assert looks_like_performance_reply_template(text) is True


def test_bracketed_metric_reply_is_parsed_for_active_task():
    metrics = _comprehensive_metrics()
    parsed = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1. 【基础综合事务标准化保障】
未完成原因/存在问题：无
下月目标：7月准备分范围进行团队建设沟通
行动方案：划分范围，人员归类，制定计划，定期执行
""",
    )

    assert parsed.touched_metrics == [1]
    assert parsed.responses[0]["reason"] == "无"
    assert parsed.responses[0]["next_target"] == "7月准备分范围进行团队建设沟通"


def test_metric_action_rewrite_with_metric_name_and_comma_edit_phrase():
    metrics = _comprehensive_metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1. 基础综合事务标准化保障
未完成原因/存在问题：无
下月目标：7月准备分范围进行团队建设沟通
行动方案：划分范围，人员归类，制定计划，定期执行

7. AI场景赋能提效
未完成原因/存在问题：使用程度低
下月目标：半年度测评召开专题会议
行动方案：梳理完成情况、会议、分解
""",
    )

    updated = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="把7. AI场景赋能提效的行动方案，改为1、梳理完成情况、会议、分解 2、重新制定更详细的目标",
        status=filled.status,
    )

    assert updated.status == PERFORMANCE_PENDING_CONFIRMATION
    assert updated.touched_metrics == [7]
    assert updated.responses[1]["actions"] == ["梳理完成情况、会议、分解", "重新制定更详细的目标"]
    assert all("改为" not in action for action in updated.responses[1]["actions"])


def test_preview_submit_phrase_confirms_pending_performance_report():
    metrics = _metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：A。下月目标：100万元。行动方案：1. A1
2 未完成原因：B。下月目标：10%。行动方案：1. B1
3 未完成原因：C。下月目标：300万元。行动方案：1. C1
""",
    )

    done = apply_performance_reply(metrics=metrics, responses=filled.responses, raw_input="【完整绩效汇报预览】提交", status=filled.status)

    assert done.status == PERFORMANCE_COMPLETED
    assert done.confirmed_by_user is True


def test_metric_clear_instruction_stays_in_performance_context():
    metrics = _comprehensive_metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：无。下月目标：7月准备分范围进行团队建设沟通。行动方案：1. 划分范围
7 未完成原因：使用程度低。下月目标：半年度测评召开专题会议。行动方案：1. 梳理完成情况
""",
    )

    assert (
        is_performance_reply_candidate(
            metrics=metrics,
            responses=filled.responses,
            raw_input="7. AI场景赋能提效，重新清空",
            status=filled.status,
        )
        is True
    )
    cleared = apply_performance_reply(
        metrics=metrics,
        responses=filled.responses,
        raw_input="7. AI场景赋能提效，重新清空",
        status=filled.status,
    )

    assert cleared.status == PERFORMANCE_COLLECTING
    assert cleared.touched_metrics == [7]
    assert cleared.responses[1]["reason"] == ""
    assert cleared.responses[1]["next_target"] == ""
    assert cleared.responses[1]["actions"] == []
    assert "请继续填写【AI场景赋能提效】" in cleared.message


def test_confirm_is_blocked_until_all_metric_fields_are_present():
    metrics = _metrics()
    partial = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="1 未完成原因：A。下月目标：100万元。行动方案：1. A1",
    )

    blocked = apply_performance_reply(metrics=metrics, responses=partial.responses, raw_input="确认提交", status=partial.status)

    assert blocked.status == PERFORMANCE_COLLECTING
    assert blocked.confirmed_by_user is False
    assert 2 in blocked.missing
    assert 3 in blocked.missing
    assert "还不能提交" in blocked.message


def test_confirm_completes_when_all_metrics_are_filled():
    metrics = _metrics()
    filled = apply_performance_reply(
        metrics=metrics,
        responses=initial_responses(metrics),
        raw_input="""
1 未完成原因：A。下月目标：100万元。行动方案：1. A1
2 未完成原因：B。下月目标：10%。行动方案：1. B1
3 未完成原因：C。下月目标：300万元。行动方案：1. C1
""",
    )

    done = apply_performance_reply(metrics=metrics, responses=filled.responses, raw_input="确认提交", status=filled.status)

    assert done.status == PERFORMANCE_COMPLETED
    assert done.confirmed_by_user is True
    assert done.missing == {}
