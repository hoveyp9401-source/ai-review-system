from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
import unicodedata


_DAILY_SECTION_LABELS = {
    "今日完成": "today_work",
    "今日工作": "today_work",
    "今天完成": "today_work",
    "今天工作": "today_work",
    "当日完成": "today_work",
    "当日工作": "today_work",
    "问题与风险": "problems",
    "风险与问题": "problems",
    "问题和风险": "problems",
    "风险和问题": "problems",
    "问题/风险": "problems",
    "风险/问题": "problems",
    "问题": "problems",
    "风险": "problems",
    "明日计划": "tomorrow_plan",
    "明天计划": "tomorrow_plan",
    "次日计划": "tomorrow_plan",
}
_LABEL_PATTERN = "|".join(
    re.escape(value)
    for value in sorted(_DAILY_SECTION_LABELS, key=len, reverse=True)
)
_SECTION_HEADER = re.compile(
    rf"(?m)(?:^|[\n。；;])[ \t]*(?:【(?P<bracket>{_LABEL_PATTERN})】"
    rf"|\[(?P<ascii>{_LABEL_PATTERN})\]"
    rf"|(?P<plain>{_LABEL_PATTERN})(?:(?:就是|是|为)|[：:])?)[ \t]*(?:[：:][ \t]*)?"
)
_ITEM_NUMBER = re.compile(
    r"^(?:(?:\d+|[一二三四五六七八九十百]+)[.、．)）]"
    r"|\((?:\d+|[一二三四五六七八九十百]+)\)"
    r"|（(?:\d+|[一二三四五六七八九十百]+)）"
    r"|[-*•·])[ \t]*"
)
_ITEM_SEPARATOR = re.compile(r"[；;\n]+")
_TERMINATION_STATE = re.compile(
    r"(?:^(?:(?:(?:目前|当前|现)?(?:已经|已)(?:被)?)|被(?:暂时|临时)?|"
    r"暂时|临时|刚刚|刚)?(?:取消|撤销|撤回|停止|暂停|放弃|终止)"
    r"|(?:(?:(?:目前|当前|现)?(?:已经|已)(?:被)?)|被(?:暂时|临时)?|"
    r"暂时|临时|刚刚|刚)?(?:取消|撤销|撤回|停止|暂停|放弃|终止)"
    r"(?:掉|完|好)?(?:了|过|着)?(?:没(?:有)?)?"
    r"[啊呀哇哪呐呢吧嘛么吗啦咯喽啰咧嘞哩唻唉嗳欸诶哎哟呦哦噢喔哈呵嘿耶哒吖啵呗罢噻哉也矣焉兮的滴捏]{0,3}$)"
)
_DAILY_RESULT_PREDICATE = (
    r"(?:完成|做完|办完|结束|"
    r"处理(?:完|好)?|审核(?:完|好)?|复核(?:完|好)?|推进(?:完|好)?|"
    r"跟进(?:完|好)?|整理(?:完|好)?|准备(?:完|好)?|制作(?:完|好)?|"
    r"提交(?:完|好)?|归档(?:完|好)?|验收(?:完|好)?|联系(?:完|好)?|"
    r"沟通(?:完|好)?|取消|撤销|撤回|停止|暂停|放弃|终止)"
)
_COLLOQUIAL_QUESTION_SUFFIX = re.compile(
    rf"(?:[吗么]$"
    rf"|{_DAILY_RESULT_PREDICATE}(?:了|过|着)?"
    rf"(?:没(?:有)?(?:呢|啊|呀)?|否|对吧|对不对|是不是|吧|不)$)"
)
_GENERIC_ASPECT_QUESTION_SUFFIX = re.compile(
    r"(?:[\u3400-\u9fff]{1,6}(?:了|过|着|完|好))"
    r"(?:没(?:有)?(?:呢|啊|呀)?|否|对吧|对不对|是不是|吧|不)$"
)
_REPEATED_PREDICATE_QUESTION_SUFFIX = re.compile(
    r"(?P<verb>[\u3400-\u9fff]{1,4})(?:了|过)?(?:还是)?"
    r"(?:没(?:有)?|不)(?P=verb).*$"
)
_LATIN_NONASSERTIVE_TAIL = re.compile(
    r"[A-Za-z][A-Za-z0-9_-]{0,40}(?:了|过|完)?"
    r"(?:没(?:有)?(?:呢|啊|呀)?|吗|对吗|对不对|是不是|吧|不)$"
)
_LATIN_REPEATED_PREDICATE_QUESTION = re.compile(
    r"(?P<left>[A-Za-z][A-Za-z0-9_-]{1,40})"
    r"(?:没(?:有)?|不|还是不)"
    r"(?P<right>[A-Za-z][A-Za-z0-9_-]{1,40}?)(?:完)?$"
)
_GENERAL_NONASSERTIVE_TAIL = re.compile(
    r"^[\u3400-\u9fff]{2,}"
    r"(?:没(?:有)?(?:呢|啊|呀)?|否|对吧|对不对|是不是|吧|不)$"
)
_OPEN_QUESTION_CONSTRUCTION = re.compile(
    r"(?:是否|是不是|有没有|能不能|可不可以|什么|为何|为什么|怎么|如何|"
    r"能否|可否|请问)"
)
_QUESTION_AS_WORK_PREFIX = re.compile(
    r"^(?:核查|确认|检查|梳理|研究|评估|分析|复核|调研)"
)
_TERMINATION_NOMINAL_WORK = re.compile(
    r"^(?:(?:继续|持续|明天|明日|后续|本周|下周|重点|进一步|着重|优先)){0,2}"
    r"(?P<work>跟进|研究|处理|评估|分析|梳理|准备|起草|审查|审核|复核|"
    r"讨论|复盘|办理|推进|协调|调研|核查)"
    r"(?P<object>.{1,40}?)(?:取消|撤销|撤回|停止|暂停|放弃|终止)$"
)
_TERMINATION_ANALYTIC_WORK = frozenset(
    {"研究", "评估", "分析", "审查", "复核", "讨论", "复盘", "调研", "核查"}
)
_TERMINATION_STATE_MARKER = re.compile(
    r"(?:目前|当前|现)?(?:已经|已)(?:被)?$|被(?:暂时|临时)?$|"
    r"(?:目前|当前|暂时|临时|刚刚|刚)$"
)
_TERMINATION_STATE_CONSTRUCTION = re.compile(
    r"(?:已经|曾经|现已|确已|早已|刚刚|不再|不予)"
    r"|已(?:被|由|经|正式|确认|全面|依法|明确|实际|最终|主动|被动|彻底|"
    r"暂时|临时|决定|通知|安排|完成|实施|予以|裁定|判决|认定|公告)"
    r"|(?:目前|当前|暂时|临时|后来|突然|最终|正式)"
    r"(?:因|由于|由|经|按照|基于|受|已经|已|被|决定|确认|最终|全面|"
    r"不再|不予|就|才|又|再)"
    r"|被.+"
    r"|(?:后来|突然|最终|正式)$"
)
_TERMINATION_RELATIVE_STATE_PREFIX = re.compile(
    r"^(?:"
    r"(?:着|了|过|完(?:成|毕)?|好|结束)(?:后)?"
    r"|(?:过程|期间)(?:中)?"
    r"|(?:了|持续|反复|超过|长达|已有)?"
    r"(?:[零〇一二三四五六七八九十百千万两\d]+(?:个)?"
    r"(?:小时|天|日|周|月|年|次|轮|阶段|季度))"
    r"|(?:多|数|几)(?:小时|天|日|周|月|年|次|轮|阶段|季度)"
    r"|已久"
    r"|(?:了|持续)?(?:一段|一阵|很长|较长|许久|很久)(?:时间|日子)?"
    r"|到(?:一半|中途)"
    r"|(?:仍在|还在|正在)(?:进行|跟进|审核|办理|处理)?"
    r"|尚未(?:完成|结束|办完|处理完|审核完)?"
    r"|(?:[\u3400-\u9fff]{1,8})?"
    r"(?:阶段|时期|周期|期间|期内|状态|范围|过程|流程|环节|初期|中期|后期)"
    r"(?:中|内|下|里|上)?"
    r")(?:后|中)?的"
)
_TERMINATION_NOMINAL_LEGAL_SUBJECT = re.compile(
    r"^(?:被告|被[\u3400-\u9fff]{1,8}(?:人|单位|企业|组织|机构|公司|"
    r"主体|经营))"
    r"(?:(?:资格|授权|许可|项目|事项|合同|案件|义务|权利|责任|身份|"
    r"行为|材料))?$"
)
_TERMINATION_LEXEME = re.compile(
    r"(?:取消|撤销|撤回|停止|暂停|放弃|终止)"
)
_TERMINATION_NOMINAL_POSSESSIVE_OBJECT = re.compile(
    r"^[\u3400-\u9fff]{0,8}"
    r"(?:人|客户|公司|企业|单位|组织|机构|部门|团队|项目|案件)的"
    r"(?:申请|许可|商标|决议|合同|项目|案件|资格|授权|协议|决定|事项|"
    r"权利|义务|责任|身份)$"
)
_TERMINATION_SIMPLE_POSSESSIVE_OBJECT = re.compile(
    r"^(?P<owner>[\u3400-\u9fff]{1,10})的"
    r"(?P<owned>[\u3400-\u9fff]{1,10})$"
)
_DAILY_SECTION_CUE = re.compile(
    r"^(?:(?:今日|今天)?(?:的)?(?:问题和风险|问题与风险|风险和问题|问题)"
    r"(?:就是|是|填|写|改为|改成|调整为|[，,:：]\s*)(?P<problems>.+)"
    r"|(?:明日|明天)?计划(?:填|写|记|改为|改成|调整为|[，,:：]\s*)"
    r"(?P<plan>.+))$",
    re.DOTALL,
)
_DAILY_ITEM_SECTION_CORRECTION = re.compile(
    r"^(?P<value>\S.+?)[，,]\s*这是(?P<target>明天|明日|今天|今日)"
    r"(?:的)?(?:工作|计划|工作计划)$",
    re.DOTALL,
)
_DAILY_ITEM_COMPLETION_CORRECTION = re.compile(
    r"^(?P<value>\S.+?)(?:今天|今日)(?:已经|已)?完成(?:了)?$",
    re.DOTALL,
)
_DAILY_COMPOUND_SEPARATOR = re.compile(
    r"(?:\n\s*\n+|(?<=[。！？!?])\s*(?="
    r"(?:今日|今天)?(?:的)?(?:问题和风险|问题与风险|风险和问题|问题)"
    r"(?:就是|是|填|写|改为|改成|调整为|[，,:：]\s*)))"
)


