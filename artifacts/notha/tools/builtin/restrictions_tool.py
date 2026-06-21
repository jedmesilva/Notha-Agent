import logging
from tools.base import Tool

logger = logging.getLogger("notha.tools.restrictions")


class RestrictionCheckTool(Tool):
    name = "verificar_restricao"
    description = (
        "Verifica se um produto pode ser negociado no NOTHA consultando a lista de itens restritos no banco de dados. "
        "OBRIGATÓRIO chamar antes de aceitar qualquer anúncio de venda ou busca de compra. "
        "Retorna PERMITIDO (produto liberado) ou RESTRITO (com categoria e motivo legal). "
        "Use sempre que o usuário mencionar qualquer produto que possa ser ilegal ou regulado."
    )
    parameters = {
        "type": "object",
        "properties": {
            "descricao_produto": {
                "type": "string",
                "description": (
                    "Descrição do produto a verificar. "
                    "Inclua nome, tipo e características relevantes. "
                    "Exemplos: 'pistola 9mm', 'papagaio silvestre', 'camiseta nike réplica'."
                ),
            }
        },
        "required": ["descricao_produto"],
    }

    async def execute(self, descricao_produto: str) -> str:
        try:
            from db.connection import get_db
            db = get_db()
            if db is None:
                logger.warning("Database unavailable — restriction check skipped.")
                return "BANCO_INDISPONIVEL: verificação não realizada, prossiga com cautela."

            from db.repositories.restrictions import RestrictionRepository
            repo = RestrictionRepository(db)
            matches = await repo.check(descricao_produto)

            if not matches:
                return "PERMITIDO: nenhuma restrição encontrada para este produto."

            lines = ["RESTRITO: este produto não pode ser negociado no NOTHA.\n"]
            for r in matches:
                category = r.get("category", "").replace("_", " ").title()
                reason = r.get("reason", "")
                scope = r.get("scope", "national")
                state_code = r.get("state_code") or ""
                municipality = r.get("municipality") or ""

                location = ""
                if scope == "state" and state_code:
                    location = f" (state: {state_code})"
                elif scope == "municipal" and municipality:
                    location = f" (municipality: {municipality})"

                lines.append(f"- Category: {category}{location}\n  Reason: {reason}")

            return "\n".join(lines)

        except Exception as e:
            logger.error("Error checking restriction: %s", e)
            return f"ERRO_VERIFICACAO: não foi possível verificar ({e}). Prossiga com cautela."
