import asyncpg
from db.connection import DB


class TransactionRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        negotiation_id: int,
        product_amount: float,
        delivery_mode: str,
        seller_pix_key: str,
        delivery_amount: float = 0,
        courier_id: int | None = None,
        courier_pix_key: str | None = None,
        notha_fee: float = 0,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO transactions
                (negotiation_id, product_amount, delivery_amount, notha_fee, delivery_mode,
                 seller_pix_key, courier_pix_key, courier_id,
                 status, retention_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', 'held_pending_delivery')
            RETURNING *
            """,
            negotiation_id,
            product_amount,
            delivery_amount,
            notha_fee,
            delivery_mode,
            seller_pix_key,
            courier_pix_key,
            courier_id,
        )

    async def find_by_id(self, transaction_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM transactions WHERE id = $1", transaction_id
        )

    async def find_by_negotiation(self, negotiation_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM transactions WHERE negotiation_id = $1", negotiation_id
        )

    async def set_asaas_charge(self, transaction_id: int, asaas_charge_id: str) -> None:
        await self._db.execute(
            """
            UPDATE transactions
            SET asaas_charge_id = $1, status = 'charge_created', updated_at = now()
            WHERE id = $2
            """,
            asaas_charge_id,
            transaction_id,
        )

    async def set_paid(self, transaction_id: int) -> None:
        await self._db.execute(
            "UPDATE transactions SET status = 'paid', updated_at = now() WHERE id = $1",
            transaction_id,
        )

    async def set_retention_status(
        self,
        transaction_id: int,
        retention_status: str,
        auto_refund_deadline=None,
        # Legacy keyword alias
        prazo_estorno_automatico=None,
    ) -> None:
        deadline = auto_refund_deadline if auto_refund_deadline is not None else prazo_estorno_automatico
        await self._db.execute(
            """
            UPDATE transactions SET
                retention_status     = $1,
                auto_refund_deadline = $2,
                updated_at           = now()
            WHERE id = $3
            """,
            retention_status,
            deadline,
            transaction_id,
        )

    async def set_transfer_ids(
        self,
        transaction_id: int,
        transfer_id_seller: str | None = None,
        transfer_id_courier: str | None = None,
        # Legacy keyword aliases
        transfer_id_vendedor: str | None = None,
        transfer_id_entregador: str | None = None,
    ) -> None:
        effective_seller  = transfer_id_seller  if transfer_id_seller  is not None else transfer_id_vendedor
        effective_courier = transfer_id_courier if transfer_id_courier is not None else transfer_id_entregador
        await self._db.execute(
            """
            UPDATE transactions SET
                asaas_transfer_id_seller  = COALESCE($1, asaas_transfer_id_seller),
                asaas_transfer_id_courier = COALESCE($2, asaas_transfer_id_courier),
                updated_at                = now()
            WHERE id = $3
            """,
            effective_seller,
            effective_courier,
            transaction_id,
        )

    async def find_pending_refunds(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM transactions
            WHERE retention_status = 'held_pending_decision'
              AND auto_refund_deadline < now()
            """
        )

    async def get_total_retained(self) -> float:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(product_amount + delivery_amount), 0)
            FROM transactions
            WHERE retention_status IN ('held_pending_delivery', 'held_pending_decision')
            """
        )
        return float(val or 0)
