from __future__ import annotations

import re


DAILY_FIELD_MARKERS = (
    "日报",
    "日志",
    "草稿",
    "今日工作",
    "今天工作",
    "今日事项",
    "工作内容",
    "问题/风险",
    "问题风险",
    "风险问题",
    "明日计划",
    "明天计划",
    "明日工作",
    "明天工作",
    "计划里面",
)

SPOKEN_CORRECTION_MARKERS = (
    "写错",
    "打错",
    "识别错",
    "听错",
    "记错",
    "错了",
)

LOCAL_DELETE_TARGETS = (
    "括号",
    "括号里",
    "括号内",
    "括号里面",
    "括号内容",
    "括号的内容",
    "这些字",
    "这几个字",
    "这个字",
    "多余文字",
    "其他文字",
)

DELETE_MARKERS = ("删掉", "删除", "删了", "去掉", "清掉", "去除")


def compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\[\]【】\"'“”‘’]+", "", str(value or "")).lower()


def contains_any(value: str, markers: tuple[str, ...]) -> bool:
    text = compact(value)
    return any(compact(marker) in text for marker in markers)


def has_daily_field_reference(text: str) -> bool:
    return contains_any(text, DAILY_FIELD_MARKERS)


def looks_like_spoken_correction(text: str) -> bool:
    value = str(text or "")
    if not contains_any(value, SPOKEN_CORRECTION_MARKERS):
        return False
    if has_daily_field_reference(value):
        return True
    parts = re.split(r"(?:写错|打错|识别错|听错|记错|错了)", value, maxsplit=1)
    return len(parts) == 2 and bool(parts[0].strip() and parts[1].strip())


def looks_like_local_delete(text: str) -> bool:
    return contains_any(text, LOCAL_DELETE_TARGETS) and contains_any(text, DELETE_MARKERS)


def looks_like_parenthetical_delete(text: str) -> bool:
    return contains_any(text, ("括号", "括号里", "括号内", "括号里面", "括号内容", "括号的内容")) and contains_any(
        text,
        DELETE_MARKERS,
    )


def looks_like_contextual_daily_edit(text: str) -> bool:
    value = str(text or "")
    if not value.strip():
        return False
    if looks_like_spoken_correction(value):
        return True
    if looks_like_local_delete(value):
        return True
    return False
