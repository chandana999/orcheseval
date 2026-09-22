"""OpenAI chat completions provider."""

import time
from typing import Any

import httpx

from app.core.config import settings
from app.evaluators.llm.providers.base import BaseLLMProvider, LLMResponse


class OpenAIProvider(BaseLLMProvider):
    def _generate(self, prompt: str, model: str, config: dict[str, Any]) -> LLMResponse:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        start = time.perf_counter()
        payload = {
            "model": model or settings.default_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": config.get("temperature", 0.0),
            "max_tokens": config.get("max_tokens", 1024),
        }
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        content = (data["choices"][0]["message"]["content"] or "").strip()
        usage = data.get("usage", {})
        return LLMResponse(
            output=content,
            latency_ms=(time.perf_counter() - start) * 1000,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", len(content.split())),
            model=payload["model"],
            provider="openai",
        )
