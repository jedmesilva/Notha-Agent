"""
CreditLimitRepository — credit_limits e group_pool_limits.

Dois tipos de limite:
  A) credit_limits     — individual: quanto um borrower pode pegar de um grupo
  B) group_pool_limits — agregado:   teto total de exposição do grupo credor
"""
import asyncpg
from decimal import Decimal
from db.connection import DB


class CreditLimitRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── credit_limits (individual) ────────────────────────────────────────────

    async def get_individual_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        lender_group_id: int,
    ) -> asyncpg.Record | None:
        """Retorna o limite individual mais recente (effective_from mais alto)."""
        return await self._db.fetch_one(
            """
            SELECT * FROM credit_limits
            WHERE borrower_type    = $1
              AND borrower_id      = $2
              AND lender_group_id  = $3
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            borrower_type, borrower_id, lender_group_id,
        )

    async def set_individual_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        lender_group_id: int,
        limit_amount: Decimal,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO credit_limits (borrower_type, borrower_id, lender_group_id, limit_amount)
            VALUES ($1, $2, $3, $4)
            """,
            borrower_type, borrower_id, lender_group_id, limit_amount,
        )

    # ── group_pool_limits (agregado) ──────────────────────────────────────────

    async def get_pool_limit(self, group_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM group_pool_limits
            WHERE group_id = $1
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            group_id,
        )

    async def increment_exposure(self, group_id: int, amount: Decimal) -> None:
        """Incrementa current_exposure_cache ao conceder um novo empréstimo."""
        await self._db.execute(
            """
            UPDATE group_pool_limits
               SET current_exposure_cache = current_exposure_cache + $1
             WHERE group_id = $2
               AND id = (
                   SELECT id FROM group_pool_limits
                   WHERE group_id = $2
                   ORDER BY effective_from DESC
                   LIMIT 1
               )
            """,
            amount, group_id,
        )

    async def decrement_exposure(self, group_id: int, amount: Decimal) -> None:
        """Decrementa current_exposure_cache ao quitar/baixar uma dívida."""
        await self._db.execute(
            """
            UPDATE group_pool_limits
               SET current_exposure_cache = GREATEST(0, current_exposure_cache - $1)
             WHERE group_id = $2
               AND id = (
                   SELECT id FROM group_pool_limits
                   WHERE group_id = $2
                   ORDER BY effective_from DESC
                   LIMIT 1
               )
            """,
            amount, group_id,
        )

    # ── validação ─────────────────────────────────────────────────────────────

    async def validate_limits(
        self,
        borrower_type: str,
        borrower_id: int,
        group_id: int,
        requested_amount: Decimal,
        active_debt_total: Decimal,
    ) -> tuple[bool, str]:
        """
        Executa as duas validações do documento (seção 6) em sequência.
        Retorna (aprovado, motivo_rejeição).
        """
        # 1. Limite individual
        ind = await self.get_individual_limit(borrower_type, borrower_id, group_id)
        if ind:
            limit = Decimal(str(ind["limit_amount"]))
            if active_debt_total + requested_amount > limit:
                return False, (
                    f"Limite individual excedido: dívidas ativas "
                    f"R$ {active_debt_total:.2f} + solicitado R$ {requested_amount:.2f} "
                    f"> limite R$ {limit:.2f}"
                )

        # 2. Limite agregado do grupo
        pool = await self.get_pool_limit(group_id)
        if pool:
            max_exp = Decimal(str(pool["max_aggregate_exposure"]))
            cur_exp = Decimal(str(pool["current_exposure_cache"]))
            if cur_exp + requested_amount > max_exp:
                return False, (
                    f"Teto de exposição do grupo atingido: exposição atual "
                    f"R$ {cur_exp:.2f} + solicitado R$ {requested_amount:.2f} "
                    f"> máximo R$ {max_exp:.2f}"
                )

        return True, ""
