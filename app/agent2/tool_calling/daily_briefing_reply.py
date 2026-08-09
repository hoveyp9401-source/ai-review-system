from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.agent2.tool_calling.contracts import ReceiptStatus, ToolReceipt

_UNSUPPORTED_CAUSAL_WORDING = (
    re.compile(
        r"(?:可能|也许|或许|大概|推测|猜测|会不会|是不是|或者|或是)"
        r".{0,80}(?:原因|统计|界面|渠道|版本|转发|误解|偏差|顺序|"
        r"先后|延迟|故障|抓取|看到|显示)"
    ),
    re.compile(
        r"(?:说明|表明).{0,80}(?:晨报生成时|生成时|当时)"
        r".{0,80}(?:已提交|未提交|提交状态)"
    ),
    re.compile(r"(?:原因是|是因为|由于|导致)"),
)
_UNSUPPORTED_LEGACY_SUBMISSION_CLASSIFICATION = (
    re.compile(r"(?:列为|列入|归入).{0,8}(?:已交|已提交)"),
    re.compile(r"(?:已交|已提交)(?:人数|名单).{0,12}(?:包含|包括)"),
)


@dataclass(frozen=True)
class DailyBriefingReplyEnvelope:
    reply: str
    premise_status: str
    recorded_evidence_quotes: tuple[str, ...]
    acknowledged_evidence_limits: tuple[str, ...]
    causal_conclusion: str | None


