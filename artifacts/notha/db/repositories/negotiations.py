import json
import asyncpg
from datetime import datetime, timedelta
from db.connection import DB
from config import ROUND_TIMEOUT_MINUTES, TOTAL_EXPIRATION_HOURS


class NegotiationRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        listing_id: int,
        buyer_id: int,
        mode: str = "proxy",
        buyer_limits: dict | None = None,
        seller_limits: dict | None = None,
    ) -> asyncpg.Record:
        now = datetime.utcnow()
        return await self._db.fetch_one(
            """
            INSERT INTO negotiations
                (listing_id, buyer_id, mode, status, buyer_limits, seller_limits,
                 responder_until, expires_at)
            VALUES ($1, $2, $3, 'active', $4, $5, $6, $7)
            RETURNING *
            """,
            listing_id,
            buyer_id,
            mode,
            json.dumps(buyer_limits) if buyer_limits else None,
            json.dumps(seller_limits) if seller_limits else None,
            now + timedelta(minutes=ROUND_TIMEOUT_MINUTES),
            now + timedelta(hours=TOTAL_EXPIRATION_HOURS),
        )

    async def find_by_id(self, negotiation_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM negotiations WHERE id = $1", negotiation_id
        )

    async def find_active_by_listing(self, listing_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM negotiations
            WHERE listing_id = $1 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            listing_id,
        )

    async def find_active_by_buyer(self, buyer_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM negotiations
            WHERE buyer_id = $1
              AND status IN ('active', 'pending_seller', 'pending_buyer')
            """,
            buyer_id,
        )

    async def update_status(self, negotiation_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE negotiations SET status = $1 WHERE id = $2",
            status,
            negotiation_id,
        )

    # Keep legacy alias used by engine code
    async def set_status(self, negotiation_id: int, status: str) -> None:
        await self.update_status(negotiation_id, status)

    async def update_price_and_status(
        self, negotiation_id: int, price: float, status: str | None = None
    ) -> None:
        if status:
            await self._db.execute(
                """
                UPDATE negotiations SET
                    current_price   = $1,
                    status          = $2,
                    human_attempts  = human_attempts + 1,
                    responder_until = $3
                WHERE id = $4
                """,
                price,
                status,
                datetime.utcnow() + timedelta(minutes=ROUND_TIMEOUT_MINUTES),
                negotiation_id,
            )
        else:
            await self._db.execute(
                "UPDATE negotiations SET current_price = $1 WHERE id = $2",
                price,
                negotiation_id,
            )

    # Legacy alias used by engine
    async def update_offer(
        self, negotiation_id: int, price: float, status: str | None = None
    ) -> None:
        await self.update_price_and_status(negotiation_id, price, status)

    async def update_seller_limit(self, negotiation_id: int, seller_limits: dict) -> None:
        await self._db.execute(
            "UPDATE negotiations SET seller_limits = $1 WHERE id = $2",
            json.dumps(seller_limits),
            negotiation_id,
        )

    async def find_timed_out(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiations WHERE status = 'active' AND responder_until < now()"
        )

    async def find_totally_expired(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiations WHERE status = 'active' AND expires_at < now()"
        )

    async def add_offer(
        self,
        negotiation_id: int,
        author: str,
        proposed_value: float,
        extra_context: str | None = None,
    ) -> asyncpg.Record:
        """author: buyer | seller | system"""
        return await self._db.fetch_one(
            """
            INSERT INTO negotiation_offers
                (negotiation_id, author, proposed_value, extra_context)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            negotiation_id,
            author,
            proposed_value,
            extra_context,
        )

    async def get_offers(self, negotiation_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiation_offers WHERE negotiation_id = $1 ORDER BY created_at ASC",
            negotiation_id,
        )

    async def add_proxy_round(
        self,
        negotiation_id: int,
        round_num: int,
        proposed_value: float,
        seller_argument: str | None = None,
        buyer_argument: str | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO proxy_negotiation_rounds
                (negotiation_id, round_number, proposed_value, seller_argument, buyer_argument)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            negotiation_id,
            round_num,
            proposed_value,
            seller_argument,
            buyer_argument,
        )

    async def confirm_proxy_round(
        self,
        round_id: int,
        confirmed_by_seller: bool | None = None,
        confirmed_by_buyer: bool | None = None,
        # Legacy keyword args accepted for compatibility
        confirmado_pelo_vendedor: bool | None = None,
        confirmado_pelo_comprador: bool | None = None,
    ) -> None:
        effective_seller = confirmed_by_seller if confirmed_by_seller is not None else confirmado_pelo_vendedor
        effective_buyer  = confirmed_by_buyer  if confirmed_by_buyer  is not None else confirmado_pelo_comprador
        if effective_seller is not None:
            await self._db.execute(
                "UPDATE proxy_negotiation_rounds SET confirmed_by_seller = $1 WHERE id = $2",
                effective_seller,
                round_id,
            )
        if effective_buyer is not None:
            await self._db.execute(
                "UPDATE proxy_negotiation_rounds SET confirmed_by_buyer = $1 WHERE id = $2",
                effective_buyer,
                round_id,
            )

    async def get_proxy_rounds(self, negotiation_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM proxy_negotiation_rounds
            WHERE negotiation_id = $1
            ORDER BY round_number ASC
            """,
            negotiation_id,
        )

    async def get_rejected_values(self, negotiation_id: int) -> list[float]:
        rows = await self._db.fetch_all(
            """
            SELECT proposed_value FROM proxy_negotiation_rounds
            WHERE negotiation_id = $1
              AND (confirmed_by_seller = FALSE OR confirmed_by_buyer = FALSE)
            """,
            negotiation_id,
        )
        return [r["proposed_value"] for r in rows]