@dataclass(frozen=True)
class StructuredReportItem:
    field: str
    value: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class StructuredDailyDocument:
    items: tuple[StructuredReportItem, ...]
    fields: frozenset[str]


def daily_semantic_detection_copy(value: str) -> str:
    """Return text-only semantic content without mutating the stored value."""

    return "".join(
        character
        for character in str(value or "")
        if unicodedata.category(character)[0] not in {"P", "S", "M", "C", "Z"}
    ).strip()


def daily_item_is_unpunctuated_question(value: str) -> bool:
    """Return whether a Daily item ends in a closed colloquial question form."""

    source = daily_semantic_detection_copy(value)
    latin_repeated = _LATIN_REPEATED_PREDICATE_QUESTION.search(source)
    work_prefix = _QUESTION_AS_WORK_PREFIX.match(source)
    if work_prefix is not None:
        embedded = source[work_prefix.end() :]
        if (
            _OPEN_QUESTION_CONSTRUCTION.search(embedded)
            or _REPEATED_PREDICATE_QUESTION_SUFFIX.search(embedded)
            or embedded.endswith("与否")
        ):
            return False
    if (
        _COLLOQUIAL_QUESTION_SUFFIX.search(source)
        or _GENERIC_ASPECT_QUESTION_SUFFIX.search(source)
        or _REPEATED_PREDICATE_QUESTION_SUFFIX.search(source)
        or _LATIN_NONASSERTIVE_TAIL.search(source)
        or (
            latin_repeated is not None
            and str(latin_repeated.group("left") or "").lower().endswith(
                str(latin_repeated.group("right") or "").lower()
            )
        )
        or _GENERAL_NONASSERTIVE_TAIL.search(source)
    ):
        return True
    return bool(
        _OPEN_QUESTION_CONSTRUCTION.search(source)
        and work_prefix is None
    )


