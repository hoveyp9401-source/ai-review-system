from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.llm.client import LLMClient, LLMTimeoutError
from app.schemas import DailyInputIntentDecision, DraftDecision, StructuredDailyReport, TeamSummaryPayload
from app.utils.json import extract_json_object

PROMPT_DIR = Path(__file__).parent / "prompts"


class LLMOutputError(ValueError):
    def __init__(self, message: str, *, meta: dict[str, Any] | None = None):
        super().__init__(message)
        self.meta = meta or {}


@dataclass(frozen=True)
class LLMCallResult:
    payload: object
    meta: dict[str, Any]


class DailyReportExtractor:
    def __init__(self, client: LLMClient):
        self.client = client
        self.prompt_template = (PROMPT_DIR / "daily_extract.md").read_text(encoding="utf-8")
        self.intent_prompt_template = (PROMPT_DIR / "daily_intent_decision.md").read_text(encoding="utf-8")
        self.draft_decision_prompt_template = (PROMPT_DIR / "draft_decision.md").read_text(encoding="utf-8")

    async def extract(self, raw_input: str) -> StructuredDailyReport:
        result = await self.extract_with_meta(raw_input)
        return result.payload  # type: ignore[return-value]

    async def extract_with_meta(
        self,
        raw_input: str,
        *,
        allow_fallback_to_pro: bool = True,
        context: dict | None = None,
    ) -> LLMCallResult:
        prompt = (
            self.prompt_template
            .replace("{{raw_input}}", raw_input.strip())
            .replace("{{context_json}}", json.dumps(context or {}, ensure_ascii=False))
        )
        meta = _meta(
            model=self.client.settings.llm_extract_model,
            thinking=self.client.settings.llm_extract_thinking,
        )
        try:
            output = await self.client.complete_json(
                system_prompt="Extract a daily report as strict JSON only.",
                user_prompt=prompt,
                model=self.client.settings.llm_extract_model,
                thinking_enabled=self.client.settings.llm_extract_thinking,
                timeout_seconds=self.client.settings.llm_extract_timeout_seconds,
                max_retries=self.client.settings.llm_extract_max_retries,
            )
            return LLMCallResult(payload=_parse_daily_report(output), meta=meta)
        except LLMTimeoutError as exc:
            meta["timeout"] = True
            raise LLMOutputError("Daily report extraction timed out.", meta=meta) from exc
        except (ValidationError, ValueError) as exc:
            if not allow_fallback_to_pro:
                raise LLMOutputError(f"Invalid daily report JSON: {exc}", meta=meta) from exc
            return await self._extract_with_pro_fallback(prompt, meta, reason="invalid_json")

    async def _extract_with_pro_fallback(self, prompt: str, meta: dict[str, Any], *, reason: str) -> LLMCallResult:
        fallback_meta = _fallback_meta(
            meta,
            model=self.client.settings.llm_high_risk_model,
            thinking=self.client.settings.llm_high_risk_thinking,
            reason=reason,
        )
        try:
            output = await self.client.complete_json(
                system_prompt="Extract a daily report as strict JSON only.",
                user_prompt=prompt,
                model=self.client.settings.llm_high_risk_model,
                thinking_enabled=self.client.settings.llm_high_risk_thinking,
                timeout_seconds=self.client.settings.llm_high_risk_timeout_seconds,
                max_retries=self.client.settings.llm_high_risk_max_retries,
            )
            return LLMCallResult(payload=_parse_daily_report(output), meta=fallback_meta)
        except LLMTimeoutError as exc:
            fallback_meta["timeout"] = True
            raise LLMOutputError("Daily report extraction fallback timed out.", meta=fallback_meta) from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"Invalid fallback daily report JSON: {exc}", meta=fallback_meta) from exc

    async def decide_intent(self, *, raw_input: str, context: dict) -> DailyInputIntentDecision:
        result = await self.decide_intent_with_meta(raw_input=raw_input, context=context)
        return result.payload  # type: ignore[return-value]

    async def decide_intent_with_meta(self, *, raw_input: str, context: dict) -> LLMCallResult:
        prompt = (
            self.intent_prompt_template
            .replace("{{raw_input}}", raw_input.strip())
            .replace("{{context_json}}", json.dumps(context, ensure_ascii=False))
        )
        meta = _meta(
            model=self.client.settings.llm_intent_model,
            thinking=self.client.settings.llm_intent_thinking,
        )
        try:
            output = await self.client.complete_json(
                system_prompt="You classify user intent for a daily-review workflow. Return strict JSON only.",
                user_prompt=prompt,
                model=self.client.settings.llm_intent_model,
                thinking_enabled=self.client.settings.llm_intent_thinking,
                timeout_seconds=self.client.settings.llm_intent_timeout_seconds,
                max_retries=self.client.settings.llm_intent_max_retries,
            )
            decision = _parse_intent(output)
        except LLMTimeoutError as exc:
            meta["timeout"] = True
            raise LLMOutputError("Daily intent decision timed out.", meta=meta) from exc
        except (ValidationError, ValueError) as exc:
            if _is_high_risk_context(context):
                raise LLMOutputError(f"Invalid daily intent JSON: {exc}", meta=meta) from exc
            return await self._decide_intent_with_pro_fallback(prompt, meta, reason="invalid_json")

        if _should_fallback_intent(decision, context, self.client.settings.llm_intent_fallback_confidence):
            return await self._decide_intent_with_pro_fallback(prompt, meta, reason="low_confidence")

        return LLMCallResult(payload=decision, meta=meta)

    async def _decide_intent_with_pro_fallback(self, prompt: str, meta: dict[str, Any], *, reason: str) -> LLMCallResult:
        fallback_meta = _fallback_meta(
            meta,
            model=self.client.settings.llm_high_risk_model,
            thinking=self.client.settings.llm_high_risk_thinking,
            reason=reason,
        )
        try:
            output = await self.client.complete_json(
                system_prompt="You classify user intent for a daily-review workflow. Return strict JSON only.",
                user_prompt=prompt,
                model=self.client.settings.llm_high_risk_model,
                thinking_enabled=self.client.settings.llm_high_risk_thinking,
                timeout_seconds=self.client.settings.llm_high_risk_timeout_seconds,
                max_retries=self.client.settings.llm_high_risk_max_retries,
            )
            return LLMCallResult(payload=_parse_intent(output), meta=fallback_meta)
        except LLMTimeoutError as exc:
            fallback_meta["timeout"] = True
            raise LLMOutputError("Daily intent fallback timed out.", meta=fallback_meta) from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"Invalid fallback daily intent JSON: {exc}", meta=fallback_meta) from exc

    async def decide_draft(self, *, raw_input: str, context: dict) -> DraftDecision:
        result = await self.decide_draft_with_meta(raw_input=raw_input, context=context)
        return result.payload  # type: ignore[return-value]

    async def decide_draft_with_meta(self, *, raw_input: str, context: dict) -> LLMCallResult:
        prompt = (
            self.draft_decision_prompt_template
            .replace("{{raw_input}}", raw_input.strip())
            .replace("{{context_json}}", json.dumps(context, ensure_ascii=False))
        )
        settings = self.client.settings
        model = getattr(settings, "llm_draft_decision_model", settings.llm_intent_model)
        input_structure = context.get("input_structure") if isinstance(context, dict) else {}
        needs_slow_reasoning = bool(input_structure.get("needs_slow_reasoning")) if isinstance(input_structure, dict) else False
        thinking = bool(getattr(settings, "llm_draft_decision_thinking", settings.llm_intent_thinking)) or needs_slow_reasoning
        timeout_seconds = float(getattr(settings, "llm_draft_decision_timeout_seconds", settings.llm_intent_timeout_seconds))
        if needs_slow_reasoning:
            timeout_seconds = max(timeout_seconds, 20.0)
        max_retries = int(getattr(settings, "llm_draft_decision_max_retries", settings.llm_intent_max_retries))
        meta = _meta(model=model, thinking=thinking)
        try:
            output = await self.client.complete_json(
                system_prompt="Decide how to handle a DingTalk daily-review message. Return strict JSON only.",
                user_prompt=prompt,
                model=model,
                thinking_enabled=thinking,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
            return LLMCallResult(payload=_parse_draft_decision(output), meta=meta)
        except LLMTimeoutError as exc:
            meta["timeout"] = True
            raise LLMOutputError("Draft decision timed out.", meta=meta) from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"Invalid draft decision JSON: {exc}", meta=meta) from exc


