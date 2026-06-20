"""
Jobs periódicos do NOTHA.

- verificar_timeouts: rodadas de negociação expiradas (a cada 60s)
- verificar_retiradas_vencidas: pickups não confirmados (a cada 5min)
- verificar_estornos_automaticos: estorno automático pós-prazo (a cada 5min)
- verificar_expiracoes_totais: negociações totalmente expiradas (a cada 5min)

Todos rodam como asyncio tasks, sem Celery.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from db.connection import get_db
from db.repositories import (
    NegotiationRepository, ListingRepository,
    TransactionRepository, DeliveryRepository,
)
from asaas import AsaasClient
from config import PRAZO_ESTORNO_POS_FALHA_DIAS

logger = logging.getLogger("notha.jobs")

_whatsapp_sender = None


def set_whatsapp_sender(sender) -> None:
    global _whatsapp_sender
    _whatsapp_sender = sender


async def _notify(user_id: int, message: str) -> None:
    if _whatsapp_sender and user_id:
        try:
            await _whatsapp_sender(str(user_id), message)
        except Exception as e:
            logger.error(f"Falha ao notificar user_id={user_id}: {e}")


async def verificar_timeouts() -> None:
    db = get_db()
    if not db:
        return

    neg_repo = NegotiationRepository(db)
    listing_repo = ListingRepository(db)

    try:
        vencidas = await neg_repo.find_timed_out()
        for neg in vencidas:
            await neg_repo.set_status(neg["id"], "expirada_por_timeout")
            logger.info(f"Negociação {neg['id']} expirada por timeout de rodada.")

            await listing_repo.add_to_interest_queue(
                listing_id=neg["listing_id"],
                buyer_id=neg["buyer_id"],
                oferta_inicial=neg["preco_atual_proposto"],
            )

            proximo = await listing_repo.next_in_queue(neg["listing_id"])
            if proximo:
                await listing_repo.remove_from_queue(proximo["id"])
                from engine.negotiation import NegotiationEngine
                engine = NegotiationEngine(db)
                await neg_repo.create(
                    listing_id=neg["listing_id"],
                    buyer_id=proximo["buyer_id"],
                    limite_comprador={"maximo": proximo["oferta_inicial"], "ideal": proximo["oferta_inicial"]},
                )
                logger.info(f"Próximo da fila: buyer_id={proximo['buyer_id']} para listing={neg['listing_id']}")
            else:
                await listing_repo.set_status(neg["listing_id"], "disponivel")

    except Exception as e:
        logger.error(f"Erro no job verificar_timeouts: {e}")


async def verificar_expiracoes_totais() -> None:
    db = get_db()
    if not db:
        return

    neg_repo = NegotiationRepository(db)
    listing_repo = ListingRepository(db)

    try:
        expiradas = await neg_repo.find_totally_expired()
        for neg in expiradas:
            fila = await listing_repo.get_queue_count(neg["listing_id"])
            if fila == 0:
                await neg_repo.set_status(neg["id"], "expirada")
                await listing_repo.set_status(neg["listing_id"], "disponivel")
                logger.info(f"Negociação {neg['id']} expirada totalmente. Listing devolvido ao catálogo.")
    except Exception as e:
        logger.error(f"Erro no job verificar_expiracoes_totais: {e}")


async def verificar_retiradas_vencidas() -> None:
    db = get_db()
    if not db:
        return

    delivery_repo = DeliveryRepository(db)
    listing_repo = ListingRepository(db)
    neg_repo = NegotiationRepository(db)
    tx_repo = TransactionRepository(db)

    try:
        vencidas = await delivery_repo.find_overdue_pickups()
        for retirada in vencidas:
            await delivery_repo.relist(retirada["id"])
            logger.info(f"Retirada {retirada['id']} não confirmada — iniciando tratamento pós-prazo.")

            neg = await neg_repo.find_by_id(retirada["negotiation_id"])
            if not neg:
                continue

            await listing_repo.set_status(neg["listing_id"], "disponivel")

            tx = await tx_repo.find_by_negotiation(neg["id"])
            if tx:
                prazo = datetime.utcnow() + timedelta(days=PRAZO_ESTORNO_POS_FALHA_DIAS)
                await tx_repo.set_retention_status(
                    tx["id"],
                    "retido_aguardando_decisao_pos_falha",
                    prazo_estorno_automatico=prazo,
                )
                logger.info(f"Transação {tx['id']} marcada para estorno automático em {PRAZO_ESTORNO_POS_FALHA_DIAS} dias.")

    except Exception as e:
        logger.error(f"Erro no job verificar_retiradas_vencidas: {e}")


async def verificar_estornos_automaticos() -> None:
    db = get_db()
    if not db:
        return

    tx_repo = TransactionRepository(db)
    asaas = AsaasClient()

    try:
        pendentes = await tx_repo.find_pending_refunds()
        for tx in pendentes:
            try:
                if tx["asaas_charge_id"]:
                    await asaas.estornar(
                        cobranca_id=tx["asaas_charge_id"],
                        idempotency_key=f"estorno-{tx['id']}",
                    )
                await tx_repo.set_retention_status(tx["id"], "estornado_automaticamente")
                logger.info(f"Estorno automático executado para transação {tx['id']}.")
            except Exception as e:
                logger.error(f"Falha no estorno automático tx={tx['id']}: {e}")
    except Exception as e:
        logger.error(f"Erro no job verificar_estornos_automaticos: {e}")


async def _run_job(name: str, coro_fn, interval_seconds: int) -> None:
    while True:
        try:
            await coro_fn()
        except Exception as e:
            logger.error(f"Job '{name}' falhou inesperadamente: {e}")
        await asyncio.sleep(interval_seconds)


async def start_all_jobs() -> None:
    """Inicia todos os jobs periódicos como asyncio tasks."""
    logger.info("Iniciando jobs periódicos do NOTHA...")
    asyncio.create_task(_run_job("verificar_timeouts", verificar_timeouts, 60))
    asyncio.create_task(_run_job("verificar_expiracoes_totais", verificar_expiracoes_totais, 300))
    asyncio.create_task(_run_job("verificar_retiradas_vencidas", verificar_retiradas_vencidas, 300))
    asyncio.create_task(_run_job("verificar_estornos_automaticos", verificar_estornos_automaticos, 300))
    logger.info("Jobs periódicos iniciados.")
