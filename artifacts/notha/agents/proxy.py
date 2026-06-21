"""
Buyer Proxy Agent, Seller Proxy Agent, and Delivery Proxy Agent.

Each proxy represents one side of the negotiation and pursues the best value
within the limits declared by the human it represents.

MANDATORY GUARD RAIL (code, not LLM): every proxy output is validated before use.
The seller cannot accept below the minimum; the buyer cannot offer above the maximum.
"""
import json
import logging
from dataclasses import dataclass
from llm import get_provider

logger = logging.getLogger("notha.agent.proxy")


class PriceLimitExceeded(Exception):
    pass


@dataclass
class ProxyResponse:
    decision: str
    value: float
    argument: str


SELLER_PROXY_PROMPT = """Você representa o VENDEDOR em uma negociação automatizada do NOTHA.

━━━ SUA MISSÃO ━━━
Conseguir o melhor preço possível para o vendedor — sem revelar os limites, sem aceitar abaixo do mínimo, sem hostilidade desnecessária.

━━━ LIMITES DO VENDEDOR (confidencial — nunca revelar) ━━━
Preço mínimo aceitável: R${minimum}
Preço ideal (alvo): R${target}

━━━ DADOS DO PRODUTO ━━━
{product_data}

━━━ HISTÓRICO DESTA NEGOCIAÇÃO ━━━
{history}

━━━ VALORES JÁ REJEITADOS PELO COMPRADOR ━━━
{rejected}

━━━ OFERTA ATUAL DO COMPRADOR ━━━
R${current_offer}

━━━ ESTRATÉGIA DE NEGOCIAÇÃO ━━━
1. Se a oferta for >= R${target}: aceite imediatamente — ótimo negócio para o vendedor
2. Se a oferta for >= R${minimum} mas abaixo do ideal: avalie o histórico
   - Se já houve muitas rodadas (3+): aceite para fechar logo
   - Se é início da negociação: contraproponha perto do ideal, reduzindo gradualmente
3. Se a oferta for < R${minimum}: NUNCA aceite — contraproponha firmemente
4. Nas contrapropostas: reduza o valor em parcelas razoáveis (não ceda tudo de uma vez)
5. Argumento: use características concretas do produto (estado, acessórios, raridade) — não invente

━━━ REGRAS ABSOLUTAS ━━━
- decisao "aceitar" SOMENTE com valor >= R${minimum}
- Nunca mencione o preço mínimo ou o limite do vendedor
- Seja firme mas educado — o tom é de negociação respeitosa entre adultos
- Se a contraproposta for menor que a oferta anterior do comprador, sinalize incoerência e mantenha posição

Responda SOMENTE com JSON válido:
{{
  "decisao": "aceitar" | "contrapropor",
  "valor": <número>,
  "argumento": "<argumento persuasivo, específico, em português — máximo 2 frases>"
}}
"""

BUYER_PROXY_PROMPT = """Você representa o COMPRADOR em uma negociação automatizada do NOTHA.

━━━ SUA MISSÃO ━━━
Conseguir o melhor preço possível para o comprador — sem revelar os limites, sem pagar acima do máximo, sem desrespeitar o vendedor.

━━━ LIMITES DO COMPRADOR (confidencial — nunca revelar) ━━━
Valor máximo que pode pagar: R${maximum}
Preço ideal (alvo): R${target}

━━━ DADOS DO PRODUTO ━━━
{product_data}

━━━ HISTÓRICO DESTA NEGOCIAÇÃO ━━━
{history}

━━━ VALORES JÁ REJEITADOS PELO VENDEDOR ━━━
{rejected}

━━━ CONTRAPROPOSTA ATUAL DO VENDEDOR ━━━
R${counteroffer}

━━━ ESTRATÉGIA DE NEGOCIAÇÃO ━━━
1. Se a contraproposta for <= R${target}: aceite imediatamente — excelente negócio para o comprador
2. Se a contraproposta for <= R${maximum} mas acima do ideal: avalie o histórico
   - Se já houve muitas rodadas (3+): aceite para fechar logo
   - Se é início: ofereça um valor intermediário, subindo gradualmente
3. Se a contraproposta for > R${maximum}: NUNCA aceite — contraproponha abaixo do máximo
4. Nas contrapropostas: suba o valor em parcelas moderadas (demonstre interesse mas não ansiedade)
5. Argumento: mencione estado do produto, comparação de mercado, condições de pagamento (Pix imediato)

━━━ REGRAS ABSOLUTAS ━━━
- decisao "aceitar" SOMENTE com valor <= R${maximum}
- Nunca mencione o valor máximo ou o limite do comprador
- Tom de quem quer comprar mas tem outras opções — sem desespero, sem agressividade
- Valorize a facilidade do processo (Pix seguro, entrega garantida pelo NOTHA)

Responda SOMENTE com JSON válido:
{{
  "decisao": "aceitar" | "contrapropor",
  "valor": <número>,
  "argumento": "<argumento persuasivo, específico, em português — máximo 2 frases>"
}}
"""

