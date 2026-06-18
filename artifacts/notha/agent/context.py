import json
import logging
from storage.base import Storage

logger = logging.getLogger("notha.context")

MAX_MESSAGES = 40


class ConversationContext:
    """
    Gerencia o histórico de conversa por usuário (phone number).
    Persiste via Storage — em memória por padrão, Supabase quando configurado.
    """

    def __init__(self, storage: Storage):
        self._storage = storage

    def _key(self, phone: str) -> str:
        return f"conversation:{phone}"

    async def get_messages(self, phone: str) -> list[dict]:
        """Retorna o histórico de mensagens do usuário."""
        messages = await self._storage.get(self._key(phone))
        return messages or []

    async def add_user_message(self, phone: str, content: str) -> None:
        messages = await self.get_messages(phone)
        messages.append({"role": "user", "content": content})
        await self._save(phone, messages)

    async def add_assistant_message(self, phone: str, content: str) -> None:
        messages = await self.get_messages(phone)
        messages.append({"role": "assistant", "content": content})
        await self._save(phone, messages)

    async def add_tool_call(self, phone: str, tool_calls: list) -> None:
        """Adiciona a mensagem do assistente com tool_calls."""
        messages = await self.get_messages(phone)
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.args, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        })
        await self._save(phone, messages)

    async def add_tool_result(self, phone: str, tool_call_id: str, result: str) -> None:
        """Adiciona o resultado de uma tool ao histórico."""
        messages = await self.get_messages(phone)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        })
        await self._save(phone, messages)

    async def clear(self, phone: str) -> None:
        """Limpa o histórico do usuário."""
        await self._storage.delete(self._key(phone))
        logger.info(f"Histórico limpo para {phone}.")

    async def _save(self, phone: str, messages: list[dict]) -> None:
        if len(messages) > MAX_MESSAGES:
            messages = messages[-MAX_MESSAGES:]
        await self._storage.set(self._key(phone), messages)
