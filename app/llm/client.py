from __future__ import annotations

from typing import Any

import httpx
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from app.config import Settings


class LLMTimeoutError(TimeoutError):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.llm_model
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def native_http_client(self) -> httpx.AsyncClient:
        """Shared authenticated transport for independent native Tool Calling."""

        return self._client

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        thinking_enabled: bool | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if thinking_enabled is not None:
            payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        attempts = max(1, (self.settings.llm_max_retries if max_retries is None else max_retries) + 1)
        timeout = self.settings.llm_timeout_seconds if timeout_seconds is None else timeout_seconds
        async for attempt in AsyncRetrying(
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            stop=stop_after_attempt(attempts),
            reraise=True,
        ):
            with attempt:
                try:
                    response = await self._client.post("/chat/completions", json=payload, timeout=timeout)
                except httpx.TimeoutException as exc:
                    raise LLMTimeoutError(f"LLM request timed out after {timeout}s") from exc
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
        raise RuntimeError("LLM request retry loop exited unexpectedly.")
