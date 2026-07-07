"""
InvestorProfileRepository — perfis de preferência e histórico de investidores.

Um perfil define:
  - Tolerância a risco: conservative / moderate / aggressive
  - Faixa de prazo aceita: min_term_days ↔ max_term_days
  - Limites de valor: min_investment_amount / max_investment_amount
  - Modo automático: auto_invest=True investe sem confirmação WhatsApp
  - level_id: nível preferido (NULL = aceita qualquer nível)

Métricas históricas (avg_investment_amount, avg_term_days, etc.) são
calculadas periodicamente pelo job recalculate_investor_metrics.
"""
from decimal import Decimal
from db.connection import DB


_RISK_ORDER = {"conservative": 1, "moderate": 2, "aggressive": 3}


class InvestorProfileRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def get_by_user(self, user_id: int):
        return await self._db.fetch_one(
            "SELECT * FROM investor_profiles WHERE user_id = $1",
            user_id,
        )

    async def upsert(
        self,
        user_id: int,
        level_id: int | None = None,
        risk_tolerance: str = "moderate",
        min_investment_amount: Decimal = Decimal("50"),
        max_investment_amount: Decimal | None = None,
        min_term_days: int = 1,
        max_term_days: int = 365,
        auto_invest: bool = False,
    ) -> int:
        """
        Cria ou atualiza o perfil. Retorna o id do registro.
        level_id=None significa que o investidor aceita qualquer nível.
        """
        return await self._db.fetch_val(
            """
            INSERT INTO investor_profiles
                (user_id, level_id, risk_tolerance, min_investment_amount,
                 max_investment_amount, min_term_days, max_term_days, auto_invest)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (user_id) DO UPDATE SET
                level_id              = EXCLUDED.level_id,
                risk_tolerance        = EXCLUDED.risk_tolerance,
                min_investment_amount = EXCLUDED.min_investment_amount,
                max_investment_amount = EXCLUDED.max_investment_amount,
                min_term_days         = EXCLUDED.min_term_days,
                max_term_days         = EXCLUDED.max_term_days,
                auto_invest           = EXCLUDED.auto_invest,
                updated_at            = NOW()
            RETURNING id
            """,
            user_id, level_id, risk_tolerance,
            min_investment_amount, max_investment_amount,
            min_term_days, max_term_days, auto_invest,
        )

    async def update_metrics(
        self,
        user_id: int,
        avg_investment_amount: Decimal | None,
        avg_term_days: int | None,
        total_invested_lifetime: Decimal,
        active_investment_count: int,
    ) -> None:
        """Atualiza métricas históricas calculadas pelo job periódico."""
        await self._db.execute(
            """
            UPDATE investor_profiles SET
                avg_investment_amount   = $2,
                avg_term_days           = $3,
                total_invested_lifetime = $4,
                active_investment_count = $5,
                last_metrics_at         = NOW(),
                updated_at              = NOW()
            WHERE user_id = $1
            """,
            user_id, avg_investment_amount, avg_term_days,
            total_invested_lifetime, active_investment_count,
        )

    async def deactivate(self, user_id: int) -> None:
        await self._db.execute(
            """
            UPDATE investor_profiles
            SET is_active = FALSE, updated_at = NOW()
            WHERE user_id = $1
            """,
            user_id,
        )

    async def list_active(self, level_id: int | None = None) -> list:
        if level_id:
            return await self._db.fetch_all(
                """
                SELECT ip.*, upn.phone, u.full_name AS user_name
                FROM investor_profiles ip
                JOIN users u ON u.id = ip.user_id
                LEFT JOIN user_phone_numbers upn ON upn.user_id = u.id AND upn.active = TRUE
                WHERE ip.is_active = TRUE
                  AND (ip.level_id IS NULL OR ip.level_id = $1)
                ORDER BY ip.total_invested_lifetime DESC
                """,
                level_id,
            )
        return await self._db.fetch_all(
            """
            SELECT ip.*, upn.phone, u.full_name AS user_name
            FROM investor_profiles ip
            JOIN users u ON u.id = ip.user_id
            LEFT JOIN user_phone_numbers upn ON upn.user_id = u.id AND upn.active = TRUE
            WHERE ip.is_active = TRUE
            ORDER BY ip.total_invested_lifetime DESC
            """
        )

    async def list_all_user_ids(self) -> list[int]:
        """Para recalcular métricas de todos os investidores com perfil."""
        rows = await self._db.fetch_all(
            "SELECT user_id FROM investor_profiles WHERE is_active = TRUE"
        )
        return [r["user_id"] for r in rows]

    # ── Matching ──────────────────────────────────────────────────────────────

    async def find_candidates(
        self,
        level_id: int,
        maturity_days: int,
        risk_level: str = "moderate",
        exclude_user_ids: list[int] | None = None,
    ) -> list:
        """
        Retorna perfis ativos compatíveis com os critérios da oportunidade.

        Filtros aplicados em SQL:
          - Nível compatível (NULL = aceita qualquer nível)
          - Prazo dentro da faixa [min_term_days, max_term_days]
          - Tolerância a risco ≥ nível mínimo da oportunidade
          - Não está na lista de exclusão (já investiu ou já tem oferta)

        auto_invest=TRUE vem primeiro para ser processado antes dos outros.
        """
        min_risk_int = _RISK_ORDER.get(risk_level, 2)
        exclude = exclude_user_ids or []

        return await self._db.fetch_all(
            """
            SELECT
                ip.*,
                upn.phone,
                u.full_name AS user_name
            FROM investor_profiles ip
            JOIN users u ON u.id = ip.user_id
            LEFT JOIN user_phone_numbers upn ON upn.user_id = u.id AND upn.active = TRUE
            WHERE ip.is_active = TRUE
              AND (ip.level_id IS NULL OR ip.level_id = $1)
              AND ip.min_term_days <= $2
              AND ip.max_term_days >= $2
              AND CASE ip.risk_tolerance
                    WHEN 'conservative' THEN 1
                    WHEN 'moderate'     THEN 2
                    WHEN 'aggressive'   THEN 3
                    ELSE 2
                  END >= $3
              AND (cardinality($4::int[]) = 0 OR ip.user_id != ALL($4::int[]))
            ORDER BY
                ip.auto_invest DESC,
                COALESCE(ip.max_investment_amount, 999999999) DESC,
                ip.total_invested_lifetime DESC
            """,
            level_id, maturity_days, min_risk_int, exclude,
        )
