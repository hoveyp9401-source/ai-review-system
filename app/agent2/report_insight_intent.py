from __future__ import annotations

import re


_ORGANIZATION_SCOPE = (
    r"(?:部门|团队|工作组|中心|"
    r"[\u4e00-\u9fffA-Za-z0-9·]{2,12}(?:部门|团队|工作组|中心|部))"
)
_PERSON_ORGANIZATION_QUALIFIER = (
    r"[\u4e00-\u9fffA-Za-z0-9·]{2,12}(?:部门|团队|工作组|中心|部|组)"
)
_COMPOUND_SURNAME = r"(?:欧阳|司马|上官|诸葛|东方|皇甫|尉迟|公孙|慕容|司徒|司空|夏侯)"
_NON_PERSON_BUSINESS_TERMS = frozenset(
    {
        "审计",
        "预算",
        "合同",
        "会议",
        "招聘",
        "培训",
        "档案",
        "材料",
        "审批",
        "归档",
        "法务",
        "财务",
        "人事",
        "行政",
        "项目",
        "案件",
        "客户",
        "供应商",
        "工作",
        "任务",
        "计划",
        "风险",
        "问题",
        "诉讼",
        "仲裁",
        "绩效",
        "报销",
        "采购",
        "销售",
        "运营",
        "系统",
        "流程",
        "日报",
        "周报",
        "月报",
        "安全",
        "纪检",
        "安保",
        "文秘",
    }
)
_PERSON_NAME = (
    rf"(?:[\u4e00-\u9fff]{{2,3}}|{_COMPOUND_SURNAME}[\u4e00-\u9fff]{{1,2}}|"
    r"[\u4e00-\u9fff]{1,6}·[\u4e00-\u9fff·]{1,12}|[A-Za-z][A-Za-z·.'-]{1,30})"
)
_PERSON_SCOPE = rf"(?:{_PERSON_NAME}|{_PERSON_ORGANIZATION_QUALIFIER}的{_PERSON_NAME})"
_DIRECT_PERSON_NAME = _PERSON_NAME
_DIRECT_PERSON_SCOPE = _PERSON_SCOPE
_UNCLOSED_SCOPE = (
    rf"(?:{_ORGANIZATION_SCOPE}|{_DIRECT_PERSON_NAME}|"
    rf"{_PERSON_ORGANIZATION_QUALIFIER}的{_DIRECT_PERSON_NAME})"
)
_QUERY_END = r"(?:吗|呢|嘛)?[？?。！!]*"
_LOOKUP_PREFIX = (
    r"(?:(?:请|麻烦)?(?:帮我|帮忙)?(?:查|查询|统计|看)(?:一下|下)?|请问|想问一下)"
    r"[，,]?"
)
_SUMMARY_PREFIX = (
    r"(?:请|麻烦)?(?:帮我|帮忙)?(?:总结|汇总|概括|梳理)(?:一下|下)?[，,]?"
)
_UNCLOSED_LOOKUP = (
    r"(?:(?:请|麻烦)?(?:帮我|帮忙)?(?:看|查|梳理|总结)(?:一下|下)?|请问)"
    r"[，,]?"
)


def report_insight_query_kind(text: str) -> str | None:
    """Recognize one complete, read-only daily-report insight query."""

    compact = "".join(str(text or "").split())
    if not compact:
        return None
    if _matches_report_count_query(compact):
        if re.search(r"(?:已经|已)完成了?(?:多少份|几份)日报", compact):
            return "completed_report_count"
        return "report_count"
    if _matches_unclosed_work_query(compact):
        return "unclosed_work"
    if _matches_recent_work_query(compact):
        return "recent_work"
    if _matches_department_attention_query(compact):
        return "department_recent_attention"
    if _matches_period_work_query(compact, period="本周"):
        return "team_current_week_work"
    if _matches_period_work_query(compact, period="上周"):
        return "team_previous_week_work"
    return None


def is_report_insight_question(text: str) -> bool:
    return standalone_report_insight_query_kind(text) is not None