def daily_item_has_termination_state(value: str) -> bool:
    """Return whether an item states cancellation or another terminal change."""

    # This is a detection-only copy. Preserve the original item for storage and
    # replies, while preventing Unicode formatting, whitespace, punctuation,
    # symbols, or combining marks from splitting a terminal-state token (for
    # example ``撤\u200b销`` or ``撤 销``).
    source = daily_semantic_detection_copy(value)
    nominal_work = _TERMINATION_NOMINAL_WORK.fullmatch(source)
    if nominal_work is not None and not _nominal_work_contains_state_construction(
        str(nominal_work.group("object") or ""),
        work_verb=str(nominal_work.group("work") or ""),
    ):
        return False
    return bool(_TERMINATION_STATE.search(source))


def daily_item_is_nominal_termination_work(value: str) -> bool:
    """Return whether a terminal word is the subject of an explicit work item."""

    source = daily_semantic_detection_copy(value)
    match = _TERMINATION_NOMINAL_WORK.fullmatch(source)
    return bool(
        match is not None
        and not _nominal_work_contains_state_construction(
            str(match.group("object") or ""),
            work_verb=str(match.group("work") or ""),
        )
    )


def _nominal_work_contains_state_construction(
    object_text: str,
    *,
    work_verb: str,
) -> bool:
    if _TERMINATION_NOMINAL_LEGAL_SUBJECT.fullmatch(object_text):
        return False
    if _TERMINATION_RELATIVE_STATE_PREFIX.search(object_text):
        return True
    relative_end = object_text.find("的")
    if relative_end > 0 and object_text.startswith(("已经", "曾经", "已")):
        relative_clause = object_text[:relative_end]
        if not _TERMINATION_LEXEME.search(relative_clause):
            return False
    if _TERMINATION_NOMINAL_POSSESSIVE_OBJECT.fullmatch(object_text):
        return False
    if object_text.startswith(("的", "中的", "过的")):
        return True
    simple_possessive = _TERMINATION_SIMPLE_POSSESSIVE_OBJECT.fullmatch(object_text)
    if simple_possessive is not None:
        owner = str(simple_possessive.group("owner") or "")
        owned = str(simple_possessive.group("owned") or "")
        if not (
            _TERMINATION_LEXEME.search(owner)
            or _TERMINATION_STATE_MARKER.search(owner)
            or _TERMINATION_STATE_CONSTRUCTION.search(owner)
            or _TERMINATION_RELATIVE_STATE_PREFIX.search(object_text)
            or _TERMINATION_LEXEME.search(owned)
            or _TERMINATION_STATE_MARKER.search(owned)
            or _TERMINATION_STATE_CONSTRUCTION.search(owned)
            or owned.endswith(("已", "已经", "被", "最终", "目前", "当前"))
        ):
            return False
    if (
        0 < relative_end < len(object_text) - 1
        and work_verb not in _TERMINATION_ANALYTIC_WORK
    ):
        return True
    return bool(
        object_text.startswith(("的", "中的", "过的"))
        or _TERMINATION_RELATIVE_STATE_PREFIX.search(object_text)
        or _TERMINATION_STATE_MARKER.search(object_text)
        or _TERMINATION_STATE_CONSTRUCTION.search(object_text)
    )


