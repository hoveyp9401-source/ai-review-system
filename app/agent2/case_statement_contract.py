from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


CaseFactKind = Literal[
    "timed_action",
    "completed_work",
    "court_feedback",
    "readiness",
    "lifecycle_transition",
    "case_event",
    "performance",
    "case_outcome",
    "strategy_decision",
    "unknown",
]


@dataclass(frozen=True)
class CaseStatementAssessment:
    """Safety assessment for a statement after one authorized Case is resolved."""

    asserted: bool
    fact_kind: CaseFactKind
    reason_code: str


_NON_ASSERTIVE_MARKERS = (
    "只是举例",
    "举个例子",
    "例如",
    "比如",
    "假设",
    "假如",
    "如果",
    "不要记录",
    "不用记录",
    "别记录",
    "不要写入",
    "别写入",
)

_PURE_NO_RISK = re.compile(
    r"(?:目前|现在)?(?:没|没有|暂无)(?:其他|别的)?(?:风险|问题)(?:了|呢|哈)?[。.!！]?"
)
_QUESTION = re.compile(
    r"(?:[？?]|什么时候|哪天|几号|怎么|如何|什么材料|是否|能否|能不能|要不要|多少|谁负责)"
)
_CASE_WORK_VERB_PATTERN = (
    r"(?:联系|沟通|对接|核对|核实|确认|查找|寻找|找|"
    r"协调|委托|调取|走访|调查|收集|整理|起草|审阅|修改|"
    r"准备|提交|补充|申请|催办|跟进|推进|参加|参与|"
    r"调解|开庭|庭审|查控|立案|执行|履行|审结|结案|"
    r"回款|付款|支付|送达|上诉|撤诉|和解)"
)
_ASSISTANT_SERVICE_REQUEST = re.compile(
    r"(?:请你?|麻烦|帮我|帮忙|给我|能否|能不能|可以帮我)"
    r".{0,10}"
    + _CASE_WORK_VERB_PATTERN
)
_TIMED_ACTION = re.compile(
    r"(?:正在|已经|已|今天|今日|刚刚|刚|目前|现已|明天|明日|后天|预计|"
    r"计划|准备|将于|定于|本周|下周[一二三四五六日天]?|周[一二三四五六日天]|"
    r"本月|下月|\d{1,2}月\d{1,2}日|\d+天后)"
    r".{0,20}"
    + _CASE_WORK_VERB_PATTERN
)
_COMPLETED_WORK = re.compile(
    _CASE_WORK_VERB_PATTERN
    + r".{0,12}(?:了|过|完成|完|好了|结束)"
)
_COURT_FEEDBACK = re.compile(
    r"(?:法院|法官|书记员|仲裁委|仲裁员|执行局|对方|对方律师)"
    r".{0,16}"
    r"(?:通知|表示|反馈|回复|告知|确认|安排|要求|决定|同意|拒绝|让|嘱咐|建议|提出)"
)
_READINESS = re.compile(
    r"(?:材料|证据|答辩状|代理词|授权手续|出庭人员|财产线索|执行申请|保全材料)"
    r".{0,16}"
    r"(?:准备|完成|提交|补充|整理|缺少|齐全|还没|尚未|未)"
    r"|(?:还没|尚未|未).{0,10}(?:准备|完成|提交|补充|整理)"
)
_LIFECYCLE_TRANSITION = re.compile(
    r"(?:进入|转入|转为|恢复|移送|变更为)"
    r".{0,10}"
    r"(?:拟诉|诉讼|受理|开庭|审结|执行|履行|上诉|结案|终本)"
)
_CASE_EVENT = re.compile(
    r"(?:收到|取得|拿到|签收|完成|达成|签署|申请|提交)"
    r".{0,14}"
    r"(?:传票|裁判文书|判决|裁定|调解书|和解|立案|执行|保全|查封|冻结|鉴定|送达)"
    r"|(?:已|已经|尚未|暂未|还没|未)"
    r".{0,12}"
    r"(?:受理|立案|开庭|庭审|查控|送达|判决|裁定|执行|履行|审结|结案|终本|"
    r"恢复执行|保全|和解|调解|上诉|撤诉)"
)
_PERFORMANCE = re.compile(
    r"(?:对方|被执行人|债务人|项目公司)"
    r".{0,16}"
    r"(?:履行|付款|支付|回款|分期|和解|拒付|未付|欠付)"
)
_CASE_OUTCOME = re.compile(
    r"(?:判决|裁决|裁定|庭审|开庭|调解|和解|查控|保全|执行|立案|送达|鉴定|上诉|撤诉|受理)"
    r".{0,20}"
    r"(?:支持|驳回|胜诉|败诉|结束|完成|成功|失败|未果|没谈成|未谈成|延期|取消|"
    r"中止|终止|撤回|生效|达成|未发现|没有发现|无可执行财产)"
)
_STRATEGY_DECISION = re.compile(
    r"(?:评估.{0,18}(?:结束|完成|暂时不诉|暂缓诉讼|继续诉讼|提起诉讼|重新评估)|"
    r"(?:暂时|暂缓|先)?不诉|暂缓诉讼|"
    r"(?:等|等待).{0,14}(?:谈判|协商).{0,14}(?:结果|重新评估)|"
    r"(?:一周后|下周|\d+天后).{0,14}重新评估)"
)


def assess_case_progress_statement(text: str) -> CaseStatementAssessment:
    compact = re.sub(r"[\s，。！、,.!]", "", str(text or "")).casefold()
    if not compact:
        return CaseStatementAssessment(False, "unknown", "empty_statement")
    if any(marker in compact for marker in _NON_ASSERTIVE_MARKERS):
        return CaseStatementAssessment(False, "unknown", "instruction_or_hypothetical")
    if _PURE_NO_RISK.fullmatch(compact):
        return CaseStatementAssessment(False, "unknown", "pure_risk_acknowledgement")
    if _ASSISTANT_SERVICE_REQUEST.search(compact):
        return CaseStatementAssessment(False, "unknown", "assistant_service_request")
    if _QUESTION.search(compact):
        return CaseStatementAssessment(False, "unknown", "question_not_assertion")

    for kind, pattern in (
        ("timed_action", _TIMED_ACTION),
        ("completed_work", _COMPLETED_WORK),
        ("court_feedback", _COURT_FEEDBACK),
        ("readiness", _READINESS),
        ("lifecycle_transition", _LIFECYCLE_TRANSITION),
        ("case_event", _CASE_EVENT),
        ("performance", _PERFORMANCE),
        ("case_outcome", _CASE_OUTCOME),
        ("strategy_decision", _STRATEGY_DECISION),
    ):
        if pattern.search(compact):
            return CaseStatementAssessment(True, kind, "asserted_case_fact")
    return CaseStatementAssessment(False, "unknown", "no_case_fact_shape")


def is_nonassertive_case_progress(text: str) -> bool:
    return assess_case_progress_statement(text).reason_code in {
        "empty_statement",
        "instruction_or_hypothetical",
        "pure_risk_acknowledgement",
        "assistant_service_request",
        "question_not_assertion",
    }
