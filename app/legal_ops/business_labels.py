from __future__ import annotations

UNKNOWN_LABEL = "数据状态异常"

CASE_TYPE_LABELS = {
    "plaintiff_case": "原告案件",
    "defendant_case": "被告案件",
}

PLAINTIFF_STAGE_LABELS = {
    "intended_filing": "拟诉",
    "litigation": "诉讼中",
    "enforcement": "执行中",
    "closed": "已结案",
}

DEFENDANT_STAGE_LABELS = {
    "accepted": "受理",
    "hearing": "开庭",
    "adjudicated": "审结",
    "performance": "履行",
    "closed": "已结案",
}

TRAVEL_STATUS_LABELS = {
    "proposed": "待确认登记",
    "planned": "已登记",
    "confirmed": "已确认",
    "changed": "已变更",
    "completed": "已完成",
    "candidate": "待确认协同",
    "notified": "已创建通知",
    "pending": "等待发送",
    "queued": "已进入发送队列",
    "processing": "正在发送",
    "sending": "正在发送",
    "sent": "平台已接受发送请求",
    "accepted_by_provider": "平台已接受发送请求",
    "delivery_confirmed": "已确认送达",
    "waiting_for_reply": "等待回复",
    "accepted_by_one": "一方已接受",
    "accept": "已接受",
    "accepted": "双方已接受",
    "accepted_by_both": "双方已接受",
    "declined": "已拒绝",
    "reject": "已拒绝",
    "failed": "发送失败",
    "dead_letter": "多次失败，需处理",
    "cancelled": "已取消",
    "expired": "已过期",
}

REPORT_STATUS_LABELS = {
    "collecting": "收集中",
    "pending_confirmation": "待确认",
    "completed": "已提交",
    "cancelled": "已取消",
    "skipped": "已跳过",
}

SOURCE_LABELS = {
    "ERP_PLAINTIFF_CASES": "ERP 原告案件底表",
    "ERP_DEFENDANT_CASES": "ERP 被告案件底表",
    "real_case_workbook": "真实来源灰测副本",
    "real_user_message": "真实用户消息",
    "human_record": "人工录入",
    "imported_record": "ERP 文件导入",
    "robot_followup": "主动追问提取",
    "ai_extracted": "AI 提取待确认",
    "system_fact": "系统事实",
    "system_derived": "系统计算",
    "legacy_import": "历史导入",
    "server_acceptance_smoke": "服务器验收记录",
    "sandbox_fixture": "演示数据",
    "sandbox_persisted": "灰测数据库记录",
}


def case_stage_label(case_type: str, status: str) -> str:
    labels = (
        PLAINTIFF_STAGE_LABELS
        if case_type == "plaintiff_case"
        else DEFENDANT_STAGE_LABELS
        if case_type == "defendant_case"
        else {}
    )
    return labels.get(status, UNKNOWN_LABEL)


def case_type_label(case_type: str) -> str:
    return CASE_TYPE_LABELS.get(case_type, UNKNOWN_LABEL)


def travel_status_label(status: str) -> str:
    return TRAVEL_STATUS_LABELS.get(status, UNKNOWN_LABEL)


def report_status_label(status: str) -> str:
    return REPORT_STATUS_LABELS.get(status, UNKNOWN_LABEL)


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, UNKNOWN_LABEL)
