import httpx
from tools.base import Tool


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
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": "1",
                        "skip_disambig": "1",
                    },
                    headers={"User-Agent": "Notha-Agent/1.0"},
                )
                data = response.json()

            if data.get("AbstractText"):
                source = data.get("AbstractURL", "")
                return f"{data['AbstractText']}\nFonte: {source}".strip()

            results = []
            for topic in data.get("RelatedTopics", [])[:4]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append(topic["Text"])

            if results:
                return "\n\n".join(results)

            return "Não encontrei resultados diretos para essa pesquisa."

        except Exception as e:
            return f"Erro ao pesquisar: {e}"