def daily_briefing_composer_messages(
    *,
    user_question: str,
    receipts: tuple[ToolReceipt, ...],
) -> list[dict[str, object]]:
    return [
        {
            "role": "system",
            "content": (
                "You are Agent2's evidence-bound daily-briefing answer composer. "
                "The tool phase is already complete. Do not call or imitate tools. "
                "Answer the user's actual question in natural Chinese, using only "
                "the supplied safe_user_facts. Keep recorded outbound text, the "
                "at-generation snapshot, current report state, and delivery evidence "
                "separate. A later state is not evidence of an earlier cause. When "
                "cause is null or evidence_limits is non-empty, do not offer any "
                "possible, likely, alternative, timing, version, forwarding, system, "
                "or misunderstanding explanation. Before writing, inspect the literal "
                "recorded message and decide whether it supports, contradicts, or cannot "
                "determine the premise in the user's question. Put that decision in "
                "premise_status and copy short exact supporting clauses into "
                "recorded_evidence_quotes. Never accept the user's premise without "
                "checking the recorded text. Absence from a recorded missing list "
                "proves only that the person was not shown as missing; it does not "
                "prove the person was listed as submitted or had already submitted. "
                "Claim a person was listed as submitted only when the literal recorded "
                "message names that person in an explicit submitted section. Return "
                "only the required JSON object. 重要：没有生成时成员快照时，未交名单中"
                "没有某人，只能回答晨报原文没有把他列为未交；禁止回答他已被列为已交、"
                "已交人数包含他或当时已经提交。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_question": user_question,
                    "safe_tool_results": [
                        {
                            "tool_name": receipt.tool_name,
                            "safe_user_facts": receipt.safe_user_facts,
                        }
                        for receipt in receipts
                        if not receipt.changed
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
        {
            "role": "system",
            "content": json.dumps(
                {
                    "canary_turn_protocol": {
                        "briefing_fact_batch_closed": True,
                        "execution_mode": "canary_execute",
                        "final_response_required": True,
                        "final_response_source": "safe_user_facts_only",
                        "terminal_response_contract": (
                            daily_briefing_reply_protocol(receipts)
                        ),
                        "further_tool_calls_allowed": False,
                        "native_or_textual_tool_calls_allowed": False,
                        "write_batch_closed": False,
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def daily_briefing_reply_protocol(
    receipts: tuple[ToolReceipt, ...],
) -> dict[str, object]:
    limits, cause_available = _briefing_constraints(receipts)
    return {
        "format": "json_object",
        "exact_fields": [
            "reply",
            "premise_status",
            "recorded_evidence_quotes",
            "acknowledged_evidence_limits",
            "causal_conclusion",
        ],
        "reply": (
            "natural Chinese answer using only the returned briefing facts; "
            "do not restate or paraphrase the required evidence limits because "
            "the server appends their exact text after the model reply"
        ),
        "premise_status": (
            "one of supported, contradicted, not_determinable, or not_applicable; "
            "use contradicted when the recorded copy directly shows the user's "
            "premise did not occur"
        ),
        "recorded_evidence_quotes": (
            "one exact string or an array of short exact strings copied from "
            "recorded_briefings[].message_text; "
            "required for supported or contradicted"
        ),
        "acknowledged_evidence_limits": list(limits),
        "causal_conclusion": (
            "a concise conclusion supported by returned cause evidence, or null"
            if cause_available
            else None
        ),
        "rules": [
            "Keep recorded outbound text, at-generation snapshot, current report state, and delivery evidence separate.",
            "Do not infer event order from equal or nearby timestamps.",
            "Do not turn absence from a missing list into a claim that the person was listed as submitted.",
            "Do not offer possible, likely, hypothetical, scheduling, delay, system-fault, or user-misunderstanding causes when causal_conclusion must be null.",
            "Do not call another tool after this briefing-fact batch.",
        ],
    }


def validate_daily_briefing_reply(
    content: str,
    receipts: tuple[ToolReceipt, ...],
) -> tuple[DailyBriefingReplyEnvelope | None, tuple[str, ...]]:
    limits, cause_available = _briefing_constraints(receipts)
    if not _briefing_fact_receipts(receipts):
        return None, ()
    try:
        payload = json.loads(str(content or ""))
    except (json.JSONDecodeError, TypeError):
        return None, ("reply_not_json_object",)
    if not isinstance(payload, dict):
        return None, ("reply_not_json_object",)
    expected_fields = {
        "reply",
        "premise_status",
        "recorded_evidence_quotes",
        "acknowledged_evidence_limits",
        "causal_conclusion",
    }
    errors: list[str] = []
    if set(payload) != expected_fields:
        errors.append("wrong_reply_fields")
    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        errors.append("empty_reply")
        reply = ""
    premise_status = str(payload.get("premise_status") or "").strip()
    if premise_status not in {
        "supported",
        "contradicted",
        "not_determinable",
        "not_applicable",
    }:
        errors.append("invalid_premise_status")
    raw_quotes = payload.get("recorded_evidence_quotes")
    if isinstance(raw_quotes, str):
        quote_values = (raw_quotes,)
    elif isinstance(raw_quotes, list) and not any(
        not isinstance(item, str) for item in raw_quotes
    ):
        quote_values = tuple(raw_quotes)
    else:
        errors.append("invalid_recorded_evidence_quotes")
        quote_values = ()
    evidence_quotes = tuple(
        dict.fromkeys(item.strip() for item in quote_values if item.strip())
    )
    recorded_texts = _recorded_message_texts(receipts)
    if premise_status in {"supported", "contradicted"} and not evidence_quotes:
        errors.append("missing_recorded_evidence_quote")
    if any(
        len(quote) > 240
        or not any(quote in recorded_text for recorded_text in recorded_texts)
        for quote in evidence_quotes
    ):
        errors.append("unbound_recorded_evidence_quote")
    acknowledged = payload.get("acknowledged_evidence_limits")
    if not isinstance(acknowledged, list) or any(
        not isinstance(item, str) for item in acknowledged
    ):
        errors.append("invalid_evidence_limits")
        acknowledged_limits: tuple[str, ...] = ()
    else:
        acknowledged_limits = tuple(acknowledged)
    if acknowledged_limits != limits:
        errors.append("evidence_limits_mismatch")
    causal_conclusion = payload.get("causal_conclusion")
    if causal_conclusion is not None and not isinstance(causal_conclusion, str):
        errors.append("invalid_causal_conclusion")
        normalized_cause = None
    else:
        normalized_cause = (
            causal_conclusion.strip()
            if isinstance(causal_conclusion, str)
            else None
        )
    if not cause_available and normalized_cause:
        errors.append("unsupported_causal_conclusion")
    if not cause_available and any(
        pattern.search(reply) for pattern in _UNSUPPORTED_CAUSAL_WORDING
    ):
        errors.append("unsupported_causal_wording")
    if not _snapshot_supports_submitted_classification(receipts) and any(
        pattern.search(reply)
        for pattern in _UNSUPPORTED_LEGACY_SUBMISSION_CLASSIFICATION
    ):
        errors.append("unsupported_legacy_submission_classification")
    if errors:
        return None, tuple(dict.fromkeys(errors))
    return (
        DailyBriefingReplyEnvelope(
            reply=reply.strip(),
            premise_status=premise_status,
            recorded_evidence_quotes=evidence_quotes,
            acknowledged_evidence_limits=acknowledged_limits,
            causal_conclusion=normalized_cause,
        ),
        (),
    )


def render_daily_briefing_reply(
    envelope: DailyBriefingReplyEnvelope,
) -> str:
    premise_intro = {
        "supported": "系统保存的晨报原文支持你描述的情况。",
        "contradicted": "系统保存的这份晨报原文与问题中的情况不一致。",
        "not_determinable": "仅凭系统保存的晨报原文，无法确认问题中的情况。",
        "not_applicable": "",
    }.get(envelope.premise_status, "")
    sections = [premise_intro, envelope.reply.strip()]
    if envelope.recorded_evidence_quotes:
        sections.append(
            "系统保存的晨报原文片段：\n"
            + "\n".join(
                f"“{quote}”" for quote in envelope.recorded_evidence_quotes
            )
        )
    sections.extend(envelope.acknowledged_evidence_limits)
    return "\n\n".join(section for section in sections if section)


def daily_briefing_reply_retry_instruction(
    errors: Iterable[str],
    receipts: tuple[ToolReceipt, ...],
    *,
    retry_number: int,
) -> str:
    return json.dumps(
        {
            "daily_briefing_reply_retry": {
                "instruction_cn": (
                    "晨报事实查询已经结束，禁止再次调用任何工具。上一版答复没有满足证据约束。"
                    "请重新读取本轮已经返回的工具事实，只输出符合 terminal_response_contract 的 JSON。"
                    "先逐字核对 recorded_briefings[].message_text，再填写 premise_status；"
                    "如果判断为 supported 或 contradicted，recorded_evidence_quotes 必须逐字复制"
                    "晨报原文中的短句，不得改写。"
                    "acknowledged_evidence_limits 必须逐字复制契约中的列表；reply 不要复述或改写这些"
                    "限制，服务器会把原文追加给用户。cause 没有证据时，causal_conclusion 必须为 null，"
                    "reply 中也不得补充任何可能原因、时序猜测、系统故障猜测或用户误解推测。"
                    "原文未交名单中没有某人，只能说明原文未把他列为未交；除非原文的已交部分"
                    "明确点名且生成快照支持，否则不得说他被列入已交名单、已交人数包含他或"
                    "当时已经提交。"
                ),
                "validation_errors": list(errors),
                "retry_number": retry_number,
                "minimal_reply_rule": (
                    "reply 只陈述系统保存的晨报原文直接显示了什么；如果记录与用户描述不一致，"
                    "最多请用户提供他看到的准确原文。不要解释差异为何发生，也不要从当前状态"
                    "推断生成时状态。"
                ),
                "terminal_response_contract": daily_briefing_reply_protocol(
                    receipts
                ),
                "further_tool_calls_allowed": False,
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _briefing_constraints(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[tuple[str, ...], bool]:
    limits: list[str] = []
    cause_available = False
    for receipt in _briefing_fact_receipts(receipts):
        facts = receipt.safe_user_facts.get("daily_briefing_facts")
        if not isinstance(facts, dict):
            continue
        for value in facts.get("evidence_limits", []):
            limit = str(value or "").strip()
            if limit and limit not in limits:
                limits.append(limit)
        if facts.get("cause") is not None:
            cause_available = True
    return tuple(limits), cause_available


def _recorded_message_texts(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[str, ...]:
    texts: list[str] = []
    for receipt in _briefing_fact_receipts(receipts):
        facts = receipt.safe_user_facts.get("daily_briefing_facts")
        if not isinstance(facts, dict):
            continue
        for event in facts.get("recorded_briefings", []):
            if not isinstance(event, dict):
                continue
            text = str(event.get("message_text") or "")
            if text and text not in texts:
                texts.append(text)
    return tuple(texts)


def _snapshot_supports_submitted_classification(
    receipts: tuple[ToolReceipt, ...],
) -> bool:
    for receipt in _briefing_fact_receipts(receipts):
        facts = receipt.safe_user_facts.get("daily_briefing_facts")
        if not isinstance(facts, dict):
            continue
        for event in facts.get("recorded_briefings", []):
            if not isinstance(event, dict):
                continue
            member = event.get("member_at_snapshot")
            if isinstance(member, dict) and member.get("classification") in {
                "submitted",
                "pending_confirmation",
            }:
                return True
    return False


def _briefing_fact_receipts(
    receipts: tuple[ToolReceipt, ...],
) -> tuple[ToolReceipt, ...]:
    return tuple(
        receipt
        for receipt in receipts
        if receipt.tool_name == "query_daily_briefing_facts"
        and receipt.status in {ReceiptStatus.SUCCESS, ReceiptStatus.NO_OP}
        and not receipt.changed
    )
