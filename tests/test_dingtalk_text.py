from app.utils.dingtalk_text import format_dingtalk_plain_text


def test_formats_report_insight_markdown_for_dingtalk_text_message():
    source = """庞总，## 刘聪未闭环工作梳理

根据上周及本周提交的日报，以下工作尚未闭环：

---

### 🔴 持续未闭环（超过一周）

| 事项 | 来源 | 最新状态 |
|------|------|----------|
| **法务权限调整方案** | 7/27 标记为问题项 | 至今无最终确定记录 |
| **部门费用梳理** | 7/27 标记为问题项，7/28 继续 | 无闭环记录 |

---

### 🟡 进行中

| 事项 | 进展轨迹 | 当前进度 |
|------|----------|----------|
| **印章库测试** | 7/28 开始 → 8/3 沟通初版问题 | 仍在测试阶段 |"""

    assert format_dingtalk_plain_text(source) == """庞总，刘聪未闭环工作梳理

根据上周及本周提交的日报，以下工作尚未闭环：

🔴 持续未闭环（超过一周）

1. 法务权限调整方案
   来源：7/27 标记为问题项
   最新状态：至今无最终确定记录

2. 部门费用梳理
   来源：7/27 标记为问题项，7/28 继续
   最新状态：无闭环记录

🟡 进行中

1. 印章库测试
   进展轨迹：7/28 开始 → 8/3 沟通初版问题
   当前进度：仍在测试阶段"""


def test_preserves_normal_plain_text_content():
    source = "已找到 3 项未闭环工作。\n请重点关注办理时限。"

    assert format_dingtalk_plain_text(source) == source


def test_formats_bullets_quotes_links_and_empty_table_values():
    source = """## 注意事项
- 第一项
> 补充说明
[查看详情](https://example.com)

| 成员 | 重点工作 | 状态 |
| :--- | --- | ---: |
| 刘聪 | 合同审查 | |"""

    assert format_dingtalk_plain_text(source) == """注意事项
• 第一项
补充说明
查看详情（https://example.com）

1. 刘聪
   重点工作：合同审查
   状态：—"""
