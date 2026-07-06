"""
ScoringRepository — user_behavior_metrics, user_risk_scores, user_locations,
                    location_risk_events, location_market_metrics,
                    risk_score_models, risk_score_weights.

Acesso de leitura/escrita para toda a camada de scoring multi-fator (seção 8).
"""
import asyncpg
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from db.connection import DB


class ScoringRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── user_behavior_metrics ─────────────────────────────────────────────────

    async def get_behavior_metrics(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM user_behavior_metrics
            WHERE user_id = $1
            ORDER BY calculated_at DESC
            LIMIT 1
            """,
            user_id,
        )

    async def upsert_behavior_metrics(self, user_id: int, metrics: dict) -> None:
        """
        Insere ou atualiza métricas comportamentais do usuário.
        Usa DELETE + INSERT porque user_behavior_metrics não tem UNIQUE em user_id
        (é append-only por design — aqui mantemos apenas a linha mais recente por usuário).
        """
        await self._db.execute(
            "DELETE FROM user_behavior_metrics WHERE user_id = $1", user_id
        )
        await self._db.execute(
            """
            INSERT INTO user_behavior_metrics
                (user_id, total_borrowed_amount, total_repaid_amount,
                 loan_request_frequency_90d, payment_frequency_score,
                 late_payments_count, defaults_count,
                 total_invested_amount, investment_frequency_90d,
                 avg_monthly_income_pluggy, avg_monthly_expenses_pluggy,
                 bank_account_age_days, calculated_at, valid_until)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, NOW(), NOW() + INTERVAL '25 hours')
            """,
            user_id,
            metrics.get("total_borrowed_amount", Decimal("0")),
            metrics.get("total_repaid_amount", Decimal("0")),
            metrics.get("loan_request_frequency_90d", 0),
            metrics.get("payment_frequency_score", Decimal("0")),
            metrics.get("late_payments_count", 0),
            metrics.get("defaults_count", 0),
            metrics.get("total_invested_amount", Decimal("0")),
            metrics.get("investment_frequency_90d", 0),
            metrics.get("avg_monthly_income_pluggy"),
            metrics.get("avg_monthly_expenses_pluggy"),
            metrics.get("bank_account_age_days"),
        )

    # ── user_risk_scores ──────────────────────────────────────────────────────

    async def get_latest_score(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM user_risk_scores
            WHERE user_id = $1
            ORDER BY calculated_at DESC
            LIMIT 1
            """,
            user_id,
        )

    async def insert_score(
        self,
        user_id: int,
        model_id: int,
        score: Decimal,
        factors_json: dict,
        valid_hours: int = 25,
    ) -> int:
        import json
        return await self._db.fetch_val(
            """
            INSERT INTO user_risk_scores
                (user_id, model_id, score, factors_json, valid_until)
            VALUES ($1, $2, $3, $4, NOW() + ($5 || ' hours')::interval)
            RETURNING id
            """,
            user_id, model_id, score, json.dumps(factors_json), str(valid_hours),
        )

    async def is_score_valid(self, user_id: int) -> bool:
        row = await self._db.fetch_one(
            """
            SELECT valid_until FROM user_risk_scores
            WHERE user_id = $1
            ORDER BY calculated_at DESC LIMIT 1
            """,
            user_id,
        )
        if not row or not row["valid_until"]:
            return False
        now = datetime.now(timezone.utc)
        valid_until = row["valid_until"]
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        return valid_until > now

    # ── risk_score_models / weights ───────────────────────────────────────────

    async def get_active_model(self) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM risk_score_models WHERE active = TRUE ORDER BY id DESC LIMIT 1"
        )

    async def get_weights(self, model_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM risk_score_weights WHERE model_id = $1",
            model_id,
        )

    # ── user_locations ────────────────────────────────────────────────────────

    async def get_user_location(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM user_locations WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 1",
            user_id,
        )

    async def upsert_location(
        self, user_id: int, city: str | None, state: str | None,
        country: str = "BR", geohash: str | None = None,
    ) -> None:
        existing = await self._db.fetch_one(
            "SELECT id FROM user_locations WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 1",
            user_id,
        )
        if existing:
            await self._db.execute(
                """
                UPDATE user_locations
                   SET city = $1, state = $2, country = $3, geohash = $4, updated_at = NOW()
                 WHERE id = $5
                """,
                city, state, country, geohash, existing["id"],
            )
        else:
            await self._db.execute(
                "INSERT INTO user_locations (user_id, city, state, country, geohash) VALUES ($1,$2,$3,$4,$5)",
                user_id, city, state, country, geohash,
            )

    # ── location_risk_events ──────────────────────────────────────────────────

    async def get_recent_risk_events(
        self, geohash: str, days: int = 30
    ) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM location_risk_events
            WHERE geohash = $1
              AND occurred_at >= NOW() - ($2 || ' days')::interval
            ORDER BY severity DESC, occurred_at DESC
            """,
            geohash, str(days),
        )

    async def insert_risk_event(
        self,
        geohash: str,
        event_type: str,
        severity: int,
        description: str,
        source: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO location_risk_events (geohash, event_type, severity, description, source, occurred_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            """,
            geohash, event_type, severity, description, source,
        )

    # ── location_market_metrics ───────────────────────────────────────────────

    async def get_market_metrics(self, geohash: str) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM location_market_metrics
            WHERE geohash = $1
            ORDER BY calculated_at DESC
            LIMIT 1
            """,
            geohash,
        )

    async def upsert_market_metrics(self, geohash: str, metrics: dict) -> None:
        await self._db.execute(
            """
            INSERT INTO location_market_metrics
                (geohash, active_loan_demand_local, avg_requested_amount_local,
                 active_investors_count, available_investment_local)
            VALUES ($1, $2, $3, $4, $5)
            """,
            geohash,
            metrics.get("active_loan_demand_local", Decimal("0")),
            metrics.get("avg_requested_amount_local", Decimal("0")),
            metrics.get("active_investors_count", 0),
            metrics.get("available_investment_local", Decimal("0")),
        )
