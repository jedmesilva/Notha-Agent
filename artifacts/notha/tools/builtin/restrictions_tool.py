"""
Verificação semântica de restrições de produtos — arquitetura correta.

Fluxo:
  1. LLM lê a descrição do usuário e gera termos de busca precisos
     (nome normalizado, sinônimos, gírias, termos relacionados em PT-BR)
  2. DB busca APENAS por esses termos — retorna só os registros encontrados,
     nunca a lista completa. Filtro de localização aplicado automaticamente.
  3. LLM faz julgamento final: os registros encontrados realmente se aplicam
     a ESTE produto específico? (elimina falsos positivos — ex: "faca de cozinha"
     pode casar com keywords de "faca de combate", mas não é a mesma coisa)
  4. Retorna PERMITIDO ou RESTRITO com base no banco, não em suposições.

O LLM nunca decide por conta própria — só interpreta e busca.
O banco é a fonte de verdade. O LLM é o intérprete e árbitro semântico.
"""
import json
import logging

from tools.base import Tool

logger = logging.getLogger("notha.tools.restrictions")

# ── Passo 1: gerar termos de busca ──────────────────────────────────────────
_PROMPT_GERAR_TERMOS = """You are a product classification expert for a global marketplace platform.

The user wants to trade the following product:
"{descricao}"
User location: {localizacao}

Your task: generate the best search terms to find this product in a database of restricted items.

Generate terms across ALL relevant languages (the user's language, Portuguese, English, Spanish, and any language relevant to the product's origin or the user's region). Include:
- Official/technical name of the product (in multiple languages if applicable)
- Common names and regional variations
- Slang, informal terms and euphemisms in any language
- Brand names if relevant (e.g. "Glock" for a pistol)
- Category-related terms — but be SPECIFIC: "kitchen knife" is NOT "combat knife", "toy gun" is NOT "firearm"

Examples:
- "roscoe" → terms: ["roscoe", "revólver", "pistola", "arma de fogo", "handgun", "revolver", "firearm"]
- "faca de cozinha" → terms: ["faca de cozinha", "kitchen knife", "cuchillo de cocina"] — NOT "arma branca"
- "arm" (body part) → terms: ["braço", "arm", "membro superior"] — NOT "arma", "weapon"
- "marijuana" → terms: ["marijuana", "cannabis", "maconha", "weed", "erva", "baseado"]

Return ONLY valid JSON:
{{"produto_identificado": "<normalized product name in English>", "termos_busca": ["term1", "term2", ...]}}

Maximum 12 terms. Be specific — avoid overly broad terms that cause false positives."""

# ── Passo 3: julgamento final ────────────────────────────────────────────────
_PROMPT_JULGAMENTO = """Você é um especialista em regulamentação de marketplace.

O usuário quer negociar: "{produto_usuario}"
Produto identificado pelo sistema: "{produto_identificado}"

A busca no banco de dados retornou os seguintes registros de itens possivelmente restritos:
{registros}

Pergunta: esses registros se aplicam ESPECIFICAMENTE ao produto do usuário?

Considere:
- Uma "faca de cozinha" NÃO é uma "faca de combate" — mesmo que ambas sejam facas
- Um "revólver de brinquedo" NÃO é uma arma de fogo real
- Um "medicamento com receita" NÃO é o mesmo que "droga ilícita"
- Seja conservador: se o produto do usuário claramente se encaixa na restrição, confirme
- Seja preciso: não restrinja produtos legítimos por semelhança superficial de nome

Retorne SOMENTE JSON válido:

Se nenhum registro se aplica a este produto específico:
{{"restrito": false}}

Se algum registro se aplica (liste apenas os IDs que realmente se aplicam):
{{"restrito": true, "ids_aplicaveis": [1, 5], "justificativa": "<motivo conciso>"}}"""


async def _call_llm_json(prompt: str, max_tokens: int = 300) -> dict:
    """Calls the LLM in JSON mode and returns a dict. Silent failure returns {}."""
    from llm import get_provider
    try:
        resp = await get_provider().complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
        )
        return json.loads(resp.text or "{}")
    except Exception as e:
        logger.error("LLM error in restriction check: %s", e)
        return {}


