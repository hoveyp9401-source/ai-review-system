from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agent.action_plan import ActionPlan
from app.llm.client import LLMClient, LLMTimeoutError
from app.llm.extractor import LLMOutputError
from app.utils.json import extract_json_object


PROMPT_PATH = Path(__file__).parent / "prompts" / "report_agent.md"


@dataclass(frozen=True)
class AgentDecisionResult:
    payload: ActionPlan
    meta: dict[str, Any]


class ReportAgent:
    def __init__(self, client: LLMClient):
        self.client = client
        self.prompt_template = PROMPT_PATH.read_text(encoding="utf-8")

    async def decide_with_meta(self, *, raw_input: str, context: dict[str, Any]) -> AgentDecisionResult:
        settings = self.client.settings
        model = getattr(settings, "report_agent_model", getattr(settings, "llm_draft_decision_model", settings.llm_model))
        thinking = bool(getattr(settings, "report_agent_thinking", False))
        timeout_seconds = float(getattr(settings, "report_agent_timeout_seconds", 10.0))
        max_retries = int(getattr(settings, "report_agent_max_retries", 0))
        meta = {
            "model": model,
            "thinking": thinking,
            "timeout": False,
        }
        prompt = (
            self.prompt_template
            .replace("{{context_json}}", json.dumps(context, ensure_ascii=False))
            .replace("{{raw_input}}", raw_input.strip())
        )
        try:
            output = await self.client.complete_json(
                system_prompt="You are a DingTalk legal daily-report agent. Return strict JSON only.",
                user_prompt=prompt,
                model=model,
                thinking_enabled=thinking,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
            data = extract_json_object(output)
            return AgentDecisionResult(payload=ActionPlan.model_validate(data), meta=meta)
        except LLMTimeoutError as exc:
            meta["timeout"] = True
            raise LLMOutputError("ReportAgent decision timed out.", meta=meta) from exc
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"Invalid ReportAgent JSON: {exc}", meta=meta) from exc

