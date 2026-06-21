import asyncpg
from db.connection import DB


class DeliveryRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        negotiation_id: int,
        delivery_mode: str,
        scheduled_date=None,
        scheduled_time: str | None = None,
        confirmation_deadline=None,
        courier_id: int | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO delivery_confirmations
                (negotiation_id, delivery_mode, courier_id, scheduled_date,
                 scheduled_time, confirmation_deadline, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'scheduled')
            RETURNING *
            """,
            negotiation_id,
            delivery_mode,
            courier_id,
            scheduled_date,
            scheduled_time,
            confirmation_deadline,
        )

    async def find_by_id(self, delivery_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM delivery_confirmations WHERE id = $1", delivery_id
        )

    async def find_by_negotiation(self, negotiation_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM delivery_confirmations
            WHERE negotiation_id = $1
            ORDER BY created_at DESC LIMIT 1
            """,
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
            "UPDATE delivery_confirmations SET confirmed_by_seller = TRUE WHERE id = $1",
            delivery_id,
        )
        return await self._check_mutual_confirmation(delivery_id)

    async def confirm_buyer(self, delivery_id: int) -> asyncpg.Record | None:
        await self._db.execute(
            "UPDATE delivery_confirmations SET confirmed_by_buyer = TRUE WHERE id = $1",
            delivery_id,
        )
        return await self._check_mutual_confirmation(delivery_id)

    async def _check_mutual_confirmation(self, delivery_id: int) -> asyncpg.Record | None:
        row = await self.find_by_id(delivery_id)
        if row and row["confirmed_by_seller"] and row["confirmed_by_buyer"]:
            from datetime import datetime
            await self._db.execute(
                """
                UPDATE delivery_confirmations
                SET status = 'confirmed', confirmed_at = $1
                WHERE id = $2
                """,
                datetime.utcnow(),
                delivery_id,
            )
            return await self.find_by_id(delivery_id)
        return None

    async def relist(self, delivery_id: int) -> None:
        from datetime import datetime
        await self._db.execute(
            """
            UPDATE delivery_confirmations
            SET status = 'unconfirmed', relisted_at = $1
            WHERE id = $2
            """,
            datetime.utcnow(),
            delivery_id,
        )

    async def convert_to_delivery(self, delivery_id: int) -> None:
        await self._db.execute(
            "UPDATE delivery_confirmations SET status = 'converted' WHERE id = $1",
            delivery_id,
        )

    async def find_overdue_pickups(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM delivery_confirmations
            WHERE status = 'scheduled' AND confirmation_deadline < now()
            """
        )
