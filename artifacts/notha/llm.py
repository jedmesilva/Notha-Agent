"""
Fábrica de providers de LLM.

Seleciona e devolve um singleton do provider configurado via LLM_PROVIDER.
Os agentes importam get_provider() daqui — nunca instanciam providers diretamente.

Provedores disponíveis:
  openai    (padrão) — requer OPENAI_API_KEY ou AI_INTEGRATIONS_OPENAI_*
  anthropic           — requer ANTHROPIC_API_KEY

Modelos configuráveis via variáveis de ambiente:
  OPENAI_MODEL    (padrão: gpt-4o-mini)
  ANTHROPIC_MODEL (padrão: claude-3-5-haiku-latest)
"""
import logging
import os
from providers.base import LLMProvider

logger = logging.getLogger("notha.llm")

_instances: dict[str, LLMProvider] = {}


def get_provider(provider: str | None = None) -> LLMProvider:
    """Retorna o singleton do provider especificado (ou do configurado em LLM_PROVIDER)."""
    name = (provider or os.environ.get("LLM_PROVIDER", "openai")).lower()

    if name not in _instances:
        if name == "openai":
            from providers.openai_provider import OpenAIProvider
            _instances[name] = OpenAIProvider()
            logger.info("Provider LLM inicializado: OpenAI (modelo=%s)",
                        os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))

        elif name == "anthropic":
            from providers.anthropic_provider import AnthropicProvider
            _instances[name] = AnthropicProvider()
            logger.info("Provider LLM inicializado: Anthropic (modelo=%s)",
                        os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"))

        else:
            raise RuntimeError(
                f"Provider '{name}' não reconhecido. Opções válidas: openai, anthropic."
            )

    return _instances[name]
