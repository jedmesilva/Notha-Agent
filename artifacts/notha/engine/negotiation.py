"""
Negotiation Engine — decide se uma oferta é aceita, recusada ou gera contraproposta.

É código DETERMINÍSTICO, não LLM. Mesma entrada sempre produz mesma saída.
LLM (Contextual Evaluator) entra apenas em casos-limite, mas com teto fixo em código.
"""
import logging
from openai import AsyncOpenAI
from config import (
    OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL,
    AJUSTE_MAXIMO_PERMITIDO, MAX_RODADAS_PROXY, MAX_TENTATIVAS_HUMANAS,
)
from db.connection import DB
from db.repositories import NegotiationRepository, ListingRepository, TransactionRepository
from agents.proxy import BuyerProxyAgent, SellerProxyAgent, ValorForaDosLimites
from asaas import AsaasClient

logger = logging.getLogger("notha.engine.negotiation")


def _make_client() -> AsyncOpenAI:
    if OPENAI_API_KEY:
        return AsyncOpenAI(api_key=OPENAI_API_KEY)
    return AsyncOpenAI(base_url=OPENAI_BASE_URL)


class NegotiationEngine:
    def __init__(self, db: DB, whatsapp_sender=None):
        self._db = db
        self._neg_repo = NegotiationRepository(db)
        self._listing_repo = ListingRepository(db)
        self._tx_repo = TransactionRepository(db)
        self._buyer_proxy = BuyerProxyAgent()
        self._seller_proxy = SellerProxyAgent()
        self._asaas = AsaasClient()
        self._sender = whatsapp_sender

    def _decidir_oferta_direta(
        self, oferta: float, preco_minimo: float, contexto_extra: str | None = None
    ) -> str:
        """Lógica determinística para modo de negociação direta."""
        if oferta >= preco_minimo:
            return "aceitar"
        if oferta >= preco_minimo * 0.9 and not contexto_extra:
            return "contrapropor"
        if contexto_extra:
            return "avaliar_contexto"
        return "recusar"

    async def _contextual_evaluator(
        self, oferta: float, preco_minimo: float, contexto_extra: str
    ) -> float:
        """
        LLM avalia fatores não-numéricos (retirada imediata, à vista, volume).
        Retorna ajuste sugerido — mas código aplica teto fixo antes de qualquer ação.
        """
        client = _make_client()
        prompt = f"""
        Um comprador ofereceu R${oferta:.2f} por um produto com preço mínimo de R${preco_minimo:.2f}.
        Contexto adicional fornecido pelo comprador: "{contexto_extra}"
        
        Que ajuste percentual de desconto (negativo) seria razoável dado o contexto? Fatores válidos:
        - Retirada imediata (hoje)
        - Pagamento instantâneo à vista
        - Compra de múltiplas unidades
        
        Responda SOMENTE com um número decimal entre 0 e 0.15 (ex: 0.05 para 5% de desconto).
        Se o contexto não justificar desconto algum, responda 0.
        """
        try:
            resp = await client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=20,
            )
            raw = resp.choices[0].message.content.strip()
            ajuste = float(raw)
            teto = abs(AJUSTE_MAXIMO_PERMITIDO)
            return min(ajuste, teto)
        except Exception as e:
            logger.error(f"Contextual evaluator falhou: {e}")
            return 0.0

    async def decidir_oferta(
        self,
        negotiation_id: int,
        oferta: float,
        preco_minimo: float,
        contexto_extra: str | None = None,
    ) -> dict:
        """Modo direto: avalia oferta e retorna decisão com valor."""
        decisao = self._decidir_oferta_direta(oferta, preco_minimo, contexto_extra)

        if decisao == "aceitar":
            return {"decisao": "aceitar", "valor": oferta}

        if decisao == "contrapropor":
            contraproposta = round((oferta + preco_minimo) / 2, 2)
            return {"decisao": "contrapropor", "valor": contraproposta}

        if decisao == "avaliar_contexto" and contexto_extra:
            ajuste = await self._contextual_evaluator(oferta, preco_minimo, contexto_extra)
            preco_ajustado = preco_minimo * (1 - ajuste)
            if oferta >= preco_ajustado:
                return {"decisao": "aceitar_com_justificativa", "valor": oferta, "ajuste_aplicado": ajuste}
            return {"decisao": "recusar", "valor": preco_minimo}

        return {"decisao": "recusar", "valor": preco_minimo}

    def _fallback_interseccao(self, comprador_max: float, vendedor_min: float) -> float | None:
        if comprador_max < vendedor_min:
            return None
        return round((comprador_max + vendedor_min) / 2, 2)

    async def negociar_entre_proxies(self, negotiation_id: int) -> dict:
        """
        Roda até MAX_RODADAS_PROXY entre os proxies.
        Se não convergirem, aplica fallback matemático.
        """
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "erro", "motivo": "negociacao_nao_encontrada"}

        listing = await self._listing_repo.find_by_id(neg["listing_id"])
        if not listing:
            return {"status": "erro", "motivo": "listing_nao_encontrado"}

        import json
        limite_comprador = json.loads(neg["limite_comprador"]) if neg["limite_comprador"] else {}
        limite_vendedor = json.loads(neg["limite_vendedor"]) if neg["limite_vendedor"] else {
            "minimo": listing["preco_minimo"],
            "ideal": listing["preco_anunciado"],
        }
        appraisal_data = json.loads(listing["appraisal_data"]) if listing["appraisal_data"] else {}

        historico = []
        rejeitados = await self._neg_repo.get_rejected_values(negotiation_id)
        oferta_atual = limite_comprador.get("ideal", listing["preco_anunciado"] * 0.9)

        rodadas_existentes = await self._neg_repo.get_proxy_rounds(negotiation_id)
        rodada_inicio = len(rodadas_existentes)

        for i in range(MAX_RODADAS_PROXY):
            rodada_num = rodada_inicio + i + 1

            try:
                resp_vendedor = await self._seller_proxy.avaliar(
                    oferta_recebida=oferta_atual,
                    limite=limite_vendedor,
                    dados_produto=appraisal_data,
                    historico=historico,
                    rejeitados=rejeitados,
                )
            except ValorForaDosLimites as e:
                logger.error(f"Guard rail vendedor violado: {e}")
                break

            if resp_vendedor.decisao == "aceitar":
                await self._neg_repo.add_proxy_round(
                    negotiation_id, rodada_num, oferta_atual,
                    argumento_vendedor=f"ACEITO: {resp_vendedor.argumento}",
                )
                return await self._propor_aos_humanos(negotiation_id, oferta_atual)

            try:
                resp_comprador = await self._buyer_proxy.avaliar(
                    contraproposta=resp_vendedor.valor,
                    limite=limite_comprador,
                    dados_produto=appraisal_data,
                    historico=historico,
                    rejeitados=rejeitados,
                )
            except ValorForaDosLimites as e:
                logger.error(f"Guard rail comprador violado: {e}")
                break

            await self._neg_repo.add_proxy_round(
                negotiation_id, rodada_num,
                valor_proposto=resp_vendedor.valor,
                argumento_vendedor=resp_vendedor.argumento,
                argumento_comprador=resp_comprador.argumento,
            )

            historico.append({
                "rodada": rodada_num,
                "valor_vendedor": resp_vendedor.valor,
                "arg_vendedor": resp_vendedor.argumento,
                "valor_comprador": resp_comprador.valor,
                "arg_comprador": resp_comprador.argumento,
            })

            if resp_comprador.decisao == "aceitar":
                return await self._propor_aos_humanos(negotiation_id, resp_vendedor.valor)

            oferta_atual = resp_comprador.valor

        valor_fallback = self._fallback_interseccao(
            limite_comprador.get("maximo", 0),
            limite_vendedor.get("minimo", listing["preco_minimo"]),
        )
        if valor_fallback is None:
            await self._neg_repo.set_status(negotiation_id, "sem_acordo")
            logger.info(f"Negotiation {negotiation_id}: sem_acordo (fallback impossível)")
            return {"status": "sem_acordo"}

        return await self._propor_aos_humanos(negotiation_id, valor_fallback)

    async def _propor_aos_humanos(self, negotiation_id: int, valor: float) -> dict:
        """Propõe valor ao vendedor e depois ao comprador, sequencialmente."""
        import json
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "erro"}

        tentativas = neg["tentativas_humanas"] or 0
        if tentativas >= MAX_TENTATIVAS_HUMANAS:
            await self._neg_repo.set_status(negotiation_id, "sem_acordo")
            return {"status": "sem_acordo", "motivo": "max_tentativas_atingido"}

        await self._neg_repo.update_offer(negotiation_id, valor, status="proposta_ao_vendedor")

        listing = await self._listing_repo.find_by_id(neg["listing_id"])
        seller_id = listing["seller_id"]

        await self._notify(
            seller_id,
            f"proposta_ao_vendedor",
            {"valor": valor, "negotiation_id": negotiation_id},
        )

        logger.info(f"Negotiation {negotiation_id}: proposta R${valor:.2f} enviada ao vendedor {seller_id}")
        return {"status": "proposta_ao_vendedor", "valor": valor, "negotiation_id": negotiation_id}

    async def aceitar_proposta_vendedor(self, negotiation_id: int) -> dict:
        """Chamado quando o vendedor confirma a proposta."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "erro"}

        valor = neg["preco_atual_proposto"]
        await self._neg_repo.set_status(negotiation_id, "proposta_ao_comprador")
        await self._notify(
            neg["buyer_id"],
            "proposta_ao_comprador",
            {"valor": valor, "negotiation_id": negotiation_id},
        )
        return {"status": "proposta_ao_comprador", "valor": valor}

    async def recusar_proposta_vendedor(self, negotiation_id: int) -> dict:
        """Vendedor recusou — registra rejeição e reinicia proxies."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "erro"}
        valor = neg["preco_atual_proposto"]
        rounds = await self._neg_repo.get_proxy_rounds(negotiation_id)
        if rounds:
            await self._neg_repo.confirm_proxy_round(rounds[-1]["id"], confirmado_pelo_vendedor=False)
        await self._neg_repo.set_status(negotiation_id, "ativa")
        logger.info(f"Negotiation {negotiation_id}: vendedor recusou R${valor:.2f}, reiniciando proxies")
        return await self.negociar_entre_proxies(negotiation_id)

    async def aceitar_proposta_comprador(self, negotiation_id: int) -> dict:
        """Comprador aceita — negociação fechada, dispara cobrança."""
        await self._neg_repo.set_status(negotiation_id, "aceita")
        logger.info(f"Negotiation {negotiation_id}: ACEITA por ambos os lados")
        return {"status": "aceita", "negotiation_id": negotiation_id}

    async def recusar_proposta_comprador(self, negotiation_id: int) -> dict:
        """Comprador recusou — registra rejeição e reinicia proxies."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "erro"}
        rounds = await self._neg_repo.get_proxy_rounds(negotiation_id)
        if rounds:
            await self._neg_repo.confirm_proxy_round(rounds[-1]["id"], confirmado_pelo_comprador=False)
        await self._neg_repo.set_status(negotiation_id, "ativa")
        return await self.negociar_entre_proxies(negotiation_id)

    async def _notify(self, user_id: int, event: str, data: dict) -> None:
        """Placeholder para notificação ao usuário via WhatsApp."""
        logger.info(f"[NOTIFY] user_id={user_id} event={event} data={data}")
