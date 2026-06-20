import logging
from tools.base import Tool

logger = logging.getLogger("notha.tools.web_search")


class WebSearchTool(Tool):
    name = "pesquisar_web"
    description = (
        "Pesquisa informações atualizadas na internet. Use quando precisar de "
        "dados recentes, notícias, fatos ou qualquer informação que possa estar "
        "desatualizada no seu treinamento."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "O termo ou pergunta a pesquisar.",
            }
        },
        "required": ["query"],
    }

    async def execute(self, query: str) -> str:
        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=4))

            if not results:
                return "Não encontrei resultados para essa pesquisa."

            lines = []
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                lines.append(f"{title}\n{body}\nFonte: {href}")

            return "\n\n".join(lines)

        except Exception as e:
            logger.error(f"Erro na busca web: {e}")
            return f"Não consegui realizar a pesquisa no momento: {e}"
