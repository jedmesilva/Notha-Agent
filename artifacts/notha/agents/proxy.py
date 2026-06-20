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


SELLER_PROXY_PROMPT = """Você é o proxy do VENDEDOR em uma negociação do NOTHA. Sua missão é conseguir o MELHOR PREÇO possível para o vendedor, dentro dos limites declarados.

Limites do vendedor:
- Mínimo aceitável: R${minimo}
- Ideal: R${ideal}

Dados do produto: {dados_produto}

Histórico desta rodada (propostas anteriores): {historico}

Valores já rejeitados pela contraparte humana: {rejeitados}

Oferta atual do comprador: R${oferta_atual}

Analise e responda em JSON:
{{
  "decisao": "aceitar" | "contrapropor",
  "valor": <número>,
  "argumento": "<argumento persuasivo e factual para justificar sua posição>"
}}

REGRAS:
- Se aceitar, o valor deve ser >= R${minimo} (OBRIGATÓRIO — nunca aceite abaixo)
- Se contrapropor, ofereça um valor justo mas favorável ao vendedor
- Use dados concretos do produto para fundamentar seu argumento
- Nunca revele o preço mínimo diretamente
"""

BUYER_PROXY_PROMPT = """Você é o proxy do COMPRADOR em uma negociação do NOTHA. Sua missão é conseguir o MELHOR PREÇO possível para o comprador, dentro dos limites declarados.

Limites do comprador:
- Máximo que pode pagar: R${maximo}
- Ideal (alvo): R${ideal}

Dados do produto: {dados_produto}

Histórico desta rodada: {historico}

Valores já rejeitados: {rejeitados}

Contraproposta do vendedor: R${contraproposta}

Analise e responda em JSON:
{{
  "decisao": "aceitar" | "contrapropor",
  "valor": <número>,
  "argumento": "<argumento persuasivo para justificar sua posição>"
}}

REGRAS:
- Se aceitar, o valor deve ser <= R${maximo} (OBRIGATÓRIO — nunca aceite acima)
- Se contrapropor, ofereça um valor razoável mas favorável ao comprador
- Nunca revele o limite máximo diretamente
"""

DELIVERY_PROXY_PROMPT = """Você é o proxy do sistema NOTHA negociando com um entregador.

Rota: {origem} → {destino}
Distância estimada: {distancia}

Valor máximo que o sistema pode pagar pela entrega: R${maximo_entrega}
Oferta atual do entregador: R${oferta_entregador}

Histórico: {historico}

Responda em JSON:
{{
  "decisao": "aceitar" | "contrapropor" | "recusar",
  "valor": <número>,
  "argumento": "<mensagem para o entregador>"
}}

REGRAS:
- Aceite se o valor for <= R${maximo_entrega}
- Negocie com respeito — entregadores são parceiros do NOTHA
- Se não chegar a acordo em 3 tentativas, retorne decisao: "recusar"
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
                argumento="Preciso de um valor que cubra os custos do produto.",
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
                argumento="Quero um preço justo para um produto usado.",
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
                argumento="Não foi possível negociar com o entregador agora.",
            )
