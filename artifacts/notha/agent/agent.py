import logging
import os

from agent.context import ConversationContext
from agent.prompt import load_system_prompt
from providers.base import LLMProvider
from tools.registry import ToolRegistry

logger = logging.getLogger("notha.agent")

MAX_TOOL_ITERATIONS = 5


def _get_storage():
    provider = os.environ.get("STORAGE_PROVIDER", "memory").lower()
    if provider == "supabase":
        from storage.supabase_storage import SupabaseStorage
        return SupabaseStorage()
    from storage.memory_storage import MemoryStorage
    return MemoryStorage()


def _get_llm_provider() -> LLMProvider:
    provider_name = os.environ.get("LLM_PROVIDER", "openai").lower()
    system_prompt = load_system_prompt()

    if provider_name == "openai":
        from providers.openai_provider import OpenAIProvider
        logger.info(f"LLM: OpenAI ({os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')})")
        return OpenAIProvider()

    if provider_name == "anthropic":
        from providers.anthropic_provider import AnthropicProvider
        logger.info(f"LLM: Anthropic ({os.environ.get('ANTHROPIC_MODEL', 'claude-3-5-haiku-latest')})")
        provider = AnthropicProvider()
        provider.set_system_prompt(system_prompt)
        return provider

    raise RuntimeError(f"Provedor LLM desconhecido: '{provider_name}'. Use: openai, anthropic.")


def _build_messages(system_prompt: str, history: list[dict]) -> list[dict]:
    return [{"role": "system", "content": system_prompt}] + history


class Agent:
    """
    Agente principal do Notha.

    Fluxo:
      1. Recebe a mensagem do usuário.
      2. Consulta o histórico de contexto.
      3. Chama o LLM com as tools disponíveis.
      4. Se o LLM solicitar tools → executa → alimenta o resultado de volta → repete.
      5. Quando o LLM gera uma resposta final → retorna para o WhatsApp.
    """

    def __init__(self, registry: ToolRegistry):
        self._registry = registry
        self._storage = _get_storage()
        self._context = ConversationContext(self._storage)
        self._system_prompt = load_system_prompt()

    async def run(self, phone: str, user_message: str) -> str:
        await self._context.add_user_message(phone, user_message)

        provider = _get_llm_provider()
        tool_schemas = self._registry.get_schemas()

        for iteration in range(MAX_TOOL_ITERATIONS):
            history = await self._context.get_messages(phone)
            messages = _build_messages(self._system_prompt, history)

            response = await provider.complete(messages, tools=tool_schemas or None)

            if not response.has_tool_calls:
                final_text = response.text or "Desculpe, não consegui gerar uma resposta."
                await self._context.add_assistant_message(phone, final_text)
                return final_text

            logger.info(f"Iteração {iteration + 1}: {len(response.tool_calls)} tool(s) solicitada(s).")
            await self._context.add_tool_call(phone, response.tool_calls)

            for tool_call in response.tool_calls:
                result = await self._registry.execute(tool_call.name, tool_call.args)
                await self._context.add_tool_result(phone, tool_call.id, result)

        logger.warning(f"Limite de {MAX_TOOL_ITERATIONS} iterações atingido para {phone}.")
        fallback = "Não consegui completar a tarefa no tempo esperado. Tente novamente."
        await self._context.add_assistant_message(phone, fallback)
        return fallback

    async def reset(self, phone: str) -> None:
        await self._context.clear(phone)
