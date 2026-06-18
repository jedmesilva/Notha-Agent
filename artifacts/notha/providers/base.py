from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """Representa uma chamada de tool solicitada pelo LLM."""
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    """Resposta normalizada do LLM — texto final ou tool calls."""
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """Interface base para todos os provedores de LLM."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """
        Envia mensagens ao LLM e retorna a resposta normalizada.

        Args:
            messages: Histórico no formato canônico (OpenAI-compatible).
            tools:    Lista de schemas de tools no formato OpenAI function calling.

        Returns:
            LLMResponse com texto final ou tool_calls para executar.
        """
        ...
