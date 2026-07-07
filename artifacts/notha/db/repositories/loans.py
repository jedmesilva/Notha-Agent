"""
LoanRepository — loan_requests e proposed_installments.

Separa proposta (o que o usuário pediu) da realidade (debts).
Se uma solicitação for rejeitada, proposed_installments nunca poluem debts.
"""
import asyncpg
from decimal import Decimal
from datetime import date
from db.connection import DB


class LoanRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── loan_requests ─────────────────────────────────────────────────────────

    async def create_request(
        self,
        user_id: int,
        level_id: int,
        requested_amount: Decimal,
    ) -> int:
        """Cria uma solicitação de empréstimo no status 'pending'. Retorna o id."""
        return await self._db.fetch_val(
            """
            INSERT INTO loan_requests (user_id, level_id, requested_amount)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            user_id, level_id, requested_amount,
        )

    async def get_by_id(self, request_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM loan_requests WHERE id = $1", request_id
        )

    async def list_by_user(
        self, user_id: int, status: str | None = None, limit: int = 10
    ) -> list[asyncpg.Record]:
        if status:
            return await self._db.fetch_all(
                """
                SELECT * FROM loan_requests
                WHERE user_id = $1 AND status = $2
                ORDER BY requested_at DESC LIMIT $3
                """,
                user_id, status, limit,
            )
        return await self._db.fetch_all(
            """
            SELECT * FROM loan_requests
            WHERE user_id = $1
            ORDER BY requested_at DESC LIMIT $2
            """,
            user_id, limit,
        )

    async def update_status(
        self,
        request_id: int,
        status: str,
        decided_by: str = "system",
        rejection_reason: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE loan_requests
               SET status           = $1,
                   decided_at       = NOW(),
                   decided_by       = $2,
                   rejection_reason = $3
             WHERE id = $4
            """,
            status, decided_by, rejection_reason, request_id,
        )

    async def active_debt_total(self, user_id: int, level_id: int) -> Decimal:
        """Soma do principal de dívidas ativas do usuário neste nível (para validação de limite)."""
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(d.principal), 0)
            FROM debts d
            JOIN loan_requests lr ON lr.id = d.loan_request_id
            WHERE lr.user_id  = $1
              AND lr.level_id = $2
              AND d.status    = 'active'
            """,
            user_id, level_id,
        )
        return Decimal(str(val or 0))

    # ── proposed_installments ─────────────────────────────────────────────────

    async def add_proposed_installment(
        self,
        loan_request_id: int,
        sequence: int,
        proposed_due_date: date,
        proposed_amount: Decimal,
        distribution_type: str = "equal",
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO proposed_installments
                (loan_request_id, sequence, proposed_due_date, proposed_amount, distribution_type)
            VALUES ($1, $2, $3, $4, $5)
            """,
            loan_request_id, sequence, proposed_due_date, proposed_amount, distribution_type,
        )

    async def get_proposed_installments(
        self, loan_request_id: int
    ) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM proposed_installments
            WHERE loan_request_id = $1
            ORDER BY sequence
            """,
            loan_request_id,
        )

    async def add_proposed_installments_bulk(
        self,
        loan_request_id: int,
        installments: list[dict],
    ) -> None:
        """
        installments: lista de dicts com chaves:
          sequence, proposed_due_date (date), proposed_amount (Decimal),
          distribution_type (str, default 'equal')
        """
        for inst in installments:
            await self.add_proposed_installment(
                loan_request_id=loan_request_id,
                sequence=inst["sequence"],
                proposed_due_date=inst["proposed_due_date"],
                proposed_amount=inst["proposed_amount"],
                distribution_type=inst.get("distribution_type", "equal"),
            )
