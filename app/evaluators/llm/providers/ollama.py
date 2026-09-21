"""Local Ollama provider. Ported from eval-orchestrator."""

import time
from typing import Any

import httpx

from app.core.config import settings
from app.evaluators.llm.providers.base import BaseLLMProvider, LLMResponse


class OllamaProvider(BaseLLMProvider):
    def _generate(self, prompt: str, model: str, config: dict[str, Any]) -> LLMResponse:
        start = time.perf_counter()
        payload = {
            "model": model or "llama3.2",
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": config.get("temperature", 0.0),
                "num_predict": config.get("max_tokens", 1024),
            },
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        content = (data.get("response") or "").strip()
        return LLMResponse(
            output=content,
            latency_ms=(time.perf_counter() - start) * 1000,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", len(content.split())),
            model=payload["model"],
            provider="ollama",
        )
