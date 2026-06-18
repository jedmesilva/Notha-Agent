from abc import ABC, abstractmethod


class Tool(ABC):
    """Interface base para todas as tools do agente."""

    name: str
    description: str
    parameters: dict  # JSON Schema dos parâmetros

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """
        Executa a tool e retorna o resultado como string.
        O LLM vai receber esse texto como resultado da chamada.
        """
        ...

    def to_openai_schema(self) -> dict:
        """Serializa a tool no formato de function calling da OpenAI."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
