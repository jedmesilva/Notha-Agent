"""
NOTHA periodic background jobs.

- check_round_timeouts: expired negotiation rounds (every 60s)
- check_overdue_pickups: unconfirmed pickups (every 5min)
- check_automatic_refunds: automatic refund after deadline (every 5min)
- check_total_expirations: fully expired negotiations (every 5min)

All run as asyncio tasks, no Celery required.
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
from config import REFUND_DEADLINE_AFTER_FAILURE_DAYS

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
            logger.error(f"Failed to notify user_id={user_id}: {e}")


async def check_round_timeouts() -> None:
    db = get_db()
    if not db:
        return

    neg_repo = NegotiationRepository(db)
    listing_repo = ListingRepository(db)

    try:
        timed_out = await neg_repo.find_timed_out()
        for neg in timed_out:
            await neg_repo.set_status(neg["id"], "expirada_por_timeout")
            logger.info(f"Negotiation {neg['id']} expired by round timeout.")

            await listing_repo.add_to_interest_queue(
                listing_id=neg["listing_id"],
                buyer_id=neg["buyer_id"],
                oferta_inicial=neg["preco_atual_proposto"],
            )

            next_in_queue = await listing_repo.next_in_queue(neg["listing_id"])
            if next_in_queue:
                await listing_repo.remove_from_queue(next_in_queue["id"])
                from engine.negotiation import NegotiationEngine
                engine = NegotiationEngine(db)
                await neg_repo.create(
                    listing_id=neg["listing_id"],
                    buyer_id=next_in_queue["buyer_id"],
                    limite_comprador={"maximo": next_in_queue["oferta_inicial"], "ideal": next_in_queue["oferta_inicial"]},
                )
                logger.info(f"Next in queue: buyer_id={next_in_queue['buyer_id']} for listing={neg['listing_id']}")
            else:
                await listing_repo.set_status(neg["listing_id"], "disponivel")

    except Exception as e:
        logger.error(f"Error in check_round_timeouts: {e}")


async def check_total_expirations() -> None:
    db = get_db()
    if not db:
        return

    neg_repo = NegotiationRepository(db)
    listing_repo = ListingRepository(db)

    try:
        expired = await neg_repo.find_totally_expired()
        for neg in expired:
            queue_count = await listing_repo.get_queue_count(neg["listing_id"])
            if queue_count == 0:
                await neg_repo.set_status(neg["id"], "expirada")
                await listing_repo.set_status(neg["listing_id"], "disponivel")
                logger.info(f"Negotiation {neg['id']} fully expired. Listing returned to catalog.")
    except Exception as e:
        logger.error(f"Error in check_total_expirations: {e}")


async def check_overdue_pickups() -> None:
    db = get_db()
    if not db:
        return

    delivery_repo = DeliveryRepository(db)
    listing_repo = ListingRepository(db)
    neg_repo = NegotiationRepository(db)
    tx_repo = TransactionRepository(db)

    try:
        overdue = await delivery_repo.find_overdue_pickups()
        for pickup in overdue:
            await delivery_repo.relist(pickup["id"])
            logger.info(f"Pickup {pickup['id']} unconfirmed — initiating post-deadline handling.")

            neg = await neg_repo.find_by_id(pickup["negotiation_id"])
            if not neg:
                continue

            await listing_repo.set_status(neg["listing_id"], "disponivel")

            tx = await tx_repo.find_by_negotiation(neg["id"])
            if tx:
                deadline = datetime.utcnow() + timedelta(days=REFUND_DEADLINE_AFTER_FAILURE_DAYS)
                await tx_repo.set_retention_status(
                    tx["id"],
                    "retido_aguardando_decisao_pos_falha",
                    prazo_estorno_automatico=deadline,
                )
                logger.info(f"Transaction {tx['id']} scheduled for automatic refund in {REFUND_DEADLINE_AFTER_FAILURE_DAYS} days.")

    except Exception as e:
        logger.error(f"Error in check_overdue_pickups: {e}")


async def check_automatic_refunds() -> None:
    db = get_db()
    if not db:
        return

    tx_repo = TransactionRepository(db)
    asaas = AsaasClient()

    try:
        pending = await tx_repo.find_pending_refunds()
        for tx in pending:
            try:
                if tx["asaas_charge_id"]:
                    await asaas.refund(
                        charge_id=tx["asaas_charge_id"],
                        idempotency_key=f"estorno-{tx['id']}",
                    )
                await tx_repo.set_retention_status(tx["id"], "estornado_automaticamente")
                logger.info(f"Automatic refund executed for transaction {tx['id']}.")
            except Exception as e:
                logger.error(f"Automatic refund failed tx={tx['id']}: {e}")
    except Exception as e:
        logger.error(f"Error in check_automatic_refunds: {e}")


async def _run_job(name: str, coro_fn, interval_seconds: int) -> None:
    while True:
        try:
            await coro_fn()
        except Exception as e:
            logger.error(f"Job '{name}' failed unexpectedly: {e}")
        await asyncio.sleep(interval_seconds)


async def start_all_jobs() -> None:
    """Starts all periodic jobs as asyncio tasks."""
    logger.info("Starting NOTHA periodic jobs...")
    asyncio.create_task(_run_job("check_round_timeouts", check_round_timeouts, 60))
    asyncio.create_task(_run_job("check_total_expirations", check_total_expirations, 300))
    asyncio.create_task(_run_job("check_overdue_pickups", check_overdue_pickups, 300))
    asyncio.create_task(_run_job("check_automatic_refunds", check_automatic_refunds, 300))
    logger.info("Periodic jobs started.")
