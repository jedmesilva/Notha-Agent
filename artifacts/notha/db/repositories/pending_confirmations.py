"""
PendingConfirmationsRepository — persistent pending business confirmations.

Replaces the in-memory PENDING_CONFIRMATIONS dict so confirmations survive
server restarts. One row per phone; expires after 2 hours if never resolved.

Current confirmation types:
  - "confirm_listing_price": seller is confirming the suggested price before
    the listing is created. Data fields: appraisal, description, category,
    asking_price, seller_id.
"""
import json
import logging
from db.connection import DB

logger = logging.getLogger("notha.db.pending_confirmations")

_EXPIRY_MINUTES = 120


class PendingConfirmationsRepository:
    def __init__(self, db: DB):
        self._db = db

    async def set(
        self,
        phone: str,
        conf_type: str,
        data: dict,
        expiry_minutes: int = _EXPIRY_MINUTES,
    ) -> None:
        """Create or replace the pending confirmation for this phone."""
        await self._db.execute(
            """
            INSERT INTO pending_confirmations
                (phone, type, data, created_at, expires_at)
            VALUES ($1, $2, $3::jsonb, NOW(), NOW() + ($4 || ' minutes')::INTERVAL)
            ON CONFLICT (phone) DO UPDATE SET
                type       = EXCLUDED.type,
                data       = EXCLUDED.data,
                created_at = NOW(),
                expires_at = NOW() + ($4 || ' minutes')::INTERVAL
            """,
            phone, conf_type, json.dumps(data), str(expiry_minutes),
        )
        logger.info("Pending confirmation SET: phone=%s type=%s", phone, conf_type)

    async def get(self, phone: str) -> dict | None:
        """Return the active (non-expired) pending confirmation, or None.

        Returns a flat dict that includes the type key merged with the data
        fields, matching the shape previously stored in PENDING_CONFIRMATIONS.
        """
        row = await self._db.fetch_one(
            """
            SELECT type, data
            FROM pending_confirmations
            WHERE phone = $1 AND expires_at > NOW()
            """,
            phone,
        )
        if row is None:
            return None
        result: dict = dict(row["data"])
        result["type"] = row["type"]
        return result

    async def clear(self, phone: str) -> None:
        """Remove any pending confirmation for this phone."""
        await self._db.execute(
            "DELETE FROM pending_confirmations WHERE phone = $1", phone
        )
        logger.info("Pending confirmation CLEARED: phone=%s", phone)
