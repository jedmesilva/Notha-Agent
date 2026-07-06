"""
RateRepository — group_rate_policies, term_rate_curve, liquidity_snapshots,
                 loan_rate_quotes, investment_rate_quotes.

Camadas 1, 2 e 3 da estrutura de taxas (seção 7).
"""
import asyncpg
from decimal import Decimal
from datetime import datetime
from db.connection import DB


class RateRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── group_rate_policies (Camada 1 — política) ─────────────────────────────

    async def get_active_policy(self, group_id: int) -> asyncpg.Record | None:
        """Política mais recente do grupo."""
        return await self._db.fetch_one(
            """
            SELECT * FROM group_rate_policies
            WHERE group_id = $1
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            group_id,
        )

    async def create_policy(
        self,
        group_id: int,
        base_borrowing_rate: Decimal,
        base_investment_rate: Decimal,
        min_spread: Decimal,
        spread_violation_strategy: str = "reject_investment",
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO group_rate_policies
                (group_id, base_borrowing_rate, base_investment_rate,
                 min_spread, spread_violation_strategy)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            group_id, base_borrowing_rate, base_investment_rate,
            min_spread, spread_violation_strategy,
        )

    # ── term_rate_curve (Camada 1 — ajuste por prazo) ────────────────────────

    async def get_term_adjustment(
        self, group_id: int, term_days: int
    ) -> int:
        """Retorna o ajuste em basis points para o prazo dado (0 se não encontrar faixa)."""
        row = await self._db.fetch_one(
            """
            SELECT adjustment_bps FROM term_rate_curve
            WHERE group_id     = $1
              AND min_term_days <= $2
              AND max_term_days >= $2
            LIMIT 1
            """,
            group_id, term_days,
        )
        return int(row["adjustment_bps"]) if row else 0

    async def upsert_term_curve(
        self,
        group_id: int,
        min_term_days: int,
        max_term_days: int,
        adjustment_bps: int,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO term_rate_curve (group_id, min_term_days, max_term_days, adjustment_bps)
            VALUES ($1, $2, $3, $4)
            """,
            group_id, min_term_days, max_term_days, adjustment_bps,
        )

    # ── liquidity_snapshots (Camada 2 — liquidez em tempo real) ──────────────

    async def get_latest_liquidity(self, group_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM liquidity_snapshots
            WHERE group_id = $1
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            group_id,
        )

    # ── loan_rate_quotes (Camada 3 — cotação) ────────────────────────────────

    async def create_loan_quote(
        self,
        loan_request_id: int,
        base_rate: Decimal,
        risk_premium: Decimal,
        term_adjustment: Decimal,
        liquidity_multiplier: Decimal,
        final_rate: Decimal,
        breakdown_json: dict,
        expires_hours: int = 24,
    ) -> int:
        import json
        return await self._db.fetch_val(
            """
            INSERT INTO loan_rate_quotes
                (loan_request_id, base_rate, risk_premium, term_adjustment,
                 liquidity_multiplier, final_rate, breakdown_json, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() + ($8 || ' hours')::interval)
            RETURNING id
            """,
            loan_request_id, base_rate, risk_premium, term_adjustment,
            liquidity_multiplier, final_rate, json.dumps(breakdown_json), str(expires_hours),
        )

    async def get_latest_loan_quote(
        self, loan_request_id: int
    ) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM loan_rate_quotes
            WHERE loan_request_id = $1
              AND expires_at > NOW()
            ORDER BY quoted_at DESC
            LIMIT 1
            """,
            loan_request_id,
        )

    # ── investment_rate_quotes ────────────────────────────────────────────────

    async def create_investment_quote(
        self,
        investment_id: int | None,
        base_rate: Decimal,
        liquidity_multiplier: Decimal,
        final_rate: Decimal,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO investment_rate_quotes
                (investment_id, base_rate, liquidity_multiplier, final_rate)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            investment_id, base_rate, liquidity_multiplier, final_rate,
        )
