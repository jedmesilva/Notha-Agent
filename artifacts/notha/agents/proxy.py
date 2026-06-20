"""
Buyer Proxy Agent, Seller Proxy Agent e Delivery Proxy Agent.

Cada proxy representa um lado da negociação e busca o melhor valor dentro dos
limites declarados pelo humano que representa.

GUARD RAIL obrigatório (código, não LLM): toda saída de proxy é validada antes
de ser usada. Vendedor não pode aceitar abaixo do mínimo, comprador não pode
oferecer acima do máximo.
"""
import json
import logging
import os
from dataclasses import dataclass
from openai import AsyncOpenAI
from config import OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger("notha.agent.proxy")


class ValorForaDosLimites(Exception):
    pass


@dataclass
class ProxyResponse:
    decisao: str
    valor: float
    argumento: str


def _make_client() -> AsyncOpenAI:
    if OPENAI_API_KEY:
        return AsyncOpenAI(api_key=OPENAI_API_KEY)
    api_key = os.environ.get("OPENAI_API_KEY", "nokey")
    return AsyncOpenAI(api_key=api_key, base_url=OPENAI_BASE_URL)


SELLER_PROXY_PROMPT = """Você representa o VENDEDOR em uma negociação automatizada do NOTHA.

━━━ SUA MISSÃO ━━━
Conseguir o melhor preço possível para o vendedor — sem revelar os limites, sem aceitar abaixo do mínimo, sem hostilidade desnecessária.

━━━ LIMITES DO VENDEDOR (confidencial — nunca revelar) ━━━
Preço mínimo aceitável: R${minimo}
Preço ideal (alvo): R${ideal}

━━━ DADOS DO PRODUTO ━━━
{dados_produto}

━━━ HISTÓRICO DESTA NEGOCIAÇÃO ━━━
{historico}

━━━ VALORES JÁ REJEITADOS PELO COMPRADOR ━━━
{rejeitados}

━━━ OFERTA ATUAL DO COMPRADOR ━━━
R${oferta_atual}

━━━ ESTRATÉGIA DE NEGOCIAÇÃO ━━━
1. Se a oferta for >= R${ideal}: aceite imediatamente — ótimo negócio para o vendedor
2. Se a oferta for >= R${minimo} mas abaixo do ideal: avalie o histórico
   - Se já houve muitas rodadas (3+): aceite para fechar logo
   - Se é início da negociação: contraproponha perto do ideal, reduzindo gradualmente
3. Se a oferta for < R${minimo}: NUNCA aceite — contraproponha firmemente
4. Nas contrapropostas: reduza o valor em parcelas razoáveis (não ceda tudo de uma vez)
5. Argumento: use características concretas do produto (estado, acessórios, raridade) — não invente

━━━ REGRAS ABSOLUTAS ━━━
- decisao "aceitar" SOMENTE com valor >= R${minimo}
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
Valor máximo que pode pagar: R${maximo}
Preço ideal (alvo): R${ideal}

━━━ DADOS DO PRODUTO ━━━
{dados_produto}

━━━ HISTÓRICO DESTA NEGOCIAÇÃO ━━━
{historico}

━━━ VALORES JÁ REJEITADOS PELO VENDEDOR ━━━
{rejeitados}

━━━ CONTRAPROPOSTA ATUAL DO VENDEDOR ━━━
R${contraproposta}

━━━ ESTRATÉGIA DE NEGOCIAÇÃO ━━━
1. Se a contraproposta for <= R${ideal}: aceite imediatamente — excelente negócio para o comprador
2. Se a contraproposta for <= R${maximo} mas acima do ideal: avalie o histórico
   - Se já houve muitas rodadas (3+): aceite para fechar logo
   - Se é início: ofereça um valor intermediário, subindo gradualmente
3. Se a contraproposta for > R${maximo}: NUNCA aceite — contraproponha abaixo do máximo
4. Nas contrapropostas: suba o valor em parcelas moderadas (demonstre interesse mas não ansiedade)
5. Argumento: mencione estado do produto, comparação de mercado, condições de pagamento (Pix imediato)

━━━ REGRAS ABSOLUTAS ━━━
- decisao "aceitar" SOMENTE com valor <= R${maximo}
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
Origem: {origem}
Destino: {destino}
Distância estimada: {distancia}
Valor máximo que o NOTHA pode pagar: R${maximo_entrega}
Oferta atual do entregador: R${oferta_entregador}

━━━ HISTÓRICO DAS RODADAS ━━━
{historico}

━━━ ESTRATÉGIA ━━━
1. Se a oferta for <= R${maximo_entrega}: aceite — frete dentro do orçamento
2. Se a oferta for até 20% acima do máximo: tente negociar (contraproponha R${maximo_entrega})
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


def _validate_proxy_response(resposta: ProxyResponse, limite: dict, eh_vendedor: bool) -> None:
    if eh_vendedor:
        minimo = limite.get("minimo", 0)
        if resposta.decisao == "aceitar" and resposta.valor < minimo:
            raise ValorForaDosLimites(
                f"Vendedor não pode aceitar R${resposta.valor:.2f} abaixo do mínimo R${minimo:.2f}"
            )
    else:
        maximo = limite.get("maximo", float("inf"))
        if resposta.decisao == "aceitar" and resposta.valor > maximo:
            raise ValorForaDosLimites(
                f"Comprador não pode oferecer R${resposta.valor:.2f} acima do máximo R${maximo:.2f}"
            )


class SellerProxyAgent:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = _make_client()
        return self._client

    async def avaliar(
        self,
        oferta_recebida: float,
        limite: dict,
        dados_produto: dict,
        historico: list,
        rejeitados: list[float] | None = None,
    ) -> ProxyResponse:
        prompt = SELLER_PROXY_PROMPT.format(
            minimo=limite.get("minimo", 0),
            ideal=limite.get("ideal", 0),
            dados_produto=json.dumps(dados_produto, ensure_ascii=False),
            historico=json.dumps(historico, ensure_ascii=False),
            rejeitados=json.dumps(rejeitados or []),
            oferta_atual=oferta_recebida,
        )
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            result = ProxyResponse(
                decisao=data.get("decisao", "contrapropor"),
                valor=float(data.get("valor", oferta_recebida)),
                argumento=data.get("argumento", ""),
            )
            _validate_proxy_response(result, limite, eh_vendedor=True)
            return result
        except ValorForaDosLimites:
            raise
        except Exception as e:
            logger.error(f"Erro no SellerProxyAgent: {e}")
            return ProxyResponse(
                decisao="contrapropor",
                valor=max(oferta_recebida, limite.get("minimo", oferta_recebida)),
                argumento="O produto está em ótimo estado e vale o preço pedido.",
            )


class BuyerProxyAgent:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = _make_client()
        return self._client

    async def avaliar(
        self,
        contraproposta: float,
        limite: dict,
        dados_produto: dict,
        historico: list,
        rejeitados: list[float] | None = None,
    ) -> ProxyResponse:
        prompt = BUYER_PROXY_PROMPT.format(
            maximo=limite.get("maximo", float("inf")),
            ideal=limite.get("ideal", 0),
            dados_produto=json.dumps(dados_produto, ensure_ascii=False),
            historico=json.dumps(historico, ensure_ascii=False),
            rejeitados=json.dumps(rejeitados or []),
            contraproposta=contraproposta,
        )
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            result = ProxyResponse(
                decisao=data.get("decisao", "contrapropor"),
                valor=float(data.get("valor", contraproposta)),
                argumento=data.get("argumento", ""),
            )
            _validate_proxy_response(result, limite, eh_vendedor=False)
            return result
        except ValorForaDosLimites:
            raise
        except Exception as e:
            logger.error(f"Erro no BuyerProxyAgent: {e}")
            return ProxyResponse(
                decisao="contrapropor",
                valor=min(contraproposta, limite.get("maximo", contraproposta)),
                argumento="Estou pagando via Pix na hora — pode ser esse valor?",
            )


class DeliveryProxyAgent:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = _make_client()
        return self._client

    async def negociar(
        self,
        origem: str,
        destino: str,
        maximo_entrega: float,
        oferta_entregador: float,
        historico: list | None = None,
        distancia: str = "não informada",
    ) -> ProxyResponse:
        prompt = DELIVERY_PROXY_PROMPT.format(
            origem=origem,
            destino=destino,
            distancia=distancia,
            maximo_entrega=maximo_entrega,
            oferta_entregador=oferta_entregador,
            historico=json.dumps(historico or []),
        )
        try:
            resp = await self._get_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return ProxyResponse(
                decisao=data.get("decisao", "recusar"),
                valor=float(data.get("valor", oferta_entregador)),
                argumento=data.get("argumento", ""),
            )
        except Exception as e:
            logger.error(f"Erro no DeliveryProxyAgent: {e}")
            return ProxyResponse(
                decisao="recusar",
                valor=0,
                argumento="Não foi possível negociar agora. Vamos tentar outro entregador.",
            )