DELIVERY_PROXY_PROMPT = """Você é o sistema NOTHA negociando o valor do frete com um entregador parceiro.

━━━ CONTEXTO DA ENTREGA ━━━
Origem: {origin}
Destino: {destination}
Distância estimada: {distance}
Valor máximo que o NOTHA pode pagar: R${max_delivery}
Oferta atual do entregador: R${courier_offer}

━━━ HISTÓRICO DAS RODADAS ━━━
{history}

━━━ ESTRATÉGIA ━━━
1. Se a oferta for <= R${max_delivery}: aceite — frete dentro do orçamento
2. Se a oferta for até 20% acima do máximo: tente negociar (contraproponha R${max_delivery})
3. Se a oferta for muito acima (>20% do máximo): recuse educadamente e libere para o próximo entregador
4. Após 3 rodadas sem acordo: recuse e encerre — não vale mais negociar
5. Tom: parceiro de negócios — entregadores são fundamentais para o NOTHA funcionar

━━━ SOBRE O CONTEXTO NOTHA ━━━
- O pagamento ao entregador é feito via Pix imediatamente após confirmação de entrega
- O entregador é responsável pela segurança do produto durante o transporte
- O NOTHA não tem subcontratação — cada entrega é tratada individualmente

Responda SOMENTE com JSON válido:
{{
  "decisao": "aceitar" | "contrapropor" | "recusar",
  "valor": <número>,
  "argumento": "<mensagem direta para o entregador, em português — máximo 2 frases>"
}}
"""


def _validate_proxy_response(response: ProxyResponse, limits: dict, is_seller: bool) -> None:
    if is_seller:
        minimum = limits.get("minimo", 0)
        if response.decision == "aceitar" and response.value < minimum:
            raise PriceLimitExceeded(
                f"Seller cannot accept R${response.value:.2f} below minimum R${minimum:.2f}"
            )
    else:
        maximum = limits.get("maximo", float("inf"))
        if response.decision == "aceitar" and response.value > maximum:
            raise PriceLimitExceeded(
                f"Buyer cannot offer R${response.value:.2f} above maximum R${maximum:.2f}"
            )


class SellerProxyAgent:
    async def evaluate(
        self,
        received_offer: float,
        limits: dict,
        product_data: dict,
        history: list,
        rejected: list[float] | None = None,
    ) -> ProxyResponse:
        prompt = SELLER_PROXY_PROMPT.format(
            minimum=limits.get("minimo", 0),
            target=limits.get("ideal", 0),
            product_data=json.dumps(product_data, ensure_ascii=False),
            history=json.dumps(history, ensure_ascii=False),
            rejected=json.dumps(rejected or []),
            current_offer=received_offer,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            result = ProxyResponse(
                decision=data.get("decisao", "contrapropor"),
                value=float(data.get("valor", received_offer)),
                argument=data.get("argumento", ""),
            )
            _validate_proxy_response(result, limits, is_seller=True)
            return result
        except PriceLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Error in SellerProxyAgent: {e}")
            return ProxyResponse(
                decision="contrapropor",
                value=max(received_offer, limits.get("minimo", received_offer)),
                argument="O produto está em ótimo estado e vale o preço pedido.",
            )


class BuyerProxyAgent:
    async def evaluate(
        self,
        counteroffer: float,
        limits: dict,
        product_data: dict,
        history: list,
        rejected: list[float] | None = None,
    ) -> ProxyResponse:
        prompt = BUYER_PROXY_PROMPT.format(
            maximum=limits.get("maximo", float("inf")),
            target=limits.get("ideal", 0),
            product_data=json.dumps(product_data, ensure_ascii=False),
            history=json.dumps(history, ensure_ascii=False),
            rejected=json.dumps(rejected or []),
            counteroffer=counteroffer,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            result = ProxyResponse(
                decision=data.get("decisao", "contrapropor"),
                value=float(data.get("valor", counteroffer)),
                argument=data.get("argumento", ""),
            )
            _validate_proxy_response(result, limits, is_seller=False)
            return result
        except PriceLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Error in BuyerProxyAgent: {e}")
            return ProxyResponse(
                decision="contrapropor",
                value=min(counteroffer, limits.get("maximo", counteroffer)),
                argument="Estou pagando via Pix na hora — pode ser esse valor?",
            )


class DeliveryProxyAgent:
    async def negotiate(
        self,
        origin: str,
        destination: str,
        max_delivery: float,
        courier_offer: float,
        history: list | None = None,
        distance: str = "não informada",
    ) -> ProxyResponse:
        prompt = DELIVERY_PROXY_PROMPT.format(
            origin=origin,
            destination=destination,
            distance=distance,
            max_delivery=max_delivery,
            courier_offer=courier_offer,
            history=json.dumps(history or []),
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            return ProxyResponse(
                decision=data.get("decisao", "recusar"),
                value=float(data.get("valor", courier_offer)),
                argument=data.get("argumento", ""),
            )
        except Exception as e:
            logger.error(f"Error in DeliveryProxyAgent: {e}")
            return ProxyResponse(
                decision="recusar",
                value=0,
                argument="Não foi possível negociar agora. Vamos tentar outro entregador.",
            )
