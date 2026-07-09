"""
CreditInstrumentRepository — data access for credit_instruments and
credit_instrument_installments tables.

A CreditInstrument is the legal debt contract that only exists AFTER
the CaptureOrder reaches 'complete' status. This is the UX "emitir dívida"
moment — but legally it is the SEP emitting the instrumento representativo
do crédito.

Principle: no instrument is created before full creditor capital is committed.
"""
import json
import logging
from decimal import Decimal
from datetime import date
from typing import Optional

logger = logging.getLogger("notha.repo.credit_instruments")


class CreditInstrumentRepository:
    def __init__(self, db):
        self._db = db

    # ── Instruments ───────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        capture_order_id: int,
        debtor_id: int,
        total_amount: Decimal,
        interest_rate: Decimal,
        creditor_rate: Decimal,
        term_days: int,
        payment_plan_final: list[dict],
        origination_fee: Decimal,
        origination_fee_pct: Decimal,
        servicing_fee_pct: Decimal,
        net_disbursed_amount: Decimal,
        allows_assignment: bool = True,
    ) -> int:
        """Creates a CreditInstrument in 'active' status. Returns its id."""
        row = await self._db.fetch_one(
            """
            INSERT INTO credit_instruments
                (capture_order_id, debtor_id, total_amount, interest_rate,
                 creditor_rate, term_days, payment_plan_final, origination_fee,
                 origination_fee_pct, servicing_fee_pct, net_disbursed_amount,
                 allows_assignment, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, 'active')
            RETURNING id
            """,
            capture_order_id, debtor_id, total_amount, interest_rate,
            creditor_rate, term_days, json.dumps(payment_plan_final),
            origination_fee, origination_fee_pct, servicing_fee_pct,
            net_disbursed_amount, allows_assignment,
        )
        return row["id"]

    async def get_by_id(self, instrument_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM credit_instruments WHERE id = $1",
            instrument_id,
        )
        return dict(row) if row else None

    async def get_by_capture_order(self, capture_order_id: int) -> Optional[dict]:
        row = await self._db.fetch_one(
            "SELECT * FROM credit_instruments WHERE capture_order_id = $1",
            capture_order_id,
        )
        return dict(row) if row else None

    async def list_by_debtor(
        self,
        debtor_id: int,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM credit_instruments
                WHERE debtor_id = $1 AND status = $2
                ORDER BY issue_date DESC LIMIT $3
                """,
                debtor_id, status, limit,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM credit_instruments
                WHERE debtor_id = $1
                ORDER BY issue_date DESC LIMIT $2
                """,
                debtor_id, limit,
            )
        return [dict(r) for r in rows]

    async def update_status(self, instrument_id: int, status: str) -> None:
        valid = ('active', 'paid_off', 'defaulted', 'renegotiated')
        if status not in valid:
            raise ValueError(f"Invalid credit instrument status: {status!r}")
        await self._db.execute(
            "UPDATE credit_instruments SET status = $1 WHERE id = $2",
            status, instrument_id,
        )

    # ── Installments ──────────────────────────────────────────────────────────

    async def add_installments_bulk(
        self,
        credit_instrument_id: int,
        installments: list[dict],
    ) -> None:
        """Bulk-inserts installment records. Each dict must have:
        {sequence, due_date, amount_due}."""
        for inst in installments:
            amount = Decimal(str(inst["amount_due"]))
            await self._db.execute(
                """
                INSERT INTO credit_instrument_installments
                    (credit_instrument_id, sequence, due_date, amount_due, remaining_amount, status)
                VALUES ($1, $2, $3, $4, $4, 'pending')
                """,
                credit_instrument_id,
                inst["sequence"],
                inst["due_date"],
                amount,
            )

    async def list_installments(
        self,
        credit_instrument_id: int,
        status: Optional[str] = None,
    ) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM credit_instrument_installments
                WHERE credit_instrument_id = $1 AND status = $2
                ORDER BY sequence ASC
                """,
                credit_instrument_id, status,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT * FROM credit_instrument_installments
                WHERE credit_instrument_id = $1
                ORDER BY sequence ASC
                """,
                credit_instrument_id,
            )
        return [dict(r) for r in rows]

    async def list_open_installments(self, credit_instrument_id: int) -> list[dict]:
        """Returns installments that are not yet fully paid, ordered by due_date (FIFO)."""
        rows = await self._db.fetch_all(
            """
            SELECT * FROM credit_instrument_installments
            WHERE credit_instrument_id = $1
              AND status IN ('pending', 'partially_paid', 'overdue')
            ORDER BY due_date ASC, sequence ASC
            """,
            credit_instrument_id,
        )
        return [dict(r) for r in rows]

    async def apply_installment_allocation(
        self,
        installment_id: int,
        amount_applied: Decimal,
    ) -> str:
        """Applies a payment allocation to an installment. Returns new status."""
        row = await self._db.fetch_one(
            """
            UPDATE credit_instrument_installments
            SET remaining_amount = GREATEST(0, remaining_amount - $1),
                status = CASE
                    WHEN remaining_amount - $1 <= 0 THEN 'paid'
                    ELSE 'partially_paid'
                END
            WHERE id = $2
            RETURNING status
            """,
            amount_applied, installment_id,
        )
        return row["status"] if row else "unknown"

    async def mark_overdue(self, credit_instrument_id: int) -> int:
        """Marks pending installments past due_date as 'overdue'. Returns count."""
        row = await self._db.fetch_one(
            """
            WITH updated AS (
                UPDATE credit_instrument_installments
                SET status = 'overdue'
                WHERE credit_instrument_id = $1
                  AND status IN ('pending', 'partially_paid')
                  AND due_date < CURRENT_DATE
                RETURNING 1
            )
            SELECT COUNT(*) AS cnt FROM updated
            """,
            credit_instrument_id,
        )
        return int(row["cnt"]) if row else 0

    async def is_fully_paid(self, credit_instrument_id: int) -> bool:
        """Returns True if all installments are in 'paid' status."""
        row = await self._db.fetch_one(
            """
            SELECT COUNT(*) AS open_count
            FROM credit_instrument_installments
            WHERE credit_instrument_id = $1
              AND status != 'paid'
            """,
            credit_instrument_id,
        )
        return int(row["open_count"]) == 0 if row else False

    async def get_summary(self, instrument_id: int) -> dict:
        """Returns a consolidated summary of the instrument and its installments."""
        instrument = await self.get_by_id(instrument_id)
        if not instrument:
            return {}

        installments = await self.list_installments(instrument_id)
        open_insts = [i for i in installments if i["status"] != "paid"]
        overdue = [i for i in installments if i["status"] == "overdue"]

        total_remaining = sum(Decimal(str(i["remaining_amount"])) for i in open_insts)
        next_due = min((i["due_date"] for i in open_insts), default=None)
        total_paid = Decimal(str(instrument["total_amount"])) - total_remaining

        return {
            "instrument_id":       instrument_id,
            "debtor_id":           instrument["debtor_id"],
            "total_amount":        Decimal(str(instrument["total_amount"])),
            "interest_rate":       Decimal(str(instrument["interest_rate"])),
            "origination_fee":     Decimal(str(instrument["origination_fee"])),
            "net_disbursed":       Decimal(str(instrument["net_disbursed_amount"])),
            "status":              instrument["status"],
            "total_installments":  len(installments),
            "open_installments":   len(open_insts),
            "overdue_installments": len(overdue),
            "total_paid":          total_paid,
            "total_remaining":     total_remaining,
            "next_due_date":       next_due,
            "issue_date":          instrument["issue_date"],
            "allows_assignment":   instrument["allows_assignment"],
        }
