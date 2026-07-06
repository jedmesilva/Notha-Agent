"""
PaymentRepository — payments e payment_allocations.

payments    = o evento de pagamento (o que entrou no caixa)
payment_allocations = como esse pagamento foi distribuído entre parcelas
"""
import asyncpg
from decimal import Decimal
from db.connection import DB


class PaymentRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── payments ──────────────────────────────────────────────────────────────

    async def create(
        self,
        debt_id: int,
        amount_paid: Decimal,
        payment_method: str = "pix",
        asaas_charge_id: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Registra o evento de pagamento. Retorna o id."""
        return await self._db.fetch_val(
            """
            INSERT INTO payments (debt_id, amount_paid, payment_method, asaas_charge_id, notes)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            debt_id, amount_paid, payment_method, asaas_charge_id, notes,
        )

    async def get_by_id(self, payment_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM payments WHERE id = $1", payment_id
        )

    async def list_by_debt(self, debt_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM payments WHERE debt_id = $1 ORDER BY paid_at DESC",
            debt_id,
        )

    async def total_paid(self, debt_id: int) -> Decimal:
        val = await self._db.fetch_val(
            "SELECT COALESCE(SUM(amount_paid), 0) FROM payments WHERE debt_id = $1",
            debt_id,
        )
        return Decimal(str(val or 0))

    # ── payment_allocations ───────────────────────────────────────────────────

    async def add_allocation(
        self,
        payment_id: int,
        installment_id: int,
        amount_allocated: Decimal,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO payment_allocations (payment_id, installment_id, amount_allocated)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            payment_id, installment_id, amount_allocated,
        )

    async def list_allocations(self, payment_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM payment_allocations WHERE payment_id = $1",
            payment_id,
        )

    async def total_allocated_to_installment(self, installment_id: int) -> Decimal:
        val = await self._db.fetch_val(
            "SELECT COALESCE(SUM(amount_allocated), 0) FROM payment_allocations WHERE installment_id = $1",
            installment_id,
        )
        return Decimal(str(val or 0))
