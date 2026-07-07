"""
InvestmentRepository — investments + investment_payouts.

Investimento = posição de um investidor no fundo (nível).
Vinculado a uma oportunidade específica (debt_id rastreável) ou geral.

Payouts são agendados no vencimento e pagos via job periódico.
"""
from decimal import Decimal
from datetime import date
from db.connection import DB


class InvestmentRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── investments ───────────────────────────────────────────────────────────

    async def create(
        self,
        investor_user_id: int,
        level_id: int,
        amount_invested: Decimal,
        rate_agreed: Decimal,
        opportunity_id: int | None = None,
        debt_id: int | None = None,
        maturity_date: date | None = None,
        maturity_at=None,                    # datetime | None — precisão de minutos/horas
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO investments
                (investor_user_id, level_id, opportunity_id, debt_id,
                 amount_invested, rate_agreed, status, maturity_date, maturity_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'active', $7, $8)
            RETURNING id
            """,
            investor_user_id, level_id, opportunity_id, debt_id,
            amount_invested, rate_agreed, maturity_date, maturity_at,
        )

    async def get_by_id(self, investment_id: int):
        return await self._db.fetch_one(
            "SELECT * FROM investments WHERE id = $1", investment_id
        )

    async def list_by_investor(
        self,
        investor_user_id: int,
        status: str | None = None,
        limit: int = 50,
    ) -> list:
        if status:
            return await self._db.fetch_all(
                """
                SELECT i.*, lv.name AS level_name
                FROM investments i
                JOIN levels lv ON lv.id = i.level_id
                WHERE i.investor_user_id = $1 AND i.status = $2
                ORDER BY i.invested_at DESC
                LIMIT $3
                """,
                investor_user_id, status, limit,
            )
        return await self._db.fetch_all(
            """
            SELECT i.*, lv.name AS level_name
            FROM investments i
            JOIN levels lv ON lv.id = i.level_id
            WHERE i.investor_user_id = $1
            ORDER BY i.invested_at DESC
            LIMIT $2
            """,
            investor_user_id, limit,
        )

    async def list_by_debt(self, debt_id: int) -> list:
        """
        Retorna todos os investimentos que cobrem uma dívida específica.
        Usa o debt_id direto (1 JOIN) — não precisa passar por investment_opportunities.
        """
        return await self._db.fetch_all(
            """
            SELECT i.*, u.full_name, u.nickname,
                   o.amount_needed, o.amount_committed, o.status AS opportunity_status
            FROM investments i
            JOIN users u ON u.id = i.investor_user_id
            LEFT JOIN investment_opportunities o ON o.id = i.opportunity_id
            WHERE i.debt_id = $1
            ORDER BY i.invested_at ASC
            """,
            debt_id,
        )

    async def coverage_summary_for_debt(self, debt_id: int) -> dict:
        """
        Resumo de cobertura de captação para uma dívida:
          total_invested  — soma dos investimentos ativos cobrindo essa dívida
          investor_count  — número de investidores distintos
          fully_covered   — True se total_invested >= principal da dívida
        """
        row = await self._db.fetch_one(
            """
            SELECT
                COALESCE(SUM(i.amount_invested), 0)::NUMERIC(15,2) AS total_invested,
                COUNT(DISTINCT i.investor_user_id)::INT             AS investor_count
            FROM investments i
            WHERE i.debt_id = $1 AND i.status = 'active'
            """,
            debt_id,
        )
        debt = await self._db.fetch_one(
            "SELECT principal FROM debts WHERE id = $1", debt_id
        )
        total     = Decimal(str(row["total_invested"])) if row else Decimal("0")
        principal = Decimal(str(debt["principal"])) if debt else Decimal("0")
        return {
            "debt_id":        debt_id,
            "total_invested": total,
            "investor_count": int(row["investor_count"]) if row else 0,
            "principal":      principal,
            "fully_covered":  total >= principal,
            "coverage_pct":   float(total / principal * 100) if principal > 0 else 0.0,
        }

    async def list_by_level(self, level_id: int, status: str | None = None) -> list:
        if status:
            return await self._db.fetch_all(
                """
                SELECT i.*, u.full_name, u.nickname
                FROM investments i
                JOIN users u ON u.id = i.investor_user_id
                WHERE i.level_id = $1 AND i.status = $2
                ORDER BY i.invested_at DESC
                """,
                level_id, status,
            )
        return await self._db.fetch_all(
            """
            SELECT i.*, u.full_name, u.nickname
            FROM investments i
            JOIN users u ON u.id = i.investor_user_id
            WHERE i.level_id = $1
            ORDER BY i.invested_at DESC
            """,
            level_id,
        )

    async def update_status(self, investment_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE investments SET status = $1 WHERE id = $2", status, investment_id
        )

    async def total_active_by_investor(
        self, investor_user_id: int, level_id: int
    ) -> Decimal:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(amount_invested), 0)
            FROM investments
            WHERE investor_user_id = $1
              AND level_id         = $2
              AND status           = 'active'
            """,
            investor_user_id, level_id,
        )
        return Decimal(str(val or 0))

    async def total_active_by_level(self, level_id: int) -> Decimal:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(amount_invested), 0)
            FROM investments
            WHERE level_id = $1 AND status = 'active'
            """,
            level_id,
        )
        return Decimal(str(val or 0))

    async def list_all_user_ids(self) -> list[int]:
        rows = await self._db.fetch_all(
            "SELECT DISTINCT investor_user_id FROM investments ORDER BY investor_user_id"
        )
        return [r["investor_user_id"] for r in rows]

    # ── investment_payouts ────────────────────────────────────────────────────

    async def schedule_payout(
        self,
        investment_id: int,
        amount: Decimal,
        period_start: date,
        period_end: date,
        scheduled_date: date,
        scheduled_at=None,  # datetime | None — precisão de minutos/horas
    ) -> int:
        """
        Agenda o payout de vencimento do investimento.

        scheduled_at (TIMESTAMPTZ) tem prioridade sobre scheduled_date (DATE) para
        investimentos de curto prazo (minutos, horas). O job de distribuição usa
        scheduled_at <= NOW() quando disponível.
        """
        return await self._db.fetch_val(
            """
            INSERT INTO investment_payouts
                (investment_id, amount, period_start, period_end,
                 scheduled_date, scheduled_at, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'scheduled')
            RETURNING id
            """,
            investment_id, amount, period_start, period_end,
            scheduled_date, scheduled_at,
        )

    async def list_pending_payouts(self, up_to_date: date | None = None) -> list:
        """
        Retorna payouts vencidos. Usa scheduled_at <= NOW() quando definido
        (precisão de minutos/horas); caso contrário, scheduled_date <= up_to_date.
        """
        return await self._db.fetch_all(
            """
            SELECT p.*, i.investor_user_id, i.level_id, i.amount_invested
            FROM investment_payouts p
            JOIN investments i ON i.id = p.investment_id
            WHERE p.status = 'scheduled'
              AND (
                    (p.scheduled_at IS NOT NULL AND p.scheduled_at <= NOW())
                 OR (p.scheduled_at IS NULL     AND p.scheduled_date <= CURRENT_DATE)
              )
            ORDER BY COALESCE(p.scheduled_at, p.scheduled_date::timestamptz) ASC
            """
        )

    async def mark_payout_paid(self, payout_id: int) -> None:
        await self._db.execute(
            """
            UPDATE investment_payouts
               SET status = 'paid', paid_at = NOW()
             WHERE id = $1
            """,
            payout_id,
        )

    async def total_paid_to_investor(self, investor_user_id: int) -> Decimal:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(p.amount), 0)
            FROM investment_payouts p
            JOIN investments i ON i.id = p.investment_id
            WHERE i.investor_user_id = $1 AND p.status = 'paid'
            """,
            investor_user_id,
        )
        return Decimal(str(val or 0))

    # ── visão consolidada do investidor ───────────────────────────────────────

    async def get_investor_position(self, investor_user_id: int, level_id: int) -> dict:
        """Posição consolidada de um investidor em um nível."""
        total_invested = await self.total_active_by_investor(investor_user_id, level_id)
        total_returned = await self.total_paid_to_investor(investor_user_id)

        pending_payouts = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(p.amount), 0)
            FROM investment_payouts p
            JOIN investments i ON i.id = p.investment_id
            WHERE i.investor_user_id = $1
              AND i.level_id         = $2
              AND p.status           = 'scheduled'
            """,
            investor_user_id, level_id,
        ) or 0

        active_count = await self._db.fetch_val(
            """
            SELECT COUNT(*) FROM investments
            WHERE investor_user_id = $1 AND level_id = $2 AND status = 'active'
            """,
            investor_user_id, level_id,
        ) or 0

        return {
            "total_invested":     total_invested,
            "total_returned":     Decimal(str(total_returned)),
            "pending_payouts":    Decimal(str(pending_payouts)),
            "active_investments": int(active_count),
            "net_position":       total_invested - Decimal(str(total_returned)),
        }
