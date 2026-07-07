"""
CreditLimitRepository — thin wrapper mantido para compatibilidade.

A lógica de limites de crédito foi unificada em LevelRepository
(artifacts/notha/db/repositories/levels.py) para manter
cohesão com as políticas de nível.

Este módulo reexporta a interface necessária para que engines
que usam CreditLimitRepository não precisem ser reescritos.
"""
import logging
from decimal import Decimal
from db.connection import DB
from db.repositories.levels import LevelRepository

logger = logging.getLogger("notha.credit_limits")


class CreditLimitRepository:
    """
    Delegação total para LevelRepository.
    Mantido como alias de compatibilidade.
    """
    def __init__(self, db: DB):
        self._db = db
        self._levels = LevelRepository(db)

    async def get_individual_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        lender_level_id: int,
    ):
        if borrower_type != "user":
            return None
        return await self._levels.get_individual_limit(borrower_id, lender_level_id)

    async def set_individual_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        lender_level_id: int,
        *,
        mode: str = "score_band",
        limit_amount: Decimal | None = None,
        limit_percentage: Decimal | None = None,
    ) -> None:
        await self._levels.set_individual_limit(
            borrower_id, lender_level_id,
            mode=mode,
            limit_amount=limit_amount,
            limit_percentage=limit_percentage,
        )

    async def get_pool_limit(self, level_id: int):
        """Compatibilidade — retorna a policy ativa do nível."""
        return await self._levels.get_policy(level_id)

    async def increment_exposure(self, level_id: int, amount: Decimal) -> None:
        await self._levels.increment_exposure(level_id, amount)

    async def decrement_exposure(self, level_id: int, amount: Decimal) -> None:
        await self._levels.decrement_exposure(level_id, amount)

    async def resolve_effective_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        level_id: int,
        user_risk_score: Decimal,
    ) -> tuple[Decimal | None, str, Decimal | None]:
        if borrower_type != "user":
            return Decimal("0"), "unsupported_borrower_type", None
        return await self._levels.resolve_effective_limit(
            borrower_id, level_id, user_risk_score
        )

    async def validate_limits(
        self,
        borrower_type: str,
        borrower_id: int,
        level_id: int,
        requested_amount: Decimal,
        active_debt_total: Decimal,
        user_risk_score: Decimal = Decimal("500"),
    ) -> tuple[bool, str, dict]:
        return await self._levels.validate_limits(
            borrower_id, level_id, requested_amount,
            active_debt_total, user_risk_score,
        )

    async def record_upgrade_suggestion(
        self,
        user_id: int,
        from_level_id: int,
        trigger_score: Decimal,
        trigger_pct: Decimal | None,
        to_level_id: int | None = None,
    ) -> int:
        """
        Registra sugestão de upgrade de nível via user_level_history
        com reason='suggested_upgrade'.
        Retorna o id do histórico criado.
        """
        result = await self._levels.transition_user_level(
            user_id=user_id,
            to_level=to_level_id or (from_level_id + 1),
            trigger_score=trigger_score,
            reason=f"suggested_upgrade from_level={from_level_id} pct={trigger_pct}",
            changed_by="system",
        )
        return result.get("to_level", to_level_id)

    async def has_pending_upgrade(self, user_id: int, from_level_id: int) -> bool:
        """
        Verifica se já existe transição de nível sugerida recentemente
        (últimas 24h) para evitar duplicar notificações.
        """
        row = await self._db.fetch_one(
            """
            SELECT id FROM user_level_history
            WHERE user_id    = $1
              AND from_level = $2
              AND reason LIKE 'suggested_upgrade%'
              AND changed_at > NOW() - INTERVAL '24 hours'
            LIMIT 1
            """,
            user_id, from_level_id,
        )
        return row is not None
