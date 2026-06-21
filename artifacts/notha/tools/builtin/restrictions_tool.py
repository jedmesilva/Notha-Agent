import logging
from tools.base import Tool

logger = logging.getLogger("notha.tools.restrictions")


class RestrictionCheckTool(Tool):
    name = "verificar_restricao"
    description = (
        "Verifica se um produto ou item pode ser negociado no NOTHA. "
        "OBRIGATÓRIO chamar antes de aceitar qualquer anúncio de venda ou busca de compra. "
        "Retorna se o item é permitido ou restrito, com o motivo legal quando restrito. "
        "Exemplos de uso: antes de listar_produto, antes de buscar_produto, "
        "quando o usuário mencionar qualquer produto que possa ser ilegal ou regulado."
    )
    parameters = {
        "type": "object",
        "properties": {
            "descricao_produto": {
                "type": "string",
                "description": (
                    "Descrição do produto a verificar. "
                    "Inclua nome, tipo, características relevantes. "
                    "Exemplo: 'pistola 9mm', 'papagaio silvestre', 'camiseta nike réplica'."
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
                logger.warning("Banco indisponível — verificação de restrição ignorada.")
                return "BANCO_INDISPONIVEL: verificação não realizada, prossiga com cautela."

            from db.repositories.restrictions import RestrictionRepository
            repo = RestrictionRepository(db)
            restricoes = await repo.verificar(descricao_produto)

            if not restricoes:
                return "PERMITIDO: nenhuma restrição encontrada para este produto."

            linhas = ["RESTRITO: este produto não pode ser negociado no NOTHA.\n"]
            for r in restricoes:
                cat = r.get("categoria", "").replace("_", " ").title()
                motivo = r.get("motivo", "")
                abrangencia = r.get("abrangencia", "nacional")
                estado = r.get("estado") or ""
                municipio = r.get("municipio") or ""

                local = ""
                if abrangencia == "estadual" and estado:
                    local = f" (estado: {estado})"
                elif abrangencia == "municipal" and municipio:
                    local = f" (município: {municipio})"

                linhas.append(f"- Categoria: {cat}{local}\n  Motivo: {motivo}")

            return "\n".join(linhas)

        except Exception as e:
            logger.error("Erro ao verificar restrição: %s", e)
            return f"ERRO_VERIFICACAO: não foi possível verificar ({e}). Prossiga com cautela."
