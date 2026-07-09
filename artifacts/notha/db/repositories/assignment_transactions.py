"""
AssignmentTransactionRepository — data access for assignment_transactions table.

Handles secondary market transfers of creditor positions.

Key compliance requirements:
  - Price MUST come from the pricing engine (never free-text)
  - Debtor notification is mandatory (Art. 290 CC) before/at settlement
  - SCD participation is never automatic or guaranteed

Status machine:
  proposed → accepted → settled | refused
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("notha.repo.assignment_transactions")


class AssignmentTransactionRepository:
    def __init__(self, db):
        self._db = db

    async def create(
        self,
        *,
        credit_instrument_id: int,
        seller_position_id: int,
        buyer_user_id: int,
        negotiated_price: Decimal,
        pricing_breakdown: dict,
    ) -> int:
        """Creates a new AssignmentTransaction in 'proposed' status. Returns its id."""
        row = await self._db.fetch_one(
            """
            INSERT INTO assignment_transactions
                (credit_instrument_id, seller_position_id, buyer_user_id,
                 negotiated_price, pricing_breakdown, status)
            VALUES ($1, $2, $3, $4, $5::jsonb, 'proposed')
            RETURNING id
            """,
            credit_instrument_id,
            seller_position_id,
            buyer_user_id,
            negotiated_price,
            json.dumps(pricing_breakdown),
        )
        return row["id"]

    async def get_by_id(self, tx_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM assignment_transactions WHERE id = $1",
            tx_id,
        )
        return dict(row) if row else None

    async def accept(self, tx_id: int) -> None:
        """Buyer confirms they want to proceed. Transitions proposed → accepted."""
        now = datetime.now(timezone.utc)
        await self._db.execute(
            """
            UPDATE assignment_transactions
            SET status = 'accepted', accepted_at = $1
            WHERE id = $2 AND status = 'proposed'
            """,
            now, tx_id,
        )

    async def settle(self, tx_id: int, debtor_notification_date: datetime) -> None:
        """
        Finalises the transfer. Records mandatory debtor notification date.
        Transitions accepted → settled.
        """
        now = datetime.now(timezone.utc)
        await self._db.execute(
            """
            UPDATE assignment_transactions
            SET status = 'settled',
                settled_at = $1,
                debtor_notification_date = $2
            WHERE id = $3 AND status = 'accepted'
            """,
            now, debtor_notification_date, tx_id,
        )

    async def refuse(self, tx_id: int) -> None:
        """Seller or buyer backs out. Transitions proposed/accepted → refused."""
        now = datetime.now(timezone.utc)
        await self._db.execute(
            """
            UPDATE assignment_transactions
            SET status = 'refused', refused_at = $1
            WHERE id = $2 AND status IN ('proposed', 'accepted')
            """,
            now, tx_id,
        )

    async def list_by_instrument(
        self,
        credit_instrument_id: int,
        status: Optional[str] = None,
    ) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM assignment_transactions
                WHERE credit_instrument_id = $1 AND status = $2
                ORDER BY proposed_at DESC
                """,
                credit_instrument_id, status,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM assignment_transactions
                WHERE credit_instrument_id = $1
                ORDER BY proposed_at DESC
                """,
                credit_instrument_id,
            )
        return [dict(r) for r in rows]

    async def list_by_user(
        self,
        user_id: int,
        role: str = "buyer",
        limit: int = 20,
    ) -> list[dict]:
        """Lists transactions where the user is buyer or (indirectly) seller."""
        if role == "buyer":
            rows = await self._db.fetch_all(
                """
                SELECT at.*, ci.debtor_id, ci.total_amount
                FROM assignment_transactions at
                JOIN credit_instruments ci ON ci.id = at.credit_instrument_id
                WHERE at.buyer_user_id = $1
                ORDER BY at.proposed_at DESC
                LIMIT $2
                """,
                user_id, limit,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT at.*, ci.debtor_id, ci.total_amount
                FROM assignment_transactions at
                JOIN credit_instruments ci ON ci.id = at.credit_instrument_id
                JOIN creditor_positions cp ON cp.id = at.seller_position_id
                WHERE cp.creditor_user_id = $1
                ORDER BY at.proposed_at DESC
                LIMIT $2
                """,
                user_id, limit,
            )
        return [dict(r) for r in rows]

    async def has_pending_for_position(self, position_id: int) -> bool:
        """Returns True if there is already an active proposal for this position."""
        row = await self._db.fetch_one(
            """
            SELECT 1 FROM assignment_transactions
            WHERE seller_position_id = $1
              AND status IN ('proposed', 'accepted')
            LIMIT 1
            """,
            position_id,
        )
        return row is not None
