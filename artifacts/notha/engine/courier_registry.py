"""
CourierRegistry — pure deterministic service for courier search and availability.

Architecture rule (doc section 9):
  Courier Registry = Service (rules are known and enumerable).
  Courier Matching Agent = Agent (negotiation has open-ended outcomes).
  These are kept separate so the LLM never has direct access to courier data
  outside of what the Matching Agent explicitly passes via tools.
"""
import logging
from db.connection import DB

logger = logging.getLogger("notha.engine.courier_registry")


class CourierRegistry:
    def __init__(self, db: DB):
        self._db = db

    async def find_available(
        self,
        city: str | None = None,
        state: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Returns available couriers filtered by region. No LLM involved."""
        try:
            if city:
                rows = await self._db.fetch_all(
                    """
                    SELECT u.id, u.full_name, u.tax_id,
                           sp.pix_key, sp.pickup_address,
                           sp.service_region, sp.min_delivery_fee
                    FROM users u
                    JOIN seller_profiles sp ON sp.user_id = u.id
                    WHERE u.role = 'courier'
                      AND sp.available = TRUE
                      AND (
                          sp.service_region ILIKE $1
                          OR sp.pickup_address ILIKE $1
                      )
                    ORDER BY sp.min_delivery_fee ASC NULLS LAST
                    LIMIT $2
                    """,
                    f"%{city}%", max_results,
                )
            else:
                rows = await self._db.fetch_all(
                    """
                    SELECT u.id, u.full_name, u.tax_id,
                           sp.pix_key, sp.pickup_address,
                           sp.service_region, sp.min_delivery_fee
                    FROM users u
                    JOIN seller_profiles sp ON sp.user_id = u.id
                    WHERE u.role = 'courier'
                      AND sp.available = TRUE
                    ORDER BY sp.min_delivery_fee ASC NULLS LAST
                    LIMIT $1
                    """,
                    max_results,
                )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("CourierRegistry.find_available error: %s", e)
            return []

    async def get_by_id(self, courier_id: int) -> dict | None:
        """Returns a specific courier by user ID."""
        try:
            row = await self._db.fetch_one(
                """
                SELECT u.id, u.full_name, u.tax_id,
                       sp.pix_key, sp.pickup_address,
                       sp.service_region, sp.min_delivery_fee
                FROM users u
                JOIN seller_profiles sp ON sp.user_id = u.id
                WHERE u.id = $1 AND u.role = 'courier'
                """,
                courier_id,
            )
            return dict(row) if row else None
        except Exception as e:
            logger.error("CourierRegistry.get_by_id error: %s", e)
            return None

    async def set_availability(self, courier_id: int, available: bool) -> None:
        """Toggle a courier's availability status."""
        try:
            await self._db.execute(
                "UPDATE seller_profiles SET available = $1 WHERE user_id = $2",
                available, courier_id,
            )
            logger.info("Courier %d availability → %s", courier_id, available)
        except Exception as e:
            logger.error("CourierRegistry.set_availability error: %s", e)

    async def register(
        self,
        user_id: int,
        service_region: str,
        min_delivery_fee: float | None = None,
    ) -> None:
        """Register or update a courier's service area."""
        try:
            await self._db.execute(
                """
                INSERT INTO seller_profiles (user_id, service_region, min_delivery_fee, available)
                VALUES ($1, $2, $3, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET
                    service_region   = EXCLUDED.service_region,
                    min_delivery_fee = COALESCE(EXCLUDED.min_delivery_fee, seller_profiles.min_delivery_fee),
                    available        = TRUE
                """,
                user_id, service_region, min_delivery_fee,
            )
            logger.info("Courier registered/updated: user_id=%d region=%s", user_id, service_region)
        except Exception as e:
            logger.error("CourierRegistry.register error: %s", e)
