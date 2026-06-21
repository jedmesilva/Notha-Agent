"""
Logistics/Delivery Agent — decides and coordinates the delivery mode after the deal
is confirmed by both parties and before releasing the held funds.

Flow:
  1. Asks the buyer: self-pickup or NOTHA delivery?
  2. If pickup: requests date/time, creates schedule, notifies seller.
  3. If delivery: activates Delivery Proxy to find and negotiate with a courier.
"""
import logging
from db.connection import DB
from db.repositories import DeliveryRepository
from agents.proxy import DeliveryProxyAgent

logger = logging.getLogger("notha.agent.logistics")

MAX_DELIVERY_ROUNDS = 3


class LogisticsAgent:
    def __init__(self, db: DB):
        self._db = db
        self._delivery_repo = DeliveryRepository(db)
        self._delivery_proxy = DeliveryProxyAgent()

    async def create_pickup_schedule(
        self,
        negotiation_id: int,
        data_agendada,
        horario_agendado: str,
        prazo_confirmacao,
        entregador_id: int | None = None,
    ):
        """Registers a pickup schedule."""
        delivery = await self._delivery_repo.create(
            negotiation_id=negotiation_id,
            modalidade="retirada",
            data_agendada=data_agendada,
            horario_agendado=horario_agendado,
            prazo_confirmacao=prazo_confirmacao,
            entregador_id=entregador_id,
        )
        logger.info(f"Pickup schedule created: delivery_id={delivery['id']}")
        return delivery

    async def find_and_negotiate_courier(
        self,
        negotiation_id: int,
        origin: str,
        destination: str,
        max_delivery: float,
        available_couriers: list[dict],
    ) -> dict | None:
        """
        Attempts to negotiate with available couriers in the area.
        Returns the courier and agreed value, or None if no deal is reached.
        """
        for courier in available_couriers:
            history = []
            offer = courier.get("valor_minimo", max_delivery * 1.2)

            for round_num in range(MAX_DELIVERY_ROUNDS):
                result = await self._delivery_proxy.negotiate(
                    origin=origin,
                    destination=destination,
                    max_delivery=max_delivery,
                    courier_offer=offer,
                    history=history,
                )
                history.append({"rodada": round_num + 1, "oferta": offer, "resposta": result.decision})

                if result.decision == "aceitar" and result.value <= max_delivery:
                    logger.info(f"Courier {courier['user_id']} accepted for R${result.value:.2f}")
                    return {
                        "user_id": courier["user_id"],
                        "chave_pix": courier["chave_pix"],
                        "valor_negociado": result.value,
                        "argumento_final": result.argument,
                    }

                if result.decision == "recusar":
                    break

                offer = result.value

        logger.warning(f"No courier found for negotiation_id={negotiation_id}")
        return None

    async def confirm_seller_delivery(self, negotiation_id: int) -> bool:
        """Records the seller's confirmation. Returns True if mutual confirmation occurred."""
        delivery = await self._delivery_repo.find_by_negotiation(negotiation_id)
        if not delivery:
            return False
        confirmed = await self._delivery_repo.confirm_seller(delivery["id"])
        return confirmed is not None

    async def confirm_buyer_receipt(self, negotiation_id: int) -> bool:
        """Records the buyer's confirmation. Returns True if mutual confirmation occurred."""
        delivery = await self._delivery_repo.find_by_negotiation(negotiation_id)
        if not delivery:
            return False
        confirmed = await self._delivery_repo.confirm_buyer(delivery["id"])
        return confirmed is not None
