from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Classe base para provedores de LLM."""

    @abstractmethod
    async def chat(self, history: list[dict]) -> str:
        """
        Envia o histórico de conversa e retorna a resposta do modelo.

        Args:
            history: Lista de mensagens no formato [{"role": "user"|"assistant", "content": "..."}]

        Returns:
            Texto da resposta gerada pelo modelo.
        """
        ...
