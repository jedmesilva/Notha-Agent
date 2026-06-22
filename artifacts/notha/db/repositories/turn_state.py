"""
TurnStateRepository — persistent per-phone turn state.

Tracks what specific field/question NOTHA last asked the user, waiting for reply.
This prevents message ambiguity: "Oi" is never mistaken for a name unless
we explicitly registered that we asked for a name in the previous turn.
"""
import json
import logging
from db.connection import DB

logger = logging.getLogger("notha.db.turn_state")

_EXPIRY_MINUTES = 30


class TurnStateRepository:
    def __init__(self, db: DB):
        self._db = db

    async def set(
        self,
        phone: str,
        pending_field: str,
        operation: str,
        context_data: dict | None = None,
        expiry_minutes: int = _EXPIRY_MINUTES,
    ) -> None:
        """Create or replace the pending turn state for this phone."""
        await self._db.execute(
            """
            INSERT INTO turn_state (phone, pending_field, operation, context_data, expires_at)
            VALUES ($1, $2, $3, $4::jsonb, NOW() + ($5 || ' minutes')::INTERVAL)
            ON CONFLICT (phone) DO UPDATE SET
                pending_field = EXCLUDED.pending_field,
                operation     = EXCLUDED.operation,
                context_data  = EXCLUDED.context_data,
                asked_at      = NOW(),
                expires_at    = NOW() + ($5 || ' minutes')::INTERVAL
            """,
            phone, pending_field, operation,
            json.dumps(context_data or {}),
            str(expiry_minutes),
        )
        logger.info("Turn state SET: phone=%s field=%s op=%s", phone, pending_field, operation)

    async def get(self, phone: str) -> dict | None:
        """Return the active (non-expired) pending turn state, or None."""
        row = await self._db.fetch_one(
            """
            SELECT phone, pending_field, operation, context_data, asked_at, expires_at
            FROM turn_state
            WHERE phone = $1 AND expires_at > NOW()
            """,
            phone,
        )
        if row is None:
            return None
        return dict(row)

    async def clear(self, phone: str) -> None:
        """Remove any pending turn state for this phone."""
        await self._db.execute("DELETE FROM turn_state WHERE phone = $1", phone)
        logger.info("Turn state CLEARED: phone=%s", phone)

    async def clear_if_field(self, phone: str, field: str) -> bool:
        """Clear turn state only if the pending_field matches. Returns True if cleared."""
        result = await self._db.execute(
            "DELETE FROM turn_state WHERE phone = $1 AND pending_field = $2",
            phone, field,
        )
        cleared = result.split()[-1] != "0" if result else False
        if cleared:
            logger.info("Turn state CLEARED for field=%s phone=%s", field, phone)
        return cleared
