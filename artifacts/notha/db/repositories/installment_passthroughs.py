"""
InstallmentPassthroughRepository — data access for installment_passthroughs table.

Records each debtor payment being distributed proportionally to creditors
who hold positions AT THE TIME OF PAYMENT (not necessarily original creditors).

Compliance rules:
  - Platform fee (servicing) is ALWAYS a separate, visible line — never buried.
  - Distribution is proportional to each creditor's participation_fraction.
  - If assignments occurred, the current position holders receive the payment.
"""
import json
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("notha.repo.installment_passthroughs")


class InstallmentPassthroughRepository:
    def __init__(self, db):
        self._db = db

    async def create(
        self,
        *,
        credit_instrument_id: int,
        installment_id: int,
        installment_number: int,
        total_amount_received: Decimal,
        total_servicing_fee: Decimal,
        total_net_distributed: Decimal,
        distribution: list[dict],
    ) -> int:
        """
        Records a payment passthrough.

        distribution format:
        [
          {
            "creditor_user_id": int,
            "position_id": int,
            "gross_amount": "100.00",
            "servicing_fee": "2.00",
            "net_amount": "98.00"
          },
          ...
        ]
        """
        row = await self._db.fetch_one(
            """
            INSERT INTO installment_passthroughs
                (credit_instrument_id, installment_id, installment_number,
                 total_amount_received, total_servicing_fee, total_net_distributed,
                 distribution)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            credit_instrument_id,
            installment_id,
            installment_number,
            total_amount_received,
            total_servicing_fee,
            total_net_distributed,
            json.dumps([
                {
                    **entry,
                    "gross_amount":    str(entry.get("gross_amount", "0")),
                    "servicing_fee":   str(entry.get("servicing_fee", "0")),
                    "net_amount":      str(entry.get("net_amount", "0")),
                }
                for entry in distribution
            ]),
        )
        return row["id"]

    async def get_by_id(self, passthrough_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM installment_passthroughs WHERE id = $1",
            passthrough_id,
        )
        return dict(row) if row else None

    async def list_by_instrument(
        self,
        credit_instrument_id: int,
        limit: int = 50,
    ) -> list[dict]:
        rows = await self._db.fetch_all(
            """
            SELECT * FROM installment_passthroughs
            WHERE credit_instrument_id = $1
            ORDER BY installment_number ASC
            LIMIT $2
            """,
            credit_instrument_id, limit,
        )
        return [dict(r) for r in rows]

    async def total_platform_fees_collected(self, credit_instrument_id: int) -> Decimal:
        """Returns total servicing fees collected across all passthroughs."""
        row = await self._db.fetch_one(
            """
            SELECT COALESCE(SUM(total_servicing_fee), 0) AS total
            FROM installment_passthroughs
            WHERE credit_instrument_id = $1
            """,
            credit_instrument_id,
        )
        return Decimal(str(row["total"])) if row else Decimal("0")

    async def creditor_earnings(
        self,
        creditor_user_id: int,
        limit: int = 50,
    ) -> list[dict]:
        """
        Returns passthrough records where this creditor received a distribution.
        Filters by creditor_user_id inside the distribution JSONB array.
        """
        rows = await self._db.fetch_all(
            """
            SELECT ip.*, ci.debtor_id, ci.total_amount
            FROM installment_passthroughs ip
            JOIN credit_instruments ci ON ci.id = ip.credit_instrument_id
            WHERE ip.distribution @> $1::jsonb
            ORDER BY ip.processed_at DESC
            LIMIT $2
            """,
            json.dumps([{"creditor_user_id": creditor_user_id}]),
            limit,
        )
        return [dict(r) for r in rows]
