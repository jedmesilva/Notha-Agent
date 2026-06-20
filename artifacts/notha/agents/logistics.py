"""
Logistics/Delivery Agent — decide e coordena a modalidade de entrega após o acordo
ser confirmado por ambas as partes e antes da liberação do valor retido.

Fluxo:
  1. Pergunta ao comprador: retirar ou entrega NOTHA?
  2. Se retirada: solicita data/horário, cria agendamento, notifica vendedor.
  3. Se entrega: aciona Delivery Proxy para encontrar e negociar com entregador.
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
        """Registra agendamento de retirada."""
        delivery = await self._delivery_repo.create(
            negotiation_id=negotiation_id,
            modalidade="retirada",
            data_agendada=data_agendada,
            horario_agendado=horario_agendado,
            prazo_confirmacao=prazo_confirmacao,
            entregador_id=entregador_id,
        )
        logger.info(f"Agendamento de retirada criado: delivery_id={delivery['id']}")
        return delivery

    async def find_and_negotiate_courier(
        self,
        negotiation_id: int,
        origem: str,
        destino: str,
        maximo_entrega: float,
        couriers_disponiveis: list[dict],
    ) -> dict | None:
        """
        Tenta negociar com entregadores disponíveis na região.
        Retorna o entregador e valor acordado, ou None se não houver acordo.
        """
        for courier in couriers_disponiveis:
            historico = []
            oferta = courier.get("valor_minimo", maximo_entrega * 1.2)

            for rodada in range(MAX_DELIVERY_ROUNDS):
                resultado = await self._delivery_proxy.negociar(
                    origem=origem,
                    destino=destino,
                    maximo_entrega=maximo_entrega,
                    oferta_entregador=oferta,
                    historico=historico,
                )
                historico.append({"rodada": rodada + 1, "oferta": oferta, "resposta": resultado.decisao})

                if resultado.decisao == "aceitar" and resultado.valor <= maximo_entrega:
                    logger.info(f"Entregador {courier['user_id']} aceito por R${resultado.valor:.2f}")
                    return {
                        "user_id": courier["user_id"],
                        "chave_pix": courier["chave_pix"],
                        "valor_negociado": resultado.valor,
                        "argumento_final": resultado.argumento,
                    }

                if resultado.decisao == "recusar":
                    break

                oferta = resultado.valor

        logger.warning(f"Nenhum entregador encontrado para negotiation_id={negotiation_id}")
        return None

    async def confirm_seller_delivery(self, negotiation_id: int) -> bool:
        """Registra confirmação do vendedor. Retorna True se confirmação mútua ocorreu."""
        delivery = await self._delivery_repo.find_by_negotiation(negotiation_id)
        if not delivery:
            return False
        confirmed = await self._delivery_repo.confirm_seller(delivery["id"])
        return confirmed is not None

    async def confirm_buyer_receipt(self, negotiation_id: int) -> bool:
        """Registra confirmação do comprador. Retorna True se confirmação mútua ocorreu."""
        delivery = await self._delivery_repo.find_by_negotiation(negotiation_id)
        if not delivery:
            return False
        confirmed = await self._delivery_repo.confirm_buyer(delivery["id"])
        return confirmed is not None
