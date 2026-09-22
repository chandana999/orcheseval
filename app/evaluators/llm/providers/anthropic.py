"""Anthropic messages provider."""

import time
from typing import Any

import httpx

from app.core.config import settings
from app.evaluators.llm.providers.base import BaseLLMProvider, LLMResponse


class AnthropicProvider(BaseLLMProvider):
    def _generate(self, prompt: str, model: str, config: dict[str, Any]) -> LLMResponse:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")

        start = time.perf_counter()
        payload = {
            "model": model or "claude-3-5-haiku-20241022",
            "max_tokens": config.get("max_tokens", 1024),
            "temperature": config.get("temperature", 0.0),
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        content = data["content"][0]["text"].strip()
        usage = data.get("usage", {})
        return LLMResponse(
            output=content,
            latency_ms=(time.perf_counter() - start) * 1000,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", len(content.split())),
            model=payload["model"],
            provider="anthropic",
        )
