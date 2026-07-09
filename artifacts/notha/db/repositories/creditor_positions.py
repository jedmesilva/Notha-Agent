"""
CreditorPositionRepository — data access for creditor_positions table.

Tracks each creditor's commitment to a CaptureOrder.

Status machine:
  reserved → confirmed (Pix received) | reverted (order failed)

CRITICAL: Only 'confirmed' positions count toward closing a CaptureOrder.
          'reserved' funds are held in escrow (platform wallet) but do NOT
          trigger instrument emission until confirmed.
"""
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("notha.repo.creditor_positions")


class CreditorPositionRepository:
    def __init__(self, db):
        self._db = db

    async def create(
        self,
        *,
        capture_order_id: int,
        creditor_user_id: int,
        committed_amount: Decimal,
        origin: str = "manual",
    ) -> int:
        """Creates a new CreditorPosition in 'reserved' status. Returns its id."""
        row = await self._db.fetch_one(
            """
            INSERT INTO creditor_positions
                (capture_order_id, creditor_user_id, committed_amount, origin, status)
            VALUES ($1, $2, $3, $4, 'reserved')
            RETURNING id
            """,
            capture_order_id,
            creditor_user_id,
            committed_amount,
            origin,
        )
        return row["id"]

    async def get_by_id(self, position_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM creditor_positions WHERE id = $1",
            position_id,
        )
        return dict(row) if row else None

    async def list_by_order(
        self,
        capture_order_id: int,
        status: Optional[str] = None,
    ) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM creditor_positions
                WHERE capture_order_id = $1 AND status = $2
                ORDER BY reserved_at ASC
                """,
                capture_order_id, status,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM creditor_positions
                WHERE capture_order_id = $1
                ORDER BY reserved_at ASC
                """,
                capture_order_id,
            )
        return [dict(r) for r in rows]

    async def list_by_creditor(
        self,
        creditor_user_id: int,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                """
                SELECT cp.*, co.approved_rate, co.creditor_rate,
                       co.target_amount, co.capture_deadline,
                       cr.user_id AS debtor_id, cr.requested_amount
                FROM creditor_positions cp
                JOIN capture_orders co ON co.id = cp.capture_order_id
                JOIN capture_requests cr ON cr.id = co.capture_request_id
                WHERE cp.creditor_user_id = $1 AND cp.status = $2
                ORDER BY cp.reserved_at DESC
                LIMIT $3
                """,
                creditor_user_id, status, limit,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT cp.*, co.approved_rate, co.creditor_rate,
                       co.target_amount, co.capture_deadline,
                       cr.user_id AS debtor_id, cr.requested_amount
                FROM creditor_positions cp
                JOIN capture_orders co ON co.id = cp.capture_order_id
                JOIN capture_requests cr ON cr.id = co.capture_request_id
                WHERE cp.creditor_user_id = $1
                ORDER BY cp.reserved_at DESC
                LIMIT $2
                """,
                creditor_user_id, limit,
            )
        return [dict(r) for r in rows]

    async def confirm(self, position_id: int) -> None:
        """Transitions reserved → confirmed after Pix payment is received."""
        now = datetime.now(timezone.utc)
        await self._db.execute(
            """
            UPDATE creditor_positions
            SET status = 'confirmed', confirmed_at = $1
            WHERE id = $2 AND status = 'reserved'
            """,
            now, position_id,
        )

    async def revert(self, position_id: int) -> None:
        """Transitions reserved → reverted when capture order expires/cancels."""
        now = datetime.now(timezone.utc)
        await self._db.execute(
            """
            UPDATE creditor_positions
            SET status = 'reverted', reverted_at = $1
            WHERE id = $2 AND status = 'reserved'
            """,
            now, position_id,
        )

    async def set_participation_fraction(
        self, position_id: int, fraction: Decimal
    ) -> None:
        """Sets the creditor's fraction of the total credit instrument (set at emission)."""
        await self._db.execute(
            "UPDATE creditor_positions SET participation_fraction = $1 WHERE id = $2",
            fraction, position_id,
        )

    async def get_confirmed_total(self, capture_order_id: int) -> Decimal:
        """Returns the total amount in confirmed positions for an order."""
        row = await self._db.fetch_one(
            """
            SELECT COALESCE(SUM(committed_amount), 0) AS total
            FROM creditor_positions
            WHERE capture_order_id = $1 AND status = 'confirmed'
            """,
            capture_order_id,
        )
        return Decimal(str(row["total"])) if row else Decimal("0")

    async def has_active_position(
        self, capture_order_id: int, creditor_user_id: int
    ) -> bool:
        """Returns True if this creditor already has an active position in this order."""
        row = await self._db.fetch_one(
            """
            SELECT 1 FROM creditor_positions
            WHERE capture_order_id = $1
              AND creditor_user_id = $2
              AND status IN ('reserved', 'confirmed')
            LIMIT 1
            """,
            capture_order_id, creditor_user_id,
        )
        return row is not None

    async def transfer_to_buyer(
        self, position_id: int, buyer_user_id: int
    ) -> None:
        """
        Transfers a confirmed position to a new creditor (secondary market settlement).
        The position_id remains the same; only creditor_user_id changes.
        """
        await self._db.execute(
            """
            UPDATE creditor_positions
            SET creditor_user_id = $1
            WHERE id = $2 AND status = 'confirmed'
            """,
            buyer_user_id, position_id,
        )