def standalone_report_insight_query_kind(text: str) -> str | None:
    # Every accepted form is anchored to the full message. A daily-work statement
    # followed by a query therefore continues through the normal write-aware path.
    return report_insight_query_kind(text)


def routable_report_insight_query_kind(text: str) -> str | None:
    """Return insight kinds safe to route without live directory validation."""

    query_kind = standalone_report_insight_query_kind(text)
    if query_kind == "recent_work":
        return None
    if query_kind == "unclosed_work":
        scope = report_insight_query_scope(text, query_kind).strip().rstrip("的")
        if not scope.endswith(("部门", "团队", "工作组", "中心", "部", "组")):
            return None
    return query_kind


def report_insight_query_scope(text: str, query_kind: str) -> str:
    compact = "".join(str(text or "").split())
    if query_kind in {"report_count", "completed_report_count"}:
        return _count_query_scope(compact)
    if query_kind == "recent_work":
        return _recent_query_scope(compact)
    if query_kind == "unclosed_work":
        return _unclosed_query_scope(compact, lookup=_UNCLOSED_LOOKUP)
    if query_kind in {"team_current_week_work", "team_previous_week_work"}:
        return _period_query_scope(compact)
    if query_kind == "department_recent_attention":
        return _attention_query_scope(compact)
    return ""


def _matches_report_count_query(text: str) -> bool:
    count_state = (
        r"(?:(?:目前|现在|至今)(?:一共|总共)?(?:有|共有)?|"
        r"(?:已经|已)(?:完成)?了?|(?:一共|总共|共有|有))?"
    )
    target = rf"(?:{_LOOKUP_PREFIX}{_PERSON_SCOPE}|{_DIRECT_PERSON_SCOPE})"
    patterns = (
        rf"{target}(?:的)?{count_state}"
        rf"(?:多少份|几份)日报(?:记录)?(?:了)?{_QUERY_END}",
        rf"{target}(?:的)?日报(?:的)?数量"
        rf"(?:是|有)?(?:多少|几份){_QUERY_END}",
        rf"{_LOOKUP_PREFIX}{_PERSON_SCOPE}(?:的)?日报(?:的)?数量{_QUERY_END}",
    )
    if not _fullmatch_any(text, patterns):
        return False
    return not _looks_like_non_person_scope(_count_query_scope(text))


def _matches_recent_work_query(text: str) -> bool:
    recent_work = (
        rf"{_DIRECT_PERSON_SCOPE}(?:最近|近期)(?:已经|已)?"
        r"(?:完成(?:了)?的?)?的?工作(?:情况|内容)?"
    )
    patterns = (
        rf"{_SUMMARY_PREFIX}{recent_work}{_QUERY_END}",
        rf"{_DIRECT_PERSON_SCOPE}(?:最近|近期)(?:都)?"
        rf"(?:(?:已经|已)?完成了?|做了|干了|推进了)"
        rf"(?:哪些|什么|啥)(?:工作|事情|事项|事)?{_QUERY_END}",
        rf"{_DIRECT_PERSON_SCOPE}(?:最近|近期)的?工作(?:有)?(?:哪些|什么)"
        rf"{_QUERY_END}",
    )
    if not _fullmatch_any(text, patterns):
        return False
    return not _looks_like_non_person_scope(_recent_query_scope(text))


def _matches_unclosed_work_query(text: str) -> bool:
    unclosed = r"(?:没|未|没有|尚未)闭环"
    subject = r"(?:的)?(?:工作|事项|任务|计划)?"
    patterns = (
        rf"{_UNCLOSED_LOOKUP}{_UNCLOSED_SCOPE}(?:有|还有|存在)?(?:什么|哪些)?{unclosed}{subject}{_QUERY_END}",
    )
    if not _fullmatch_any(text, patterns):
        return False
    return not _looks_like_non_person_scope(_unclosed_query_scope(text, lookup=_UNCLOSED_LOOKUP))


