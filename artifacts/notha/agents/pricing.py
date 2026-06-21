"""
Pricing/Appraisal Agent — suggests price and minimum price when listing a product.

Runs ONCE per listing, asynchronously.
Consults: external market (web search), internal history (SQL), visual assessment (vision LLM).
Structured output: preco_sugerido, preco_minimo_sugerido, justificativa, confianca, fontes.
"""
import json
import logging
from llm import get_provider

logger = logging.getLogger("notha.agent.pricing")

PRICING_SYSTEM_PROMPT = """Você é o agente de precificação do NOTHA — especialista em avaliar produtos físicos usados para venda entre particulares via WhatsApp, no mercado brasileiro.

━━━ SUA TAREFA ━━━
Com base nos dados fornecidos, sugira:
1. preco_sugerido — preço justo para anunciar publicamente (atrativo para compradores, honesto para o mercado)
2. preco_minimo_sugerido — piso abaixo do qual o vendedor não deve aceitar (NUNCA revelado ao comprador)

━━━ CRITÉRIOS DE AVALIAÇÃO ━━━

Estado do produto (aplique depreciação):
- Novo/lacrado: 85-95% do preço de varejo atual
- Seminovo (usado com cuidado, sem marcas): 60-80% do preço de varejo
- Bom estado (uso normal, pequenas marcas): 45-65% do preço de varejo
- Regular (desgaste visível, funciona): 30-50% do preço de varejo
- Ruim / com defeito: 15-30% do preço de varejo — exige menção explícita na descrição

Fatores que elevam o preço:
+ Acompanha acessórios originais, caixa ou nota fiscal
+ Produto descontinuado ou difícil de encontrar
+ Alta demanda no momento (iPhone recente, console novo, etc.)
+ Revisado ou com garantia do vendedor

Fatores que reduzem o preço:
- Sem acessórios ou carregador
- Sem nota fiscal / sem procedência
- Modelo desatualizado (há versão mais nova disponível)
- Arranhões, amassados, tela trincada
- Bateria degradada (eletrônicos)

━━━ REGRAS CRÍTICAS ━━━
- O preço_mínimo NUNCA deve ser abaixo de 55% do preço sugerido (evita venda com grande prejuízo)
- Se o produto tiver defeito grave e funcional, o mínimo pode cair até 40% do valor de mercado, mas exige justificativa explícita
- Se o preço informado pelo vendedor estiver acima de 120% do mercado, sinalize com alerta
- Se o preço informado pelo vendedor estiver abaixo de 60% do mercado, avalie se há defeito implícito não declarado
- Não invente preços de mercado — se não tiver referência, indique confianca: "baixa" e explique
- Preços devem ser múltiplos de R$5 ou R$10 (mais natural em negociações informais)
- Produtos acima de R$5.000: arredonde para múltiplos de R$50

━━━ REFERÊNCIAS POR CATEGORIA ━━━
Eletrônicos: deprecia rápido — celulares perdem 20-30% ao sair da caixa
Eletrodomésticos: deprecia moderado — vida útil longa mantém valor
Móveis: depende muito do estado e da marca
Vestuário / calçados: sem uso = 50-70% do novo; usado = 10-30%
Brinquedos / infantil: completo e limpo vale mais; incompleto cai muito
Veículos (acessórios): siga tabela FIPE como referência
Outros: use bom senso e sinalize confiança baixa se não tiver referência

━━━ SAÍDA OBRIGATÓRIA ━━━
Retorne SOMENTE um JSON válido, sem texto fora do JSON:
{
  "preco_sugerido": <número arredondado>,
  "preco_minimo_sugerido": <número arredondado>,
  "justificativa": "<2-4 frases explicando o raciocínio, mencionando o estado e o mercado>",
  "confianca": "alta | media | baixa",
  "fontes": ["historico_interno", "avaliacao_visual", "mercado_externo", "descricao_textual"],
  "alerta": "<null ou mensagem de alerta se o preço do vendedor for muito discrepante>"
}
"""


class PricingAgent:
    def __init__(self, db=None):
        self._db = db

    async def appraise(
        self,
        descricao: str,
        categoria: str | None,
        fotos: list[str] | None = None,
        preco_informado_vendedor: float | None = None,
        historico_similares: list[dict] | None = None,
    ) -> dict:
        sources_used = []
        context_parts = [f"Descrição do produto: {descricao}"]

        if categoria:
            context_parts.append(f"Categoria: {categoria}")

        if preco_informado_vendedor:
            context_parts.append(f"Preço informado pelo vendedor: R${preco_informado_vendedor:.2f}")

        if historico_similares:
            sources_used.append("historico_interno")
            prices = [
                h.get("preco_final", h.get("preco_anunciado", 0))
                for h in historico_similares
                if h.get("preco_final") or h.get("preco_anunciado")
            ]
            if prices:
                avg = sum(prices) / len(prices)
                minimum = min(prices)
                maximum = max(prices)
                context_parts.append(
                    f"Histórico de {len(prices)} vendas similares no NOTHA: "
                    f"média R${avg:.0f}, mínimo R${minimum:.0f}, máximo R${maximum:.0f}. "
                    f"Valores: {[f'R${p:.0f}' for p in prices]}"
                )

        context_parts.append("Descrição textual analisada.")
        sources_used.append("descricao_textual")

        user_content = []

        if fotos:
            # fotos must be a list of base64 data URIs (data:image/...;base64,...)
            # Direct WhatsApp URLs require Authorization header and won't work here
            valid = [f for f in fotos[:3] if isinstance(f, str) and f.startswith("data:image/")]
            if valid:
                sources_used.append("avaliacao_visual")
                for data_uri in valid:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": data_uri, "detail": "low"},
                    })
            elif fotos:
                logger.warning(
                    "pricing.appraise() received photos that are not base64 data URIs — ignored. "
                    "Use download_media_as_base64() before calling appraise()."
                )

        user_content.append({
            "type": "text",
            "text": "\n".join(context_parts) + "\n\nAvalie este produto e retorne o JSON de precificação.",
        })

        try:
            resp = await get_provider().complete(
                messages=[
                    {"role": "system", "content": PRICING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=600,
                json_mode=True,
            )
            raw = resp.text or "{}"
            result = json.loads(raw)
            result["fontes"] = list(set(result.get("fontes", []) + sources_used))

            if preco_informado_vendedor and result.get("preco_sugerido"):
                diff_pct = abs(preco_informado_vendedor - result["preco_sugerido"]) / result["preco_sugerido"]
                result["alerta_preco_vendedor"] = diff_pct > 0.20

            return result

        except Exception as e:
            logger.error(f"Error in PricingAgent.appraise: {e}")
            fallback_price = preco_informado_vendedor or 0
            return {
                "preco_sugerido": fallback_price,
                "preco_minimo_sugerido": round(fallback_price * 0.80, 2),
                "justificativa": "Avaliação automática indisponível. Usando referência informada pelo vendedor.",
                "confianca": "baixa",
                "fontes": ["descricao_textual"],
                "alerta": None,
                "alerta_preco_vendedor": False,
            }

    async def web_search_price(self, query: str) -> str | None:
        """Searches market price via web search (uses DDGS if available)."""
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(f"preço {query} usado Brasil site:olx.com.br OR site:mercadolivre.com.br", max_results=5))
                if results:
                    snippets = [r.get("body", "") for r in results if r.get("body")]
                    return " | ".join(snippets[:4])
        except Exception as e:
            logger.debug(f"Web search unavailable: {e}")
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
