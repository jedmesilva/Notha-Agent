"""
RateRepository — level_policies, level_term_curve, liquidity_snapshots,
                 loan_rate_quotes, investment_rate_quotes.

Camadas 1, 2 e 3 da estrutura de taxas.
As políticas financeiras são controladas por nível (levels),
não mais por grupos.
"""
import asyncpg
from decimal import Decimal
from db.connection import DB


class RateRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── level_policies (Camada 1 — política) ─────────────────────────────────

    async def get_active_policy(self, level_id: int) -> asyncpg.Record | None:
        """Política mais recente do nível."""
        return await self._db.fetch_one(
            """
            SELECT * FROM level_policies
            WHERE level_id = $1
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            level_id,
        )

    # ── level_term_curve (Camada 1 — ajuste por prazo) ───────────────────────

    async def get_term_adjustment(
        self,
        level_id: int,
        term_days: int,
        policy=None,
    ) -> int:
        """
        Retorna o ajuste em basis points para o prazo dado.

        Estratégias (campo term_rate_formula em level_policies):
          'bands'  — lookup em level_term_curve (padrão).
                     Lança ValueError se o prazo não estiver coberto.
          'linear' — base_bps + scale × term_days
          'log'    — base_bps + scale × ln(term_days)   (prazo > 0)
          'sqrt'   — base_bps + scale × √(term_days)

        O uso de fórmulas elimina a necessidade de definir faixas fixas: qualquer
        prazo, incluindo minutos e horas expressos em frações de dia, é calculável.
        """
        import math

        _policy = policy or await self.get_active_policy(level_id)
        formula = (
            _policy["term_rate_formula"]
            if _policy and _policy["term_rate_formula"]
            else "bands"
        )

        if formula == "bands":
            row = await self._db.fetch_one(
                """
                SELECT adjustment_bps FROM level_term_curve
                WHERE level_id     = $1
                  AND min_term_days <= $2
                  AND max_term_days >= $2
                LIMIT 1
                """,
                level_id, term_days,
            )
            if row is None:
                raise ValueError(
                    f"Prazo de {term_days} dias não coberto pela level_term_curve "
                    f"do nível {level_id}. Configure uma faixa ou altere "
                    f"term_rate_formula para 'linear', 'log' ou 'sqrt'."
                )
            return int(row["adjustment_bps"])

        # Parâmetros da fórmula
        base_bps = float(
            _policy["term_rate_base_bps"]
            if _policy and _policy["term_rate_base_bps"] is not None
            else 0
        )
        scale = float(
            _policy["term_rate_scale"]
            if _policy and _policy["term_rate_scale"] is not None
            else 0
        )
        t = max(term_days, 1)  # evita log(0) / sqrt(0)

        if formula == "linear":
            result = base_bps + scale * t
        elif formula == "log":
            result = base_bps + scale * math.log(t)
        elif formula == "sqrt":
            result = base_bps + scale * math.sqrt(t)
        else:
            raise ValueError(
                f"term_rate_formula desconhecida: '{formula}'. "
                "Use 'bands', 'linear', 'log' ou 'sqrt'."
            )

        return round(result)

    # ── liquidity_snapshots (Camada 2 — liquidez em tempo real) ──────────────

    async def get_latest_liquidity(self, level_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM liquidity_snapshots
            WHERE level_id = $1
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            level_id,
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
