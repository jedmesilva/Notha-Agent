"""
Pricing/Appraisal Agent — forma sugestão de preço e preço mínimo no cadastro do produto.

Roda UMA VEZ por listing, de forma assíncrona.
Consulta: mercado externo (web search), histórico interno (SQL), avaliação visual (vision LLM).
Saída estruturada: preco_sugerido, preco_minimo_sugerido, justificativa, confianca, fontes.
"""
import json
import logging
import os
from openai import AsyncOpenAI
from config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL

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
            precos = [
                h.get("preco_final", h.get("preco_anunciado", 0))
                for h in historico_similares
                if h.get("preco_final") or h.get("preco_anunciado")
            ]
            if precos:
                media = sum(precos) / len(precos)
                minimo = min(precos)
                maximo = max(precos)
                contexto_parts.append(
                    f"Histórico de {len(precos)} vendas similares no NOTHA: "
                    f"média R${media:.0f}, mínimo R${minimo:.0f}, máximo R${maximo:.0f}. "
                    f"Valores: {[f'R${p:.0f}' for p in precos]}"
                )

        contexto_parts.append("Descrição textual analisada.")
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
            "text": "\n".join(contexto_parts) + "\n\nAvalie este produto e retorne o JSON de precificação.",
        })

        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": PRICING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=600,
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
                "preco_minimo_sugerido": round(fallback * 0.80, 2),
                "justificativa": "Avaliação automática indisponível. Usando referência informada pelo vendedor.",
                "confianca": "baixa",
                "fontes": ["descricao_textual"],
                "alerta": None,
                "alerta_preco_vendedor": False,
            }

    async def web_search_price(self, query: str) -> str | None:
        """Busca preço de mercado via web search (usa DDGS se disponível)."""
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(f"preço {query} usado Brasil site:olx.com.br OR site:mercadolivre.com.br", max_results=5))
                if results:
                    snippets = [r.get("body", "") for r in results if r.get("body")]
                    return " | ".join(snippets[:4])
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
