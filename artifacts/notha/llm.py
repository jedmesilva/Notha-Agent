"""
LLM provider factory.

Selects and returns a singleton of the provider configured via LLM_PROVIDER.
Agents import get_provider() from here — they never instantiate providers directly.

Available providers:
  openai    (default) — requires OPENAI_API_KEY or AI_INTEGRATIONS_OPENAI_*
  anthropic           — requires ANTHROPIC_API_KEY

Configurable models via environment variables:
  OPENAI_MODEL    (default: gpt-4o-mini)
  ANTHROPIC_MODEL (default: claude-3-5-haiku-latest)
"""
import logging
import os
from providers.base import LLMProvider

logger = logging.getLogger("notha.llm")

_instances: dict[str, LLMProvider] = {}


def get_provider(provider: str | None = None) -> LLMProvider:
    """Returns the singleton for the specified provider (or the one set in LLM_PROVIDER)."""
    name = (provider or os.environ.get("LLM_PROVIDER", "openai")).lower()

    if name not in _instances:
        if name == "openai":
            from providers.openai_provider import OpenAIProvider
            _instances[name] = OpenAIProvider()
            logger.info("LLM provider initialized: OpenAI (model=%s)",
                        os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))

        elif name == "anthropic":
            from providers.anthropic_provider import AnthropicProvider
            _instances[name] = AnthropicProvider()
            logger.info("LLM provider initialized: Anthropic (model=%s)",
                        os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"))

        else:
            raise RuntimeError(
                f"Provider '{name}' not recognized. Valid options: openai, anthropic."
            )

    return _instances[name]