def parse_structured_daily_section(
    text: str,
    field: str,
) -> tuple[StructuredReportItem, ...] | None:
    """Parse one explicitly labeled Daily section without semantic rewriting."""

    if field not in {"today_work", "problems", "tomorrow_plan"}:
        return None
    source = str(text or "")
    headers = list(_SECTION_HEADER.finditer(source))
    matching_indexes = [
        index
        for index, header in enumerate(headers)
        if _DAILY_SECTION_LABELS[
            next(
                value
                for value in (
                    header.group("bracket"),
                    header.group("ascii"),
                    header.group("plain"),
                )
                if value
            )
        ]
        == field
    ]
    if len(matching_indexes) != 1:
        return None
    index = matching_indexes[0]
    header = headers[index]
    section_end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
    items = _parse_section_items(
        source,
        field=field,
        start_offset=header.end(),
        end_offset=section_end,
    )
    if not items or any(
        _daily_section_item_is_nonassertive(
            item.field,
            item.value,
            raw=source[item.start_offset:item.end_offset],
        )
        for item in items
    ):
        return None
    return items


def parse_structured_daily_document(text: str) -> StructuredDailyDocument | None:
    """Parse a self-identifying Daily document without consulting conversation focus.

    A high-confidence document must identify both current work and the next-day
    plan.  This is deliberately a document-shape contract, not a classifier
    fallback: old Weekly/Monthly focus is never evidence about the document type.
    """

    source = str(text or "")
    headers = list(_SECTION_HEADER.finditer(source))
    if len(headers) < 2:
        return _parse_explicit_temporal_daily_document(source)
    items: list[StructuredReportItem] = []
    fields: set[str] = set()
    for index, header in enumerate(headers):
        label = next(
            value
            for value in (
                header.group("bracket"),
                header.group("ascii"),
                header.group("plain"),
            )
            if value
        )
        field = _DAILY_SECTION_LABELS[label]
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
        section_items = _parse_section_items(
            source,
            field=field,
            start_offset=header.end(),
            end_offset=section_end,
        )
        if any(
            _daily_section_item_is_nonassertive(
                item.field,
                item.value,
                raw=source[item.start_offset:item.end_offset],
            )
            for item in section_items
        ):
            return None
        if section_items:
            fields.add(field)
            items.extend(section_items)
    if not {"today_work", "tomorrow_plan"}.issubset(fields):
        return None
    return StructuredDailyDocument(items=tuple(items), fields=frozenset(fields))


