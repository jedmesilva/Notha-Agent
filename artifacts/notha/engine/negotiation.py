"""
Negotiation Engine — decides whether an offer is accepted, rejected, or generates a counteroffer.

This is DETERMINISTIC code, not LLM. Same input always produces the same output.
LLM (Contextual Evaluator) is used only for edge cases, but with a fixed cap enforced in code.
"""
import logging
from openai import AsyncOpenAI
from config import (
    OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL,
    MAX_ALLOWED_ADJUSTMENT, MAX_PROXY_ROUNDS, MAX_HUMAN_ATTEMPTS,
)
from db.connection import DB
from db.repositories import NegotiationRepository, ListingRepository, TransactionRepository
from agents.proxy import BuyerProxyAgent, SellerProxyAgent, PriceLimitExceeded
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

    def _evaluate_direct_offer(
        self, offer: float, min_price: float, extra_context: str | None = None
    ) -> str:
        """Deterministic logic for direct negotiation mode."""
        if offer >= min_price:
            return "accept"
        if offer >= min_price * 0.9 and not extra_context:
            return "counter"
        if extra_context:
            return "evaluate_context"
        return "reject"

    async def _contextual_evaluator(
        self, offer: float, min_price: float, extra_context: str
    ) -> float:
        """
        LLM evaluates non-numeric factors (immediate pickup, cash payment, bulk purchase).
        Returns suggested adjustment — but code enforces a hard cap before any action.
        """
        client = _make_client()
        prompt = f"""
        Um comprador ofereceu R${offer:.2f} por um produto com preço mínimo de R${min_price:.2f}.
        Contexto adicional fornecido pelo comprador: "{extra_context}"
        
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
            adjustment = float(raw)
            cap = abs(MAX_ALLOWED_ADJUSTMENT)
            return min(adjustment, cap)
        except Exception as e:
            logger.error(f"Contextual evaluator failed: {e}")
            return 0.0

    async def decide_offer(
        self,
        negotiation_id: int,
        offer: float,
        min_price: float,
        extra_context: str | None = None,
    ) -> dict:
        """Direct mode: evaluates an offer and returns a decision with value."""
        decision = self._evaluate_direct_offer(offer, min_price, extra_context)

        if decision == "accept":
            return {"decision": "accept", "value": offer}

        if decision == "counter":
            counteroffer = round((offer + min_price) / 2, 2)
            return {"decision": "counter", "value": counteroffer}

        if decision == "evaluate_context" and extra_context:
            adjustment = await self._contextual_evaluator(offer, min_price, extra_context)
            adjusted_price = min_price * (1 - adjustment)
            if offer >= adjusted_price:
                return {"decision": "accept_with_context", "value": offer, "adjustment_applied": adjustment}
            return {"decision": "reject", "value": min_price}

        return {"decision": "reject", "value": min_price}

    # Legacy alias
    async def decidir_oferta(self, negotiation_id, offer, min_price, extra_context=None):
        result = await self.decide_offer(negotiation_id, offer, min_price, extra_context)
        return {
            "decisao": result.get("decision"),
            "valor": result.get("value"),
            **{k: v for k, v in result.items() if k not in ("decision", "value")},
        }

    def _fallback_intersection(self, buyer_max: float, seller_min: float) -> float | None:
        if buyer_max < seller_min:
            return None
        return round((buyer_max + seller_min) / 2, 2)

    async def negotiate_between_proxies(self, negotiation_id: int) -> dict:
        """
        Runs up to MAX_PROXY_ROUNDS between the proxies.
        If they don't converge, applies a mathematical fallback.
        """
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "error", "reason": "negotiation_not_found"}

        listing = await self._listing_repo.find_by_id(neg["listing_id"])
        if not listing:
            return {"status": "error", "reason": "listing_not_found"}

        import json
        buyer_limits  = json.loads(neg["buyer_limits"])  if neg["buyer_limits"]  else {}
        seller_limits = json.loads(neg["seller_limits"]) if neg["seller_limits"] else {
            "minimo": listing["floor_price"],
            "ideal":  listing["listed_price"],
        }
        appraisal_data = json.loads(listing["appraisal_data"]) if listing["appraisal_data"] else {}

        history = []
        rejected = await self._neg_repo.get_rejected_values(negotiation_id)
        current_offer = buyer_limits.get("ideal", listing["listed_price"] * 0.9)

        existing_rounds = await self._neg_repo.get_proxy_rounds(negotiation_id)
        start_round = len(existing_rounds)

        for i in range(MAX_PROXY_ROUNDS):
            round_num = start_round + i + 1

            try:
                seller_resp = await self._seller_proxy.evaluate(
                    received_offer=current_offer,
                    limits=seller_limits,
                    product_data=appraisal_data,
                    history=history,
                    rejected=rejected,
                )
            except PriceLimitExceeded as e:
                logger.error(f"Seller guard rail violated: {e}")
                break

            if seller_resp.decision == "aceitar":
                await self._neg_repo.add_proxy_round(
                    negotiation_id, round_num, current_offer,
                    seller_argument=f"ACCEPTED: {seller_resp.argument}",
                )
                return await self._propose_to_humans(negotiation_id, current_offer)

            try:
                buyer_resp = await self._buyer_proxy.evaluate(
                    counteroffer=seller_resp.value,
                    limits=buyer_limits,
                    product_data=appraisal_data,
                    history=history,
                    rejected=rejected,
                )
            except PriceLimitExceeded as e:
                logger.error(f"Buyer guard rail violated: {e}")
                break

            await self._neg_repo.add_proxy_round(
                negotiation_id, round_num,
                proposed_value=seller_resp.value,
                seller_argument=seller_resp.argument,
                buyer_argument=buyer_resp.argument,
            )

            history.append({
                "round":          round_num,
                "seller_value":   seller_resp.value,
                "seller_arg":     seller_resp.argument,
                "buyer_value":    buyer_resp.value,
                "buyer_arg":      buyer_resp.argument,
            })

            if buyer_resp.decision == "aceitar":
                return await self._propose_to_humans(negotiation_id, seller_resp.value)

            current_offer = buyer_resp.value

        fallback_value = self._fallback_intersection(
            buyer_limits.get("maximo", 0),
            seller_limits.get("minimo", listing["floor_price"]),
        )
        if fallback_value is None:
            await self._neg_repo.update_status(negotiation_id, "no_deal")
            logger.info(f"Negotiation {negotiation_id}: no agreement (fallback impossible)")
            return {"status": "no_deal"}

        return await self._propose_to_humans(negotiation_id, fallback_value)

    async def _propose_to_humans(self, negotiation_id: int, price: float) -> dict:
        """Proposes a price to the seller and then the buyer, sequentially."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "error"}

        attempts = neg["human_attempts"] or 0
        if attempts >= MAX_HUMAN_ATTEMPTS:
            await self._neg_repo.update_status(negotiation_id, "no_deal")
            return {"status": "no_deal", "reason": "max_attempts_reached"}

        await self._neg_repo.update_price_and_status(negotiation_id, price, "pending_seller")

        listing = await self._listing_repo.find_by_id(neg["listing_id"])
        seller_id = listing["seller_id"]

        await self._notify(
            seller_id,
            "pending_seller",
            {"value": price, "negotiation_id": negotiation_id},
        )

        logger.info(f"Negotiation {negotiation_id}: proposal R${price:.2f} sent to seller {seller_id}")
        return {"status": "pending_seller", "value": price, "negotiation_id": negotiation_id}

    async def accept_seller_proposal(self, negotiation_id: int) -> dict:
        """Called when the seller confirms the proposal."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "error"}

        price = neg["current_price"]
        await self._neg_repo.update_status(negotiation_id, "pending_buyer")
        await self._notify(
            neg["buyer_id"],
            "pending_buyer",
            {"value": price, "negotiation_id": negotiation_id},
        )
        return {"status": "pending_buyer", "value": price}

    async def reject_seller_proposal(self, negotiation_id: int) -> dict:
        """Seller rejected — records the rejection and restarts proxies."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "error"}
        price = neg["current_price"]
        rounds = await self._neg_repo.get_proxy_rounds(negotiation_id)
        if rounds:
            await self._neg_repo.confirm_proxy_round(rounds[-1]["id"], confirmed_by_seller=False)
        await self._neg_repo.update_status(negotiation_id, "active")
        logger.info(f"Negotiation {negotiation_id}: seller rejected R${price:.2f}, restarting proxies")
        return await self.negotiate_between_proxies(negotiation_id)

    async def accept_buyer_proposal(self, negotiation_id: int) -> dict:
        """Buyer accepts — negotiation closed."""
        await self._neg_repo.update_status(negotiation_id, "accepted")
        logger.info(f"Negotiation {negotiation_id}: ACCEPTED by both sides")
        return {"status": "accepted", "negotiation_id": negotiation_id}

    async def reject_buyer_proposal(self, negotiation_id: int) -> dict:
        """Buyer rejected — records the rejection and restarts proxies."""
        neg = await self._neg_repo.find_by_id(negotiation_id)
        if not neg:
            return {"status": "error"}
        rounds = await self._neg_repo.get_proxy_rounds(negotiation_id)
        if rounds:
            await self._neg_repo.confirm_proxy_round(rounds[-1]["id"], confirmed_by_buyer=False)
        await self._neg_repo.update_status(negotiation_id, "active")
        return await self.negotiate_between_proxies(negotiation_id)

    async def _notify(self, user_id: int, event: str, data: dict) -> None:
        """Placeholder for WhatsApp user notification."""
        logger.info(f"[NOTIFY] user_id={user_id} event={event} data={data}")
