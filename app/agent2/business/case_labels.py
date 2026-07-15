from __future__ import annotations


def case_type_label(value: object) -> str:
    raw = str(value or "").strip()
    return {
        "plaintiff": "原告案件",
        "plaintiff_case": "原告案件",
        "claimant": "原告案件",
        "defendant": "被告案件",
        "defendant_case": "被告案件",
    }.get(raw, raw)


def case_stage_label(value: object) -> str:
    raw = str(value or "").strip()
    return {
        "intended_filing": "拟诉",
        "litigation": "诉讼中",
        "enforcement": "执行中",
        "accepted": "受理",
        "hearing": "开庭",
        "adjudicated": "审结",
        "performance": "履行",
        "closed": "已结案",
    }.get(raw, raw)
