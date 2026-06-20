"""
Pricing/Appraisal Agent — forma sugestão de preço e preço mínimo no cadastro do produto.

Roda UMA VEZ por listing, de forma assíncrona.
Consulta: mercado externo (web search), histórico interno (SQL), avaliação visual (vision LLM).
Saída estruturada: preco_sugerido, preco_minimo_sugerido, justificativa, confianca, fontes.
"""
import json
import logging
from openai import AsyncOpenAI
from config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger("notha.agent.pricing")

PRICING_SYSTEM_PROMPT = """Você é o agente de precificação do NOTHA, especializado em avaliar produtos físicos usados para venda via WhatsApp.

Sua tarefa é sugerir:
1. Um preço anunciado justo (preco_sugerido)
2. Um preço mínimo aceitável (preco_minimo_sugerido) — nunca revelado ao comprador

Baseie-se nos dados fornecidos: descrição, fotos (se disponíveis), histórico de vendas similares e preço de mercado.

REGRA CRÍTICA: nunca sugira um preço mínimo abaixo de 60% do valor de mercado do produto novo sem justificativa explícita de dano severo ou produto incompleto.

Retorne SOMENTE um JSON válido com os campos:
{
  "preco_sugerido": <número>,
  "preco_minimo_sugerido": <número>,
  "justificativa": "<texto explicando a avaliação>",
  "confianca": "alta|media|baixa",
  "fontes": ["historico_interno", "avaliacao_visual", "mercado_externo", "descricao_textual"]
}
"""


def _make_client() -> AsyncOpenAI:
    if OPENAI_API_KEY:
        return AsyncOpenAI(api_key=OPENAI_API_KEY)
    api_key = os.environ.get("OPENAI_API_KEY", "nokey")
    return AsyncOpenAI(api_key=api_key, base_url=OPENAI_BASE_URL)


class PricingAgent:
    def __init__(self, db=None):
        self._client: AsyncOpenAI | None = None
        self._db = db

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = _make_client()
        return self._client

    async def appraise(
        self,
        descricao: str,
        categoria: str | None,
        fotos: list[str] | None = None,
        preco_informado_vendedor: float | None = None,
        historico_similares: list[dict] | None = None,
    ) -> dict:
        fontes_usadas = []
        contexto_parts = [f"Descrição do produto: {descricao}"]

        if categoria:
            contexto_parts.append(f"Categoria: {categoria}")

        if preco_informado_vendedor:
            contexto_parts.append(f"Preço informado pelo vendedor: R${preco_informado_vendedor:.2f}")

        if historico_similares:
            fontes_usadas.append("historico_interno")
            precos = [h.get("preco_final", h.get("preco_anunciado", 0)) for h in historico_similares if h.get("preco_final") or h.get("preco_anunciado")]
            if precos:
                media = sum(precos) / len(precos)
                contexto_parts.append(f"Histórico de {len(precos)} vendas similares: média R${media:.2f}, valores: {[f'R${p:.0f}' for p in precos]}")

        contexto_parts.append("Descricao textual analisada.")
        fontes_usadas.append("descricao_textual")

        user_content = []

        if fotos:
            fontes_usadas.append("avaliacao_visual")
            for foto_url in fotos[:3]:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": foto_url, "detail": "low"},
                })

        user_content.append({
            "type": "text",
            "text": "\n".join(contexto_parts) + "\n\nAvalie e retorne o JSON de precificação.",
        })

        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": PRICING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            result = json.loads(raw)
            result["fontes"] = list(set(result.get("fontes", []) + fontes_usadas))

            if preco_informado_vendedor and result.get("preco_sugerido"):
                diff_pct = abs(preco_informado_vendedor - result["preco_sugerido"]) / result["preco_sugerido"]
                result["alerta_preco_vendedor"] = diff_pct > 0.20

            return result

        except Exception as e:
            logger.error(f"Erro no PricingAgent.appraise: {e}")
            fallback = preco_informado_vendedor or 0
            return {
                "preco_sugerido": fallback,
                "preco_minimo_sugerido": round(fallback * 0.85, 2),
                "justificativa": "Avaliação automática indisponível. Usando referência do vendedor.",
                "confianca": "baixa",
                "fontes": ["descricao_textual"],
                "alerta_preco_vendedor": False,
            }

    async def web_search_price(self, query: str) -> str | None:
        """Busca preço de mercado via web search (usa DDGS se disponível)."""
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(f"preço {query} usado Brasil", max_results=3))
                if results:
                    snippets = [r.get("body", "") for r in results]
                    return " | ".join(snippets[:3])
        except Exception as e:
            logger.debug(f"Web search indisponível: {e}")
        return None

    async def appraise_with_web_search(
        self,
        descricao: str,
        categoria: str | None,
        fotos: list[str] | None = None,
        preco_informado_vendedor: float | None = None,
        historico_similares: list[dict] | None = None,
    ) -> dict:
        web_context = await self.web_search_price(descricao)
        hist = list(historico_similares or [])
        if web_context:
            hist = [{"descricao": "mercado_externo", "preco_final": None, "_web": web_context}] + hist

        result = await self.appraise(
            descricao=descricao,
            categoria=categoria,
            fotos=fotos,
            preco_informado_vendedor=preco_informado_vendedor,
            historico_similares=hist,
        )
        if web_context:
            result["fontes"] = list(set(result.get("fontes", []) + ["mercado_externo"]))
        return result