_TEMPORAL_CLAUSE = re.compile(r"[^；;\n。！？!?]+[；;\n。！？!?]*")
_TODAY_CLAUSE = re.compile(r"^(?:我)?(?:今天|今日)(?P<content>.+)$")
_TOMORROW_CLAUSE = re.compile(r"^(?:我)?(?:明天|明日)(?P<content>.+)$")
_TODAY_ASSERTIVE_PREDICATE = re.compile(
    r"(?:完成|做了|处理|审核|推进|跟进|整理|制作|参加|沟通|开会|开庭|提交|梳理|评估|复盘|出差|归档|验收)"
)
_TOMORROW_PLAN_PREDICATE = re.compile(
    r"(?:跟进|推进|处理|审核|整理|制作|参加|提交|沟通|开会|开庭|出差|前往|去|拜访|准备|完成|复核|发送|回复|联系|梳理|评估|复盘|验收)"
)
_STANDALONE_TODAY_WORK_PREFIX = re.compile(
    r"^(?:我)?(?:今天|今日)(?:的)?(?P<body>.+)$",
    re.DOTALL,
)
_STANDALONE_TODAY_WORK_PREDICATE = re.compile(
    r"^(?:主要)?(?:完成(?:了)?|做了|处理(?:了)?|审核(?:了)?|推进(?:了)?|跟进(?:了)?|"
    r"整理(?:了)?|制作(?:了)?|参加(?:了)?|沟通(?:了)?|联系(?:了)?|开会|开庭|"
    r"提交(?:了)?|梳理(?:了)?|评估(?:了)?|复盘(?:了)?|出差|归档(?:了)?|验收(?:了)?)"
)
_EXPLICIT_TODAY_WORK_LABEL = re.compile(
    r"^(?:今日|今天)(?:工作|完成)(?:是|为|：|:|，|,)?(?P<content>.+)$",
    re.DOTALL,
)


def _parse_explicit_temporal_daily_document(
    source: str,
) -> StructuredDailyDocument | None:
    """Parse only an all-Daily sequence of explicit today/tomorrow clauses.

    This closes a model-variance gap for phrases such as
    ``今天完成……；明天跟进……`` while deliberately refusing questions,
    negations, cancellations, hypotheticals, quotations, and mixed-domain
    clauses.  Those continue through the semantic model.
    """

    items: list[StructuredReportItem] = []
    fields: set[str] = set()
    clause_count = 0
    for clause_match in _TEMPORAL_CLAUSE.finditer(source):
        raw = clause_match.group(0)
        body = raw.strip(" \t\r\n，,；;。！？!?")
        if not body:
            continue
        clause_count += 1
        if _temporal_daily_clause_is_nonassertive(raw, body):
            return None
        today = _TODAY_CLAUSE.fullmatch(body)
        tomorrow = _TOMORROW_CLAUSE.fullmatch(body)
        if today is not None:
            content = str(today.group("content") or "").strip(" \t，,:：")
            if not content or not _TODAY_ASSERTIVE_PREDICATE.search(content):
                return None
            if re.search(
                r"(?:没|没有|未|尚未|还没|并未).{0,8}"
                r"(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交|归档|验收)",
                content,
            ):
                return None
            field = "today_work"
        elif tomorrow is not None:
            content = str(tomorrow.group("content") or "").strip(" \t，,:：")
            if not content or not _TOMORROW_PLAN_PREDICATE.search(content):
                return None
            if re.search(
                r"(?:取消|不去|不再|不用|无需|没法|无法|去不了|不做|不处理)",
                content,
            ):
                return None
            field = "tomorrow_plan"
        else:
            return None
        body_start = clause_match.start() + raw.find(body)
        # Keep the explicit temporal cue in the segment used by Admission while
        # storing only the concise item content in the Daily entity.
        start = body_start
        items.append(
            StructuredReportItem(
                field=field,
                value=content,
                start_offset=start,
                end_offset=body_start + len(body),
            )
        )
        fields.add(field)
    if clause_count != len(items) or not {"today_work", "tomorrow_plan"}.issubset(fields):
        return None
    return StructuredDailyDocument(items=tuple(items), fields=frozenset(fields))


def _temporal_daily_clause_is_nonassertive(raw: str, body: str) -> bool:
    if re.search(r"[！？!?]", raw):
        return True
    if daily_item_is_unpunctuated_question(body):
        return True
    if re.match(r"^(?:如果|假如|假设|要是|听说|据说)", body):
        return True
    if re.match(r"^\S{1,8}(?:说|称|表示|反馈|提到)", body):
        return True
    return bool(re.search(r"[“”‘’\"]", body))


