"""
DebtRepository — debts e debt_installments.

Cobre o contrato real: só criado após aprovação de loan_request.
interest_rate_applied é snapshot imutável da cotação.
"""
import asyncpg
from decimal import Decimal
from datetime import date
from db.connection import DB


class DebtRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── debts ─────────────────────────────────────────────────────────────────

    async def create(
        self,
        loan_request_id: int,
        from_wallet_id: int,
        to_wallet_id: int,
        principal: Decimal,
        interest_rate_applied: Decimal,
        term_days: int,
        overpayment_strategy: str = "advance_installments",
    ) -> int:
        """Cria o registro de dívida e marca como 'active'. Retorna o id."""
        return await self._db.fetch_val(
            """
            INSERT INTO debts
                (loan_request_id, from_wallet_id, to_wallet_id, principal,
                 interest_rate_applied, term_days, status, overpayment_strategy, disbursed_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'active', $7, NOW())
            RETURNING id
            """,
            loan_request_id, from_wallet_id, to_wallet_id, principal,
            interest_rate_applied, term_days, overpayment_strategy,
        )

    async def get_by_id(self, debt_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM debts WHERE id = $1", debt_id)

    async def get_by_loan_request(self, loan_request_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM debts WHERE loan_request_id = $1", loan_request_id
        )

    async def list_active_by_group(self, group_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT d.* FROM debts d
            JOIN loan_requests lr ON lr.id = d.loan_request_id
            WHERE lr.group_id = $1 AND d.status = 'active'
            """,
            group_id,
        )

    async def total_active_principal_by_group(self, group_id: int) -> Decimal:
        """Exposição total ativa de um grupo (para group_pool_limits)."""
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(d.principal), 0)
            FROM debts d
            JOIN loan_requests lr ON lr.id = d.loan_request_id
            WHERE lr.group_id = $1 AND d.status = 'active'
            """,
            group_id,
        )
        return Decimal(str(val or 0))

    async def update_status(self, debt_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE debts SET status = $1 WHERE id = $2", status, debt_id
        )

    async def reduce_principal(self, debt_id: int, amount: Decimal) -> None:
        """Abate de principal (estratégia reduce_principal em overpayments)."""
        await self._db.execute(
            "UPDATE debts SET principal = GREATEST(0, principal - $1) WHERE id = $2",
            amount, debt_id,
        )

    # ── debt_installments ─────────────────────────────────────────────────────

    async def add_installment(
        self,
        debt_id: int,
        sequence: int,
        due_date: date,
        amount_due: Decimal,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO debt_installments
                (debt_id, sequence, due_date, amount_due, remaining_amount, status)
            VALUES ($1, $2, $3, $4, $4, 'pending')
            RETURNING id
            """,
            debt_id, sequence, due_date, amount_due,
        )

    async def add_installments_bulk(
        self, debt_id: int, installments: list[dict]
    ) -> None:
        """installments: list of {sequence, due_date, amount_due}"""
        for inst in installments:
            await self.add_installment(
                debt_id=debt_id,
                sequence=inst["sequence"],
                due_date=inst["due_date"],
                amount_due=inst["amount_due"],
            )

    async def get_installment(self, installment_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM debt_installments WHERE id = $1", installment_id
        )

    async def list_open_installments(self, debt_id: int) -> list[asyncpg.Record]:
        """Parcelas em aberto (pending, partially_paid, overdue) por ordem FIFO."""
        return await self._db.fetch_all(
            """
            SELECT * FROM debt_installments
            WHERE debt_id = $1
              AND status != 'paid'
            ORDER BY due_date ASC, sequence ASC
            """,
            debt_id,
        )

    async def list_all_installments(self, debt_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM debt_installments
            WHERE debt_id = $1
            ORDER BY sequence ASC
            """,
            debt_id,
        )

    async def apply_allocation(
        self, installment_id: int, allocated: Decimal
    ) -> str:
        """
        Subtrai `allocated` de remaining_amount e atualiza status.
        Retorna o novo status da parcela.
        """
        row = await self._db.fetch_one(
            "SELECT remaining_amount FROM debt_installments WHERE id = $1",
            installment_id,
        )
        if not row:
            return "unknown"
        new_remaining = Decimal(str(row["remaining_amount"])) - allocated
        if new_remaining <= 0:
            new_remaining = Decimal("0")
            new_status = "paid"
        else:
            new_status = "partially_paid"
        await self._db.execute(
            """
            UPDATE debt_installments
               SET remaining_amount = $1,
                   status           = $2
             WHERE id = $3
            """,
            new_remaining, new_status, installment_id,
        )
        return new_status

    async def check_debt_fully_paid(self, debt_id: int) -> bool:
        """Retorna True se todas as parcelas estão pagas."""
        unpaid = await self._db.fetch_val(
            """
            SELECT COUNT(*) FROM debt_installments
            WHERE debt_id = $1 AND status != 'paid'
            """,
            debt_id,
        )
        return (unpaid or 0) == 0
