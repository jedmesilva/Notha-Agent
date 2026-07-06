"""
InvestmentRepository — investments + investment_payouts.

Investimento = posição de um investidor no fundo (grupo).
Vinculado a uma oportunidade específica (debt_id rastreável) ou geral.

Payouts são agendados mensalmente e pagos via job periódico.
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
        group_id: int,
        amount_invested: Decimal,
        rate_agreed: Decimal,
        opportunity_id: int | None = None,
        maturity_date: date | None = None,
        maturity_at=None,  # datetime | None — precisão de minutos/horas
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO investments
                (investor_user_id, group_id, opportunity_id,
                 amount_invested, rate_agreed, status, maturity_date, maturity_at)
            VALUES ($1, $2, $3, $4, $5, 'active', $6, $7)
            RETURNING id
            """,
            investor_user_id, group_id, opportunity_id,
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
                SELECT i.*, g.name AS group_name
                FROM investments i
                JOIN groups g ON g.id = i.group_id
                WHERE i.investor_user_id = $1 AND i.status = $2
                ORDER BY i.invested_at DESC
                LIMIT $3
                """,
                investor_user_id, status, limit,
            )
        return await self._db.fetch_all(
            """
            SELECT i.*, g.name AS group_name
            FROM investments i
            JOIN groups g ON g.id = i.group_id
            WHERE i.investor_user_id = $1
            ORDER BY i.invested_at DESC
            LIMIT $2
            """,
            investor_user_id, limit,
        )

    async def list_by_group(self, group_id: int, status: str | None = None) -> list:
        if status:
            return await self._db.fetch_all(
                """
                SELECT i.*, u.full_name, u.nickname
                FROM investments i
                JOIN users u ON u.id = i.investor_user_id
                WHERE i.group_id = $1 AND i.status = $2
                ORDER BY i.invested_at DESC
                """,
                group_id, status,
            )
        return await self._db.fetch_all(
            """
            SELECT i.*, u.full_name, u.nickname
            FROM investments i
            JOIN users u ON u.id = i.investor_user_id
            WHERE i.group_id = $1
            ORDER BY i.invested_at DESC
            """,
            group_id,
        )

    async def update_status(self, investment_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE investments SET status = $1 WHERE id = $2", status, investment_id
        )

    async def total_active_by_investor(
        self, investor_user_id: int, group_id: int
    ) -> Decimal:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(amount_invested), 0)
            FROM investments
            WHERE investor_user_id = $1
              AND group_id         = $2
              AND status           = 'active'
            """,
            investor_user_id, group_id,
        )
        return Decimal(str(val or 0))

    async def total_active_by_group(self, group_id: int) -> Decimal:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(amount_invested), 0)
            FROM investments
            WHERE group_id = $1 AND status = 'active'
            """,
            group_id,
        )
        return Decimal(str(val or 0))

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
        investimentos de curto prazo (minutos, horas).  O job de distribuição usa
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
        Retorna payouts vencidos.  Usa scheduled_at <= NOW() quando definido
        (precisão de minutos/horas); caso contrário, scheduled_date <= up_to_date.
        """
        return await self._db.fetch_all(
            """
            SELECT p.*, i.investor_user_id, i.group_id, i.amount_invested
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

    async def get_investor_position(self, investor_user_id: int, group_id: int) -> dict:
        """Posição consolidada de um investidor em um grupo."""
        total_invested = await self.total_active_by_investor(investor_user_id, group_id)
        total_returned = await self.total_paid_to_investor(investor_user_id)

        pending_payouts = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(p.amount), 0)
            FROM investment_payouts p
            JOIN investments i ON i.id = p.investment_id
            WHERE i.investor_user_id = $1
              AND i.group_id         = $2
              AND p.status           = 'scheduled'
            """,
            investor_user_id, group_id,
        ) or 0

        active_count = await self._db.fetch_val(
            """
            SELECT COUNT(*) FROM investments
            WHERE investor_user_id = $1 AND group_id = $2 AND status = 'active'
            """,
            investor_user_id, group_id,
        ) or 0

        return {
            "total_invested":         total_invested,
            "total_returned":         Decimal(str(total_returned)),
            "pending_payouts":        Decimal(str(pending_payouts)),
            "active_investments":     int(active_count),
            "net_position":           total_invested - Decimal(str(total_returned)),
        }
