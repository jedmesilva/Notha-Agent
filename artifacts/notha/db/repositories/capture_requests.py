"""
CaptureRequestRepository — data access for capture_requests table.

A CaptureRequest is the borrower's initial intent in the P2P flow.
No capital is committed at this stage — it is just a request.

Status machine:
  draft → in_capture → captured | partial_expired | cancelled
"""
import json
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("notha.repo.capture_requests")


class CaptureRequestRepository:
    def __init__(self, db):
        self._db = db

    async def create(
        self,
        *,
        user_id: int,
        level_id: int,
        requested_amount: Decimal,
        term_days: int,
        payment_plan: list[dict],
        proposed_rate: Optional[Decimal] = None,
        credit_score_at_request: Optional[Decimal] = None,
    ) -> int:
        """Creates a new CaptureRequest in 'draft' status. Returns its id."""
        row = await self._db.fetch_one(
            """
            INSERT INTO capture_requests
                (user_id, level_id, requested_amount, term_days,
                 payment_plan, proposed_rate, credit_score_at_request, status)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'draft')
            RETURNING id
            """,
            user_id,
            level_id,
            requested_amount,
            term_days,
            json.dumps(payment_plan),
            proposed_rate,
            credit_score_at_request,
        )
        return row["id"]

    async def get_by_id(self, request_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM capture_requests WHERE id = $1",
            request_id,
        )
        return dict(row) if row else None

    async def list_by_user(
        self,
        user_id: int,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM capture_requests
                WHERE user_id = $1 AND status = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                user_id, status, limit,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM capture_requests
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                user_id, limit,
            )
        return [dict(r) for r in rows]

    async def update_status(
        self,
        request_id: int,
        status: str,
        rejection_reason: Optional[str] = None,
        credit_score: Optional[Decimal] = None,
    ) -> None:
        valid = ('draft', 'in_capture', 'captured', 'partial_expired', 'cancelled')
        if status not in valid:
            raise ValueError(f"Invalid status: {status!r}")

        await self._db.execute(
            """
            UPDATE capture_requests
            SET status = $1,
                rejection_reason = COALESCE($2, rejection_reason),
                credit_score_at_request = COALESCE($3, credit_score_at_request),
                updated_at = NOW()
            WHERE id = $4
            """,
            status, rejection_reason, credit_score, request_id,
        )

    async def set_credit_score(self, request_id: int, score: Decimal) -> None:
        await self._db.execute(
            "UPDATE capture_requests SET credit_score_at_request = $1, updated_at = NOW() WHERE id = $2",
            score, request_id,
        )

    async def active_debt_total(self, user_id: int) -> Decimal:
        """Total amount in active credit instruments for this user (as debtor)."""
        row = await self._db.fetch_one(
            """
            SELECT COALESCE(SUM(ci.total_amount), 0) AS total
            FROM credit_instruments ci
            WHERE ci.debtor_id = $1 AND ci.status = 'active'
            """,
            user_id,
        )
        return Decimal(str(row["total"])) if row else Decimal("0")
