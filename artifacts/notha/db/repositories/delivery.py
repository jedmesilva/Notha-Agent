import asyncpg
from db.connection import DB


class DeliveryRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        negotiation_id: int,
        modalidade: str,
        data_agendada=None,
        horario_agendado: str | None = None,
        prazo_confirmacao=None,
        entregador_id: int | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO delivery_confirmations
                (negotiation_id, modalidade, entregador_id, data_agendada,
                 horario_agendado, prazo_confirmacao, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'agendada')
            RETURNING *
            """,
            negotiation_id,
            modalidade,
            entregador_id,
            data_agendada,
            horario_agendado,
            prazo_confirmacao,
        )

    async def find_by_id(self, delivery_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM delivery_confirmations WHERE id = $1", delivery_id
        )

    async def find_by_negotiation(self, negotiation_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM delivery_confirmations WHERE negotiation_id = $1 ORDER BY created_at DESC LIMIT 1",
            negotiation_id,
        )

    async def set_status(self, delivery_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE delivery_confirmations SET status = $1 WHERE id = $2",
            status,
            delivery_id,
        )

    async def confirm_seller(self, delivery_id: int) -> asyncpg.Record | None:
        await self._db.execute(
            "UPDATE delivery_confirmations SET confirmado_pelo_vendedor = TRUE WHERE id = $1",
            delivery_id,
        )
        return await self._check_mutual_confirmation(delivery_id)

    async def confirm_buyer(self, delivery_id: int) -> asyncpg.Record | None:
        await self._db.execute(
            "UPDATE delivery_confirmations SET confirmado_pelo_comprador = TRUE WHERE id = $1",
            delivery_id,
        )
        return await self._check_mutual_confirmation(delivery_id)

    async def _check_mutual_confirmation(self, delivery_id: int) -> asyncpg.Record | None:
        row = await self.find_by_id(delivery_id)
        if row and row["confirmado_pelo_vendedor"] and row["confirmado_pelo_comprador"]:
            from datetime import datetime
            await self._db.execute(
                "UPDATE delivery_confirmations SET status = 'confirmada', confirmado_em = $1 WHERE id = $2",
                datetime.utcnow(),
                delivery_id,
            )
            return await self.find_by_id(delivery_id)
        return None

    async def relist(self, delivery_id: int) -> None:
        from datetime import datetime
        await self._db.execute(
            "UPDATE delivery_confirmations SET status = 'nao_confirmada', relisted_at = $1 WHERE id = $2",
            datetime.utcnow(),
            delivery_id,
        )

    async def convert_to_delivery(self, delivery_id: int) -> None:
        await self._db.execute(
            "UPDATE delivery_confirmations SET status = 'convertida_entrega' WHERE id = $1",
            delivery_id,
        )

    async def find_overdue_pickups(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM delivery_confirmations WHERE status = 'agendada' AND prazo_confirmacao < now()"
        )
