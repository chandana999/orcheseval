"""LLM provider contract. Ported from eval-orchestrator (tenacity retry preserved)."""

from typing import Any, Protocol

from tenacity import retry, stop_after_attempt, wait_exponential


class LLMResponse(dict):
    @property
    def output(self) -> str:
        return self["output"]

    @property
    def latency_ms(self) -> float:
        return self["latency_ms"]

    @property
    def prompt_tokens(self) -> int:
        return self.get("prompt_tokens", 0)

    @property
    def completion_tokens(self) -> int:
        return self.get("completion_tokens", 0)

    @property
    def model(self) -> str:
        return self.get("model", "")


class LLMProviderProtocol(Protocol):
    def generate(
        self, prompt: str, model: str, config: dict[str, Any] | None = None
    ) -> LLMResponse: ...


class BaseLLMProvider:
    timeout: float

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def generate(
        self, prompt: str, model: str, config: dict[str, Any] | None = None
    ) -> LLMResponse:
        return self._generate(prompt, model, config or {})

    def _generate(self, prompt: str, model: str, config: dict[str, Any]) -> LLMResponse:
        raise NotImplementedError
