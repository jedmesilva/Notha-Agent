"""
CaptureOrderRepository — data access for capture_orders table.

A CaptureOrder is the offer distributed to potential creditors.
One CaptureOrder per active CaptureRequest.

Status machine:
  open → complete | partial_expired | cancelled

IMPORTANT: committed_amount tracks only CONFIRMED positions.
           Reserved positions are held but do not count toward closure.
"""
import logging
from decimal import Decimal
from datetime import datetime
from typing import Optional

logger = logging.getLogger("notha.repo.capture_orders")


class CaptureOrderRepository:
    def __init__(self, db):
        self._db = db

    async def create(
        self,
        *,
        capture_request_id: int,
        target_amount: Decimal,
        minimum_threshold: Decimal,
        approved_rate: Decimal,
        creditor_rate: Decimal,
        origination_fee_pct: Decimal,
        servicing_fee_pct: Decimal,
        capture_deadline: datetime,
    ) -> int:
        """Creates an open CaptureOrder. Returns its id."""
        row = await self._db.fetch_one(
            """
            INSERT INTO capture_orders
                (capture_request_id, target_amount, minimum_threshold,
                 approved_rate, creditor_rate, origination_fee_pct,
                 servicing_fee_pct, capture_deadline, status, committed_amount)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'open', 0)
            RETURNING id
            """,
            capture_request_id,
            target_amount,
            minimum_threshold,
            approved_rate,
            creditor_rate,
            origination_fee_pct,
            servicing_fee_pct,
            capture_deadline,
        )
        return row["id"]

    async def get_by_id(self, order_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM capture_orders WHERE id = $1",
            order_id,
        )
        return dict(row) if row else None

    async def get_active_for_request(self, capture_request_id: int) -> Optional[dict]:
        """Returns the open CaptureOrder for a given request, if any."""
        row = await self._db.fetch_one(
            """
            SELECT * FROM capture_orders
            WHERE capture_request_id = $1 AND status = 'open'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            capture_request_id,
        )
        return dict(row) if row else None

    async def list_open(self, limit: int = 20) -> list[dict]:
        rows = await self._db.fetch_all(
            """
            SELECT co.*, cr.user_id AS debtor_id, cr.requested_amount
            FROM capture_orders co
            JOIN capture_requests cr ON cr.id = co.capture_request_id
            WHERE co.status = 'open' AND co.capture_deadline > NOW()
            ORDER BY co.created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]

    async def add_confirmed_commitment(
        self, order_id: int, amount: Decimal
    ) -> str:
        """
        Atomically increments committed_amount and transitions to 'complete' when funded.

        Uses a single SQL statement to avoid TOCTOU races:
          - Only updates rows WHERE status = 'open' — late confirms on closed orders are no-ops.
          - Sets status → 'complete' and records completed_at in the same statement when
            the new committed_amount reaches target_amount.

        Returns new status string ('open', 'complete') or 'not_found' if the row
        was not open (already complete, cancelled, or non-existent).
        """
        row = await self._db.fetch_one(
            """
            UPDATE capture_orders
               SET committed_amount = committed_amount + $1,
                   status       = CASE
                                    WHEN committed_amount + $1 >= target_amount THEN 'complete'
                                    ELSE status
                                  END,
                   completed_at = CASE
                                    WHEN committed_amount + $1 >= target_amount THEN NOW()
                                    ELSE completed_at
                                  END
             WHERE id = $2
               AND status = 'open'
            RETURNING status
            """,
            amount, order_id,
        )
        if not row:
            # Order was not open — commit is a no-op
            return "not_found"

        return row["status"]

    async def update_status(
        self,
        order_id: int,
        status: str,
        completed_at: Optional[datetime] = None,
    ) -> None:
        valid = ('open', 'complete', 'partial_expired', 'cancelled')
        if status not in valid:
            raise ValueError(f"Invalid capture_order status: {status!r}")

        if completed_at:
            await self._db.execute(
                "UPDATE capture_orders SET status = $1, completed_at = $2 WHERE id = $3",
                status, completed_at, order_id,
            )
        else:
            await self._db.execute(
                "UPDATE capture_orders SET status = $1 WHERE id = $2",
                status, order_id,
            )

    async def list_expired_open(self) -> list[dict]:
        """Returns open orders whose capture_deadline has passed."""
        rows = await self._db.fetch_all(
            """
            SELECT * FROM capture_orders
            WHERE status = 'open' AND capture_deadline <= NOW()
            """,
        )
        return [dict(r) for r in rows]
