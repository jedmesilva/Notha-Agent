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
    """Interface base para todos os provedores de LLM.

    Mensagens seguem o formato canônico OpenAI:
      {"role": "system"|"user"|"assistant", "content": str | list}

    O provider é responsável por traduzir para o formato nativo (ex: Anthropic).
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        Envia mensagens ao LLM e retorna a resposta normalizada.

        Args:
            messages:    Histórico no formato canônico OpenAI.
            tools:       Schemas de tools no formato OpenAI function calling.
            model:       Sobrescreve o modelo padrão do provider para esta chamada.
            temperature: Temperatura de amostragem.
            max_tokens:  Limite de tokens na resposta.
            json_mode:   Se True, força resposta em JSON válido.

        Returns:
            LLMResponse com .text (str | None) e .tool_calls (list[ToolCall]).
        """
        ...
