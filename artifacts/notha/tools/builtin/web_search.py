import re
import logging
import httpx
from tools.base import Tool

logger = logging.getLogger("notha.tools.web_search")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


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
            async with httpx.AsyncClient(
                timeout=12, follow_redirects=True, headers=_HEADERS
            ) as client:
                resp = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                )
                resp.raise_for_status()
                html = resp.text

            titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
            snippets = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL
            )

            results = []
            for title, snippet in zip(titles[:4], snippets[:4]):
                t = _clean(title)
                s = _clean(snippet)
                if t or s:
                    results.append(f"{t}\n{s}".strip())

            if results:
                return "\n\n".join(results)

            return "Não encontrei resultados para essa pesquisa."

        except Exception as e:
            logger.error(f"Erro na busca web: {e}")
            return f"Não consegui realizar a pesquisa no momento: {e}"
