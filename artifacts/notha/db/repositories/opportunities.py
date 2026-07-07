"""
OpportunityRepository — investment_opportunities.

Uma oportunidade é criada automaticamente após cada aprovação de empréstimo
para repor o saldo retirado do pool (nível). Também pode ser criada manualmente
para captação geral de liquidez.

Estados:
  open             → aguardando investidores
  partially_funded → tem algum compromisso, mas não 100%
  fully_funded     → 100% comprometido — fundo reposto
  expired          → expirou sem completar (job periódico marca)
  cancelled        → cancelada manualmente
"""
from decimal import Decimal
from db.connection import DB


class OpportunityRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        level_id: int,
        amount_needed: Decimal,
        expected_rate: Decimal,
        expires_at,
        debt_id: int | None = None,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO investment_opportunities
                (level_id, debt_id, amount_needed, amount_committed,
                 expected_rate, status, expires_at)
            VALUES ($1, $2, $3, 0, $4, 'open', $5)
            RETURNING id
            """,
            level_id, debt_id, amount_needed, expected_rate, expires_at,
        )

    async def get_by_id(self, opp_id: int):
        return await self._db.fetch_one(
            "SELECT * FROM investment_opportunities WHERE id = $1", opp_id
        )

    async def list_open(self, level_id: int | None = None, limit: int = 50) -> list:
        """Lista oportunidades abertas ou parcialmente financiadas."""
        if level_id:
            return await self._db.fetch_all(
                """
                SELECT o.*, lv.name AS level_name,
                       (o.amount_needed - o.amount_committed) AS amount_remaining
                FROM investment_opportunities o
                JOIN levels lv ON lv.id = o.level_id
                WHERE o.level_id = $1
                  AND o.status IN ('open', 'partially_funded')
                  AND o.expires_at > NOW()
                ORDER BY o.created_at DESC
                LIMIT $2
                """,
                level_id, limit,
            )
        return await self._db.fetch_all(
            """
            SELECT o.*, lv.name AS level_name,
                   (o.amount_needed - o.amount_committed) AS amount_remaining
            FROM investment_opportunities o
            JOIN levels lv ON lv.id = o.level_id
            WHERE o.status IN ('open', 'partially_funded')
              AND o.expires_at > NOW()
            ORDER BY o.created_at DESC
            LIMIT $1
            """,
            limit,
        )

    async def list_by_level(self, level_id: int, limit: int = 100) -> list:
        return await self._db.fetch_all(
            """
            SELECT *, (amount_needed - amount_committed) AS amount_remaining
            FROM investment_opportunities
            WHERE level_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            level_id, limit,
        )

    async def add_commitment(self, opp_id: int, amount: Decimal) -> str:
        """
        Incrementa amount_committed e atualiza status.
        Retorna o novo status.
        """
        await self._db.execute(
            """
            UPDATE investment_opportunities
               SET amount_committed = amount_committed + $1
             WHERE id = $2
            """,
            amount, opp_id,
        )
        row = await self._db.fetch_one(
            "SELECT amount_needed, amount_committed FROM investment_opportunities WHERE id = $1",
            opp_id,
        )
        needed    = Decimal(str(row["amount_needed"]))
        committed = Decimal(str(row["amount_committed"]))

        if committed >= needed:
            new_status = "fully_funded"
        elif committed > 0:
            new_status = "partially_funded"
        else:
            new_status = "open"

        await self._db.execute(
            "UPDATE investment_opportunities SET status = $1 WHERE id = $2",
            new_status, opp_id,
        )
        return new_status

    async def cancel(self, opp_id: int) -> None:
        await self._db.execute(
            "UPDATE investment_opportunities SET status = 'cancelled' WHERE id = $1",
            opp_id,
        )

    async def expire_stale(self) -> int:
        """Marca como expired oportunidades abertas que passaram do prazo. Retorna count."""
        result = await self._db.execute(
            """
            UPDATE investment_opportunities
               SET status = 'expired'
             WHERE status IN ('open', 'partially_funded')
               AND expires_at <= NOW()
            """
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    async def get_by_debt(self, debt_id: int):
        return await self._db.fetch_one(
            """
            SELECT * FROM investment_opportunities
            WHERE debt_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            debt_id,
        )
