from app.core.config import settings
from app.evaluators.llm.providers.anthropic import AnthropicProvider
from app.evaluators.llm.providers.base import LLMProviderProtocol
from app.evaluators.llm.providers.ollama import OllamaProvider
from app.evaluators.llm.providers.openai import OpenAIProvider
from app.services.errors import InvalidEvaluatorConfigError

PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}


def get_llm_provider(provider: str | None = None) -> LLMProviderProtocol:
    name = (provider or settings.default_provider).lower()
    factory = PROVIDERS.get(name)
    if factory is None:
        raise InvalidEvaluatorConfigError(
            f"unsupported LLM provider '{name}'; expected one of {sorted(PROVIDERS)}"
        )
    return factory(timeout=settings.llm_timeout_seconds)
