from evalorch.core.config import settings
from evalorch.evaluators.llm.providers.base import LLMProviderProtocol
from evalorch.evaluators.llm.providers.openai import OpenAIProvider
from evalorch.services.errors import InvalidEvaluatorConfigError

PROVIDERS = {
    "openai": OpenAIProvider,
}


def get_llm_provider(provider: str | None = None) -> LLMProviderProtocol:
    name = (provider or settings.default_provider).lower()
    factory = PROVIDERS.get(name)
    if factory is None:
        raise InvalidEvaluatorConfigError(
            f"unsupported LLM provider '{name}'; expected one of {sorted(PROVIDERS)}"
        )
    return factory(timeout=settings.llm_timeout_seconds)
