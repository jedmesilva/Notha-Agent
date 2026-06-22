import logging
from tools.base import Tool

logger = logging.getLogger("notha.tools.web_search")


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Searches the internet for up-to-date information. Use when you need recent data, "
        "news, facts, or any information that may be outdated in your training."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search term or question to look up.",
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
                return "No results found for this search."

            lines = []
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                lines.append(f"{title}\n{body}\nSource: {href}")

            return "\n\n".join(lines)

        except Exception as e:
            logger.error(f"Web search error: {e}")
            return f"Could not perform the search at this time: {e}"
