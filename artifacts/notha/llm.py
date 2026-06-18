import os
import logging
from providers.base import LLMProvider

logger = logging.getLogger("notha.llm")

SYSTEM_PROMPT = """Você é o Notha, um assistente inteligente e prestativo disponível pelo WhatsApp.
Responda de forma clara, concisa e amigável. Suas respostas devem ser adequadas para o formato de mensagem do WhatsApp — evite formatações complexas como markdown, use texto simples."""


def get_provider() -> LLMProvider:
    """
    Retorna o provedor de LLM configurado via variável de ambiente LLM_PROVIDER.

    Provedores disponíveis:
      - openai    (padrão) → requer OPENAI_API_KEY ou AI_INTEGRATIONS_OPENAI_*
      - anthropic           → requer ANTHROPIC_API_KEY

    Modelos configuráveis via:
      - OPENAI_MODEL    (padrão: gpt-4o-mini)
      - ANTHROPIC_MODEL (padrão: claude-3-5-haiku-latest)
    """
    provider_name = os.environ.get("LLM_PROVIDER", "openai").lower()

    if provider_name == "openai":
        from providers.openai_provider import OpenAIProvider
        logger.info("Usando provedor: OpenAI")
        return OpenAIProvider(system_prompt=SYSTEM_PROMPT)

    if provider_name == "anthropic":
        from providers.anthropic_provider import AnthropicProvider
        logger.info("Usando provedor: Anthropic")
        return AnthropicProvider(system_prompt=SYSTEM_PROMPT)

    raise RuntimeError(
        f"Provedor '{provider_name}' não reconhecido. "
        "Opções válidas: openai, anthropic."
    )


async def chat(history: list[dict]) -> str:
    """Envia o histórico ao provedor configurado e retorna a resposta."""
    provider = get_provider()
    return await provider.chat(history)
