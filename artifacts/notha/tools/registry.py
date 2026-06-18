import logging
from tools.base import Tool

logger = logging.getLogger("notha.tools")


class ToolRegistry:
    """Registro central de tools disponíveis para o agente."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Registra uma tool pelo nome."""
        self._tools[tool.name] = tool
        logger.info(f"Tool registrada: {tool.name}")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_schemas(self) -> list[dict]:
        """Retorna os schemas de todas as tools no formato OpenAI."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    async def execute(self, name: str, args: dict) -> str:
        """Executa uma tool pelo nome e retorna o resultado como string."""
        tool = self.get(name)
        if not tool:
            return f"Erro: tool '{name}' não encontrada."
        try:
            logger.info(f"Executando tool '{name}' com args: {args}")
            result = await tool.execute(**args)
            logger.info(f"Tool '{name}' retornou: {str(result)[:100]}")
            return result
        except Exception as e:
            logger.error(f"Erro ao executar tool '{name}': {e}")
            return f"Erro ao executar '{name}': {e}"


registry = ToolRegistry()