def _daily_section_item_is_nonassertive(
    field: str,
    value: str,
    *,
    raw: str,
) -> bool:
    """Keep deterministic Daily writes limited to asserted section content.

    A rejected item is not discarded. It falls back to the semantic model so
    cancellation, correction, question, hypothetical, and reported-speech
    meanings can follow their own domain path instead of gaining Daily write
    authority from a section heading alone.
    """

    body = str(value or "").strip()
    if not body or _temporal_daily_clause_is_nonassertive(raw, body):
        return True
    if field == "today_work":
        return bool(
            re.search(
                r"(?:不是|没有|尚未|还没|并未|未曾|未能|没能).{0,12}"
                r"(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交|归档|验收)",
                body,
            )
        )
    if field == "tomorrow_plan":
        nominal_termination_work = daily_item_is_nominal_termination_work(body)
        return bool(
            daily_item_has_termination_state(body)
            or (
                not nominal_termination_work
                and re.search(
                    r"(?:取消|不去|不再|不用|无需|没法|无法|去不了|不做|不处理|算了)",
                    body,
                )
            )
            or re.search(r"不是.{0,12}(?:而是|是)", body)
        )
    return False


def structured_daily_semantic_payload(text: str) -> dict[str, Any] | None:
    document = parse_structured_daily_document(text)
    if document is None:
        return None
    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    for index, item in enumerate(document.items, start=1):
        entity_id = f"structured-daily-event-{index}"
        action_id = f"structured-daily-capture-{index}"
        entity_ids.append(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "daily_event",
                "value": item.value,
                "confidence": 1.0,
                "attributes": {"field": item.field},
            }
        )
        actions.append(
            {
                "action_id": action_id,
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        )
        segments.append(
            {
                "segment_id": f"structured-daily-segment-{index}",
                "text": text[item.start_offset:item.end_offset],
                "intents": ["daily_append"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
            }
        )
    return {
        "intents": ["daily_append"],
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": entity_ids,
            "remember_turn": True,
        },
    }


def explicit_standalone_daily_fact_semantic_payload(
    text: str,
) -> dict[str, Any] | None:
    """Project one explicit current-day work assertion into Daily.

    This is intentionally a language-act contract rather than a topic list. It
    recovers a positive Daily facet when the model calls an otherwise explicit
    first-person fact ``chat`` while still rejecting questions, hypotheses,
    quotations, negated completion, and cancellations. Other model-produced
    domain facets remain intact and continue through normal Admission.
    """

    source = str(text or "").strip()
    if not source or _standalone_today_work_is_nonassertive(source):
        return None
    label_match = _EXPLICIT_TODAY_WORK_LABEL.fullmatch(source)
    if label_match is not None:
        value = str(label_match.group("content") or "").strip(" \t\r\n，,：:。")
    else:
        prefix_match = _STANDALONE_TODAY_WORK_PREFIX.fullmatch(source)
        if prefix_match is None:
            return None
        body = str(prefix_match.group("body") or "").strip(" \t\r\n，,：:。")
        if not body or _STANDALONE_TODAY_WORK_PREDICATE.match(body) is None:
            return None
        value = body
    if not value or re.match(r"^(?:不是|没有|暂无|尚未|还没|未曾|未能|没能|无)$", value):
        return None
    payload = _daily_append_payload(source, value=value, field="today_work")
    payload["entities"][0]["attributes"]["statement_mode"] = "asserted"
    return payload


def _standalone_today_work_is_nonassertive(source: str) -> bool:
    if re.search(r"[？?]", source):
        return True
    if re.search(r"(?:是否|吗|呢|如何|怎么|为什么|有没有|多少|哪些|什么|何时|几点)", source):
        return True
    if re.match(r"^(?:如果|假如|假设|要是|听说|据说)", source):
        return True
    if re.match(r"^\S{1,8}(?:说|称|表示|反馈|提到)", source):
        return True
    if re.search(r"[“”‘’\"']", source):
        return True
    return bool(
        re.search(
            r"(?:没|没有|尚未|还没|未曾|未能|没能|不再|不去|取消|算了)"
            r".{0,8}(?:完成|做完|处理|审核|推进|跟进|整理|制作|参加|提交|归档|验收)",
            source,
        )
    )


def complete_daily_document_replace_semantic_payload(
    text: str,
    resources: dict[str, Any],
    *,
    document: StructuredDailyDocument | None = None,
) -> dict[str, Any] | None:
    """Replace all three explicitly supplied Daily sections as one decision."""

    source = str(text or "")
    document = document or parse_structured_daily_document(source)
    if document is None or set(document.fields) != {
        "today_work",
        "problems",
        "tomorrow_plan",
    }:
        return None
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    report_id = str(draft.get("report_id") or "").strip()
    version = draft.get("version")
    if not report_id or not isinstance(version, int) or isinstance(version, bool):
        return None

    grouped: dict[str, list[StructuredReportItem]] = {}
    for item in document.items:
        grouped.setdefault(item.field, []).append(item)
    if any(not grouped.get(field) for field in document.fields):
        return None

    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    ordered_fields = sorted(grouped, key=lambda field: grouped[field][0].start_offset)
    for index, field in enumerate(ordered_fields, start=1):
        items = grouped[field]
        start = items[0].start_offset
        end = items[-1].end_offset
        entity_id = f"complete-daily-section-{index}"
        action_id = f"complete-daily-replace-{index}"
        entity_ids.append(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "daily_report",
                "value": source[start:end],
                "confidence": 1.0,
                "attributes": {
                    "report_id": report_id,
                    "version": version,
                    "field": field,
                    "items": [item.value for item in items],
                },
            }
        )
        actions.append(
            {
                "action_id": action_id,
                "action_type": "replace_daily_section",
                "intent": "daily_modify",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        )
        segments.append(
            {
                "segment_id": f"complete-daily-segment-{index}",
                "text": source[start:end],
                "intents": ["daily_modify"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": start,
                "end_offset": end,
            }
        )
    return {
        "intents": ["daily_modify"],
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_modify",
            "remember_entity_ids": entity_ids,
            "remember_turn": True,
        },
    }


def daily_section_cue_semantic_payload(
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any] | None:
    """Parse one explicit section replacement without treating missing fields as empty."""

    source = str(text or "").strip()
    match = _DAILY_SECTION_CUE.fullmatch(source)
    if match is None:
        return None
    group_name = "problems" if match.group("problems") is not None else "plan"
    value = str(match.group(group_name) or "").strip(" \t\r\n，,:：")
    if not value:
        return None
    field = "problems" if group_name == "problems" else "tomorrow_plan"
    if _daily_section_item_is_nonassertive(field, value, raw=source):
        return None
    return _daily_section_replace_payload(source, resources, field=field, items=[value])


def _daily_section_replace_payload(
    source: str,
    resources: dict[str, Any],
    *,
    field: str,
    items: list[str],
) -> dict[str, Any] | None:
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    report_id = str(draft.get("report_id") or "").strip()
    version = draft.get("version")
    if not report_id or not isinstance(version, int) or isinstance(version, bool):
        return None
    entity_id = "daily-section-replace-target"
    action_id = "daily-section-replace-action"
    return {
        "intents": ["daily_modify"],
        "segments": [
            {
                "segment_id": "daily-section-replace-segment",
                "text": source,
                "intents": ["daily_modify"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "daily_report",
                "value": source,
                "confidence": 1.0,
                "attributes": {
                    "report_id": report_id,
                    "version": version,
                    "field": field,
                    "items": items,
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "replace_daily_section",
                "intent": "daily_modify",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_modify",
            "remember_entity_ids": [entity_id],
            "remember_turn": True,
        },
    }


def daily_item_section_correction_semantic_payload(
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any] | None:
    """Move an exact trusted item between Daily sections using its stable ID."""

    source = str(text or "").strip().rstrip("。！!")
    match = _DAILY_ITEM_SECTION_CORRECTION.fullmatch(source)
    completion_match = (
        _DAILY_ITEM_COMPLETION_CORRECTION.fullmatch(source)
        if match is None
        else None
    )
    if match is None and completion_match is None:
        return None
    selected_match = match or completion_match
    assert selected_match is not None
    value = str(selected_match.group("value") or "").strip(" ，,。")
    target_field = "today_work"
    if match is not None:
        target_field = (
            "tomorrow_plan"
            if str(match.group("target") or "") in {"明天", "明日"}
            else "today_work"
        )
    source_field = "today_work" if target_field == "tomorrow_plan" else "tomorrow_plan"
    source_matches = [
        item
        for item in _trusted_daily_field_items(resources, source_field)
        if str(item.get("text") or "").strip() == value
    ]
    target_matches = [
        item
        for item in _trusted_daily_field_items(resources, target_field)
        if str(item.get("text") or "").strip() == value
    ]
    if len(source_matches) == 1 and not target_matches:
        item_id = str(source_matches[0].get("item_id") or "").strip()
        if not item_id:
            return _daily_clarification_payload(
                source,
                reason="daily_section_correction_target_unbound",
                question="找到了对应内容，但缺少稳定条目编号；本次没有修改。",
            )
        entity_id = "daily-section-correction-target"
        action_id = "daily-section-correction-action"
        return {
            "intents": ["daily_modify"],
            "segments": [
                {
                    "segment_id": "daily-section-correction-segment",
                    "text": source,
                    "intents": ["daily_modify"],
                    "entity_ids": [entity_id],
                    "action_ids": [action_id],
                    "start_offset": 0,
                    "end_offset": len(source),
                }
            ],
            "entities": [
                {
                    "entity_id": entity_id,
                    "entity_type": "daily_item_target",
                    "value": value,
                    "confidence": 1.0,
                    "attributes": {
                        "source_field": source_field,
                        "target_field": target_field,
                        "target_item_ids": [item_id],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": action_id,
                    "action_type": "move_daily_items",
                    "intent": "daily_modify",
                    "entity_ids": [entity_id],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_report",
                "remember_entity_ids": [entity_id],
                "remember_turn": True,
            },
        }
    if len(target_matches) == 1 and not source_matches:
        return _daily_clarification_payload(
            source,
            reason="daily_item_already_in_target_section",
            question="这项内容已经在目标栏目中，本次没有修改。",
        )
    if source_matches or target_matches:
        return _daily_clarification_payload(
            source,
            reason="daily_section_correction_ambiguous",
            question="当前有多条相同内容，无法唯一确定要调整哪一条；本次没有修改。",
        )
    return _daily_append_payload(source, value=value, field=target_field)


def daily_compound_section_correction_semantic_payload(
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any] | None:
    """Combine one item correction and one explicit section update by segment."""

    source = str(text or "")
    parts = [
        value.strip()
        for value in _DAILY_COMPOUND_SEPARATOR.split(source)
        if value.strip()
    ]
    if len(parts) != 2:
        return None
    correction = daily_item_section_correction_semantic_payload(parts[0], resources)
    section = daily_section_cue_semantic_payload(parts[1], resources)
    if correction is None or section is None:
        return None
    entities = [*(correction.get("entities") or []), *(section.get("entities") or [])]
    actions = [
        *(correction.get("required_actions") or []),
        *(section.get("required_actions") or []),
    ]
    segments: list[dict[str, Any]] = []
    for payload, part in ((correction, parts[0]), (section, parts[1])):
        offset = source.find(part)
        for raw_segment in payload.get("segments") or []:
            segment = dict(raw_segment)
            segment["start_offset"] = offset + int(segment.get("start_offset", 0))
            segment["end_offset"] = offset + int(segment.get("end_offset", len(part)))
            segments.append(segment)
    entity_ids = [str(item.get("entity_id") or "") for item in entities]
    return {
        "intents": list(
            dict.fromkeys(
                [*(correction.get("intents") or []), *(section.get("intents") or [])]
            )
        ),
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": (
            correction.get("clarification_need") or section.get("clarification_need")
        ),
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": [value for value in entity_ids if value],
            "remember_turn": True,
        },
    }


def _daily_append_payload(source: str, *, value: str, field: str) -> dict[str, Any]:
    entity_id = "daily-section-correction-new-item"
    action_id = "daily-section-correction-capture"
    return {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "daily-section-correction-new-segment",
                "text": source,
                "intents": ["daily_append"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "daily_event",
                "value": value,
                "confidence": 1.0,
                "attributes": {"field": field},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": [entity_id],
            "remember_turn": True,
        },
    }


def _trusted_daily_field_items(
    resources: dict[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    items = draft.get("items")
    return [
        dict(item)
        for item in items
        if isinstance(item, dict) and str(item.get("field") or "") == field
    ] if isinstance(items, list) else []


def _daily_clarification_payload(
    source: str,
    *,
    reason: str,
    question: str,
) -> dict[str, Any]:
    return {
        "intents": ["daily_modify"],
        "segments": [
            {
                "segment_id": f"{reason}-segment",
                "text": source,
                "intents": ["daily_modify"],
                "entity_ids": [],
                "action_ids": [],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": {
            "reason": reason,
            "missing_fields": [],
            "question": question,
        },
        "context_update": {
            "current_goal": "daily_report",
            "preserve_current_goal": True,
            "remember_turn": True,
        },
    }


def _parse_section_items(
    source: str,
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> tuple[StructuredReportItem, ...]:
    items: list[StructuredReportItem] = []
    cursor = start_offset
    for separator in _ITEM_SEPARATOR.finditer(source, start_offset, end_offset):
        items.extend(_parse_item(source, field=field, start_offset=cursor, end_offset=separator.start()))
        cursor = separator.end()
    items.extend(_parse_item(source, field=field, start_offset=cursor, end_offset=end_offset))
    return tuple(items)


def _parse_item(
    source: str,
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> tuple[StructuredReportItem, ...]:
    raw = source[start_offset:end_offset]
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw.rstrip())
    local_start = leading
    local_end = trailing
    numbered = _ITEM_NUMBER.match(raw[local_start:local_end])
    if numbered is not None:
        local_start += numbered.end()
    value = raw[local_start:local_end].strip()
    if numbered is None:
        value = value.rstrip("。；;").rstrip()
    if not value:
        return ()
    value_leading = len(raw[local_start:local_end]) - len(raw[local_start:local_end].lstrip())
    absolute_start = start_offset + local_start + value_leading
    absolute_end = absolute_start + len(value)
    return (
        StructuredReportItem(
            field=field,
            value=value,
            start_offset=absolute_start,
            end_offset=absolute_end,
        ),
    )