def _matches_period_work_query(text: str, *, period: str) -> bool:
    scoped_period = rf"(?:{_ORGANIZATION_SCOPE}的?)?{period}"
    work_question = (
        r"(?:都)?(?:做了|完成了|开展了|推进了)(?:什么|哪些|啥)"
        r"(?:工作|事情|事项|事)?"
    )
    work_summary = r"(?:的)?工作(?:情况|内容|总结)?"
    patterns = (
        rf"{_SUMMARY_PREFIX}{scoped_period}(?:{work_question}|{work_summary}){_QUERY_END}",
        rf"{scoped_period}{work_question}{_QUERY_END}",
        rf"{scoped_period}(?:的)?工作(?:有)?(?:哪些|什么){_QUERY_END}",
    )
    return _fullmatch_any(text, patterns)


def _matches_department_attention_query(text: str) -> bool:
    recent_scope = rf"(?:最近|近期){_ORGANIZATION_SCOPE}"
    attention_subject = r"(?:重点|风险|问题|事项|事情)"
    patterns = (
        rf"{recent_scope}(?:有|存在)?(?:什么|哪些){attention_subject}"
        rf"(?:需要|值得)?关注(?:的)?(?:事情|事项|问题)?{_QUERY_END}",
        rf"{recent_scope}(?:有|存在)?(?:什么|哪些)(?:需要|值得)?关注(?:的)?"
        rf"{attention_subject}{_QUERY_END}",
        rf"{recent_scope}(?:需要|值得)?关注(?:什么|哪些)(?:的)?"
        rf"{attention_subject}?{_QUERY_END}",
        rf"{recent_scope}(?:有|存在)(?:什么|哪些)?{attention_subject}{_QUERY_END}",
        rf"{_SUMMARY_PREFIX}(?:最近|近期){_ORGANIZATION_SCOPE}(?:的)?"
        rf"{attention_subject}(?:(?:需要|值得)关注(?:的)?(?:事情|事项|问题)?)?"
        rf"{_QUERY_END}",
    )
    return _fullmatch_any(text, patterns)


def _fullmatch_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.fullmatch(pattern, text) is not None for pattern in patterns)


def _count_query_scope(text: str) -> str:
    body = re.sub(rf"^{_LOOKUP_PREFIX}", "", text, count=1)
    question = re.search(r"(?:多少份|几份)日报|日报(?:的)?数量", body)
    if question is None:
        return ""
    scope = body[: question.start()]
    scope = re.sub(
        r"(?:(?:目前|现在|至今)(?:一共|总共)?(?:有|共有)?|"
        r"(?:已经|已)(?:完成)?了?|(?:一共|总共|共有|有))$",
        "",
        scope,
    )
    return scope.rstrip("的")


def _recent_query_scope(text: str) -> str:
    body = re.sub(rf"^{_SUMMARY_PREFIX}", "", text, count=1)
    return body.split("最近", 1)[0].split("近期", 1)[0]


def _unclosed_query_scope(text: str, *, lookup: str) -> str:
    body = re.sub(rf"^{lookup}", "", text, count=1)
    return re.split(
        r"(?:有|还有|存在)?(?:什么|哪些)?(?:没有|尚未|没|未)闭环",
        body,
        maxsplit=1,
    )[0]


def _period_query_scope(text: str) -> str:
    body = re.sub(rf"^{_SUMMARY_PREFIX}", "", text, count=1)
    return re.split(r"(?:本周|上周)", body, maxsplit=1)[0].rstrip("的")


def _attention_query_scope(text: str) -> str:
    body = re.sub(rf"^{_SUMMARY_PREFIX}", "", text, count=1)
    body = re.sub(r"^(?:最近|近期)", "", body, count=1)
    match = re.match(_ORGANIZATION_SCOPE, body)
    return str(match.group(0) if match else "").rstrip("的")


def _looks_like_non_person_scope(value: str) -> bool:
    scope = str(value or "").strip().rstrip("的")
    if not scope:
        return True
    if scope.startswith(("今天", "今日", "昨天", "昨日", "明天", "明日", "刚才")):
        return True
    if scope.endswith(("案件", "项目", "案")):
        return True
    if scope.endswith(("部门", "团队", "工作组", "中心", "部", "组")):
        return False
    person_name = scope.rsplit("的", 1)[-1]
    return any(term in person_name for term in _NON_PERSON_BUSINESS_TERMS)