class TeamSummaryGenerator:
    def __init__(self, client: LLMClient):
        self.client = client
        self.prompt_template = (PROMPT_DIR / "team_summary.md").read_text(encoding="utf-8")

    async def generate(self, reports: list[dict]) -> TeamSummaryPayload:
        reports_json = json.dumps(reports, ensure_ascii=False)
        prompt = self.prompt_template.replace("{{reports_json}}", reports_json)
        output = await self.client.complete_json(
            system_prompt="Generate a daily report summary as strict JSON only.",
            user_prompt=prompt,
            model=self.client.settings.llm_summary_model,
            thinking_enabled=self.client.settings.llm_summary_thinking,
            timeout_seconds=self.client.settings.llm_summary_timeout_seconds,
            max_retries=self.client.settings.llm_summary_max_retries,
        )
        try:
            data = extract_json_object(output)
            return TeamSummaryPayload.model_validate(data)
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"Invalid team summary JSON: {exc}") from exc


def _parse_daily_report(output: str) -> StructuredDailyReport:
    data = extract_json_object(output)
    return StructuredDailyReport.model_validate(data)


def _parse_intent(output: str) -> DailyInputIntentDecision:
    data = extract_json_object(output)
    return DailyInputIntentDecision.model_validate(data)


def _parse_draft_decision(output: str) -> DraftDecision:
    data = extract_json_object(output)
    return DraftDecision.model_validate(data)


def _meta(*, model: str, thinking: bool) -> dict[str, Any]:
    return {
        "model": model,
        "thinking": thinking,
        "timeout": False,
        "fallback_to_pro": False,
        "fallback_reason": "",
    }


def _fallback_meta(meta: dict[str, Any], *, model: str, thinking: bool, reason: str) -> dict[str, Any]:
    fallback_meta = dict(meta)
    fallback_meta.update(
        {
            "model": model,
            "thinking": thinking,
            "fallback_to_pro": True,
            "fallback_reason": reason,
            "timeout": False,
        }
    )
    return fallback_meta


def _is_high_risk_context(context: dict) -> bool:
    return bool(context.get("completed") or context.get("pending_confirmation"))


def _should_fallback_intent(decision: DailyInputIntentDecision, context: dict, confidence_threshold: float) -> bool:
    if decision.should_update_report is False:
        return False
    if decision.message_kind in {"ambiguous", "non_report_interaction"}:
        return False
    if float(decision.confidence) < confidence_threshold and not _is_high_risk_context(context):
        return True
    return False
