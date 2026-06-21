"""
Verificação semântica de restrições de produtos.

Fluxo:
  1. Busca todas as categorias de restrição ativas no banco
  2. LLM avalia semanticamente se o produto descrito se enquadra em alguma delas
     — independente da língua usada, gírias, sinônimos ou eufemismos
  3. Se houver match, busca os registros completos pelo ID e retorna os detalhes
  4. Se não houver match, retorna PERMITIDO

O banco é a fonte de verdade das regras.
O LLM é o intérprete semântico — ele entende o que o produto é, não compara strings.
"""
import json
import logging

from tools.base import Tool

logger = logging.getLogger("notha.tools.restrictions")

_AVALIACAO_PROMPT = """Você é um especialista em classificação de produtos para uma plataforma de marketplace.

Analise o produto descrito e verifique se ele se enquadra em alguma das categorias de restrição listadas abaixo.

━━━ PRODUTO DESCRITO PELO USUÁRIO ━━━
{descricao}

━━━ CATEGORIAS DE RESTRIÇÃO ATIVAS (do banco de dados) ━━━
{categorias}

━━━ INSTRUÇÕES ━━━
- Interprete semanticamente o produto — considere sinônimos, gírias, outros idiomas e eufemismos.
  Exemplos: "roscoe", "ferro", "glock", "pistola", "gun", "arma" → todos são armas de fogo.
  "braço" (membro humano), "arm chair" (poltrona) → NÃO são armas.
- Seja conservador: na dúvida sobre um produto claramente perigoso, inclua no match.
- Seja preciso: não restrinja produtos legítimos por ambiguidade de linguagem.
- Retorne SOMENTE JSON válido, sem texto extra.

━━━ FORMATO DE RETORNO ━━━
Se o produto é permitido:
{{"permitido": true, "ids_restricoes": []}}

Se o produto é restrito (liste os IDs das categorias que se aplicam):
{{"permitido": false, "ids_restricoes": [1, 5, 12]}}
"""


class RestrictionCheckTool(Tool):
    name = "verificar_restricao"
    description = (
        "Verifica se um produto pode ser negociado no NOTHA consultando a base de restrições. "
        "OBRIGATÓRIO chamar antes de aceitar qualquer anúncio de venda ou busca de compra. "
        "O sistema entende semanticamente o produto — gírias, sinônimos e outros idiomas são reconhecidos. "
        "Retorna PERMITIDO (produto liberado) ou RESTRITO (com categoria e motivo legal)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "descricao_produto": {
                "type": "string",
                "description": (
                    "Descrição completa do produto a verificar, exatamente como o usuário descreveu. "
                    "Inclua nome, tipo, marca e características relevantes mencionadas. "
                    "Exemplos: 'pistola 9mm', 'papagaio silvestre', 'camiseta nike réplica', 'roscoe calibre 38'."
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

            # ── Passo 1: busca todas as restrições ativas do banco ───────────
            restricoes = await repo.list_active_for_llm()

            if not restricoes:
                return "PERMITIDO: nenhuma restrição cadastrada no sistema."

            # ── Passo 2: LLM avalia semanticamente ──────────────────────────
            categorias_fmt = "\n".join(
                f"ID {r['id']}: [{r['category'].replace('_', ' ').upper()}] "
                f"{r['description']} — {r['reason']}"
                for r in restricoes
            )

            prompt = _AVALIACAO_PROMPT.format(
                descricao=descricao_produto,
                categorias=categorias_fmt,
            )

            from llm import get_provider
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=200,
                json_mode=True,
            )

            resultado = json.loads(resp.text or '{"permitido": true, "ids_restricoes": []}')

            if resultado.get("permitido", True):
                logger.info("Restrição: PERMITIDO para '%s'", descricao_produto[:60])
                return "PERMITIDO: nenhuma restrição encontrada para este produto."

            # ── Passo 3: busca os detalhes completos pelos IDs identificados ─
            ids_match = [int(i) for i in resultado.get("ids_restricoes", []) if str(i).isdigit()]
            if not ids_match:
                return "PERMITIDO: nenhuma restrição encontrada para este produto."

            detalhes = await repo.fetch_by_ids(ids_match)
            if not detalhes:
                return "PERMITIDO: nenhuma restrição ativa encontrada para este produto."

            logger.warning(
                "Restrição: RESTRITO para '%s' — categorias: %s",
                descricao_produto[:60],
                [r["category"] for r in detalhes],
            )

            linhas = ["RESTRITO: este produto não pode ser negociado no NOTHA.\n"]
            for r in detalhes:
                categoria = r.get("category", "").replace("_", " ").title()
                reason = r.get("reason", "")
                scope = r.get("scope", "national")
                state_code = r.get("state_code") or ""
                municipality = r.get("municipality") or ""

                location = ""
                if scope == "state" and state_code:
                    location = f" (estado: {state_code})"
                elif scope == "municipal" and municipality:
                    location = f" (município: {municipality})"

                linhas.append(f"- Categoria: {categoria}{location}\n  Motivo: {reason}")

            return "\n".join(linhas)

        except Exception as e:
            logger.error("Erro ao verificar restrição: %s", e)
            return f"ERRO_VERIFICACAO: não foi possível verificar ({e}). Prossiga com cautela."