class RestrictionCheckTool(Tool):
    name = "verificar_restricao"
    description = (
        "Verifica se um produto pode ser negociado no NOTHA. "
        "OBRIGATÓRIO chamar antes de aceitar qualquer anúncio de venda ou busca de compra. "
        "Entende semanticamente o produto — gírias, sinônimos e outros idiomas são reconhecidos. "
        "Retorna PERMITIDO ou RESTRITO com base em registros reais do banco de dados."
    )
    parameters = {
        "type": "object",
        "properties": {
            "descricao_produto": {
                "type": "string",
                "description": (
                    "Descrição do produto exatamente como o usuário mencionou. "
                    "Inclua nome, tipo, marca e características relevantes. "
                    "Exemplos: 'pistola 9mm', 'papagaio silvestre', 'camiseta nike réplica', 'roscoe calibre 38'."
                ),
            },
            "estado": {
                "type": "string",
                "description": (
                    "Sigla do estado do usuário (ex: 'SP', 'RJ', 'MG') — "
                    "para aplicar restrições estaduais. Opcional."
                ),
            },
            "municipio": {
                "type": "string",
                "description": (
                    "Município do usuário (ex: 'São Paulo', 'Campinas') — "
                    "para aplicar restrições municipais. Opcional."
                ),
            },
        },
        "required": ["descricao_produto"],
    }

    async def execute(
        self,
        descricao_produto: str,
        estado: str | None = None,
        municipio: str | None = None,
    ) -> str:
        try:
            from db.connection import get_db
            db = get_db()
            if db is None:
                logger.warning("Banco indisponível — verificação de restrição ignorada.")
                return "BANCO_INDISPONIVEL: verificação não realizada, prossiga com cautela."

            from db.repositories.restrictions import RestrictionRepository
            repo = RestrictionRepository(db)

            # ── Passo 1: LLM gera termos de busca específicos ───────────────
            loc_parts = []
            if municipio:
                loc_parts.append(municipio)
            if estado:
                loc_parts.append(f"state {estado}")
            localizacao_str = ", ".join(loc_parts) if loc_parts else "unknown"

            resultado_termos = await _call_llm_json(
                _PROMPT_GERAR_TERMOS.format(
                    descricao=descricao_produto,
                    localizacao=localizacao_str,
                ),
                max_tokens=300,
            )

            termos = resultado_termos.get("termos_busca", [])
            produto_identificado = resultado_termos.get("produto_identificado", descricao_produto)

            if not termos:
                # Fallback: usa a descrição bruta como único termo
                termos = [descricao_produto]

            logger.info(
                "Verificação: produto='%s' termos=%s",
                produto_identificado, termos[:5]
            )

            # ── Passo 2: DB busca APENAS pelos termos gerados ───────────────
            encontrados = await repo.search_by_terms(
                terms=termos,
                state_code=estado or None,
                municipality=municipio or None,
            )

            if not encontrados:
                logger.info("Verificação: PERMITIDO — nenhum registro encontrado para '%s'", produto_identificado)
                return "PERMITIDO: nenhuma restrição encontrada para este produto."

            # ── Passo 3: LLM julga se os registros realmente se aplicam ─────
            registros_fmt = "\n".join(
                f"ID {r['id']}: [{r['category'].replace('_',' ').upper()}] "
                f"{r['description']} — {r['reason']}"
                + (f" (estado: {r['state_code']})" if r.get('state_code') else "")
                + (f" (município: {r['municipality']})" if r.get('municipality') else "")
                for r in encontrados
            )

            resultado_julgamento = await _call_llm_json(
                _PROMPT_JULGAMENTO.format(
                    produto_usuario=descricao_produto,
                    produto_identificado=produto_identificado,
                    registros=registros_fmt,
                ),
                max_tokens=200,
            )

            if not resultado_julgamento.get("restrito", False):
                logger.info(
                    "Verificação: PERMITIDO após julgamento — '%s' não se enquadra nos registros encontrados",
                    produto_identificado,
                )
                return "PERMITIDO: produto não se enquadra nas restrições cadastradas."

            # ── Monta resposta final com os registros confirmados ────────────
            ids_confirmados = resultado_julgamento.get("ids_aplicaveis", [])
            registros_confirmados = (
                [r for r in encontrados if r["id"] in ids_confirmados]
                if ids_confirmados else encontrados
            )

            logger.warning(
                "Verificação: RESTRITO — '%s' | categorias=%s",
                produto_identificado,
                [r["category"] for r in registros_confirmados],
            )

            linhas = ["RESTRITO: este produto não pode ser negociado no NOTHA.\n"]
            for r in registros_confirmados:
                categoria = r.get("category", "").replace("_", " ").title()
                reason = r.get("reason", "")
                scope = r.get("scope", "national")
                state_code = r.get("state_code") or ""
                mun = r.get("municipality") or ""

                location = ""
                if scope == "state" and state_code:
                    location = f" (estado: {state_code})"
                elif scope == "municipal" and mun:
                    location = f" (município: {mun})"

                linhas.append(f"- Categoria: {categoria}{location}\n  Motivo: {reason}")

            return "\n".join(linhas)

        except Exception as e:
            logger.error("Erro ao verificar restrição: %s", e)
            return f"ERRO_VERIFICACAO: não foi possível verificar ({e}). Prossiga com cautela."
