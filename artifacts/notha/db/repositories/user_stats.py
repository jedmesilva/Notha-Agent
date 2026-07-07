"""
UserStatsRepository — user_loan_stats, user_payment_stats,
                      user_investment_stats, user_credit_profile.

These tables hold pre-computed behavioral metrics used by the credit algorithm.
They are populated by periodic jobs, NOT computed on every request.

Architecture:
  Raw events (debts, payments, investments)
      ↓  [job: recalculate_user_stats]
  user_loan_stats / user_payment_stats / user_investment_stats
      ↓  [job: recalculate_credit_profile]
  user_credit_profile  (credit_limit, personal_risk_rate, default_rate)
      ↓  [read by engine]
  Loan approval, rate pricing, level progression
"""
import logging
from decimal import Decimal
from db.connection import DB

logger = logging.getLogger("notha.user_stats")

_ZERO = Decimal("0")


class UserStatsRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Loan stats ─────────────────────────────────────────────────────────────

    async def upsert_loan_stats(
        self,
        user_id: int,
        *,
        requests_last_30d: int = 0,
        requests_last_90d: int = 0,
        requests_last_365d: int = 0,
        grants_last_30d: int = 0,
        grants_last_90d: int = 0,
        grants_last_365d: int = 0,
        grant_rate: Decimal = _ZERO,
        total_requests_count: int = 0,
        total_grants_count: int = 0,
        total_requested_amount: Decimal = _ZERO,
        total_granted_amount: Decimal = _ZERO,
        avg_requested_amount: Decimal = _ZERO,
        avg_granted_amount: Decimal = _ZERO,
        avg_utilization_rate: Decimal = _ZERO,
        max_utilization_ever: Decimal = _ZERO,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO user_loan_stats (
                user_id,
                requests_last_30d, requests_last_90d, requests_last_365d,
                grants_last_30d,   grants_last_90d,   grants_last_365d,
                grant_rate,
                total_requests_count, total_grants_count,
                total_requested_amount, total_granted_amount,
                avg_requested_amount, avg_granted_amount,
                avg_utilization_rate, max_utilization_ever,
                calculated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, $16,
                NOW()
            )
            ON CONFLICT (user_id) DO UPDATE SET
                requests_last_30d      = EXCLUDED.requests_last_30d,
                requests_last_90d      = EXCLUDED.requests_last_90d,
                requests_last_365d     = EXCLUDED.requests_last_365d,
                grants_last_30d        = EXCLUDED.grants_last_30d,
                grants_last_90d        = EXCLUDED.grants_last_90d,
                grants_last_365d       = EXCLUDED.grants_last_365d,
                grant_rate             = EXCLUDED.grant_rate,
                total_requests_count   = EXCLUDED.total_requests_count,
                total_grants_count     = EXCLUDED.total_grants_count,
                total_requested_amount = EXCLUDED.total_requested_amount,
                total_granted_amount   = EXCLUDED.total_granted_amount,
                avg_requested_amount   = EXCLUDED.avg_requested_amount,
                avg_granted_amount     = EXCLUDED.avg_granted_amount,
                avg_utilization_rate   = EXCLUDED.avg_utilization_rate,
                max_utilization_ever   = EXCLUDED.max_utilization_ever,
                calculated_at          = NOW()
            """,
            user_id,
            requests_last_30d, requests_last_90d, requests_last_365d,
            grants_last_30d,   grants_last_90d,   grants_last_365d,
            grant_rate,
            total_requests_count, total_grants_count,
            total_requested_amount, total_granted_amount,
            avg_requested_amount, avg_granted_amount,
            avg_utilization_rate, max_utilization_ever,
        )

    async def get_loan_stats(self, user_id: int) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM user_loan_stats WHERE user_id = $1", user_id
        )
        return dict(row) if row else None

    async def compute_and_upsert_loan_stats(self, user_id: int) -> None:
        """
        Compute loan stats directly from raw tables and upsert.
        Called by the periodic recalculation job.
        """
        row = await self._db.fetch_one(
            """
            WITH
            requests AS (
                SELECT
                    COUNT(*)                                         AS total_requests,
                    COUNT(*) FILTER (WHERE requested_at >= NOW() - INTERVAL '30 days')  AS req_30d,
                    COUNT(*) FILTER (WHERE requested_at >= NOW() - INTERVAL '90 days')  AS req_90d,
                    COUNT(*) FILTER (WHERE requested_at >= NOW() - INTERVAL '365 days') AS req_365d,
                    COALESCE(SUM(requested_amount), 0)               AS total_req_amount,
                    COALESCE(AVG(requested_amount), 0)               AS avg_req_amount
                FROM loan_requests
                WHERE user_id = $1
            ),
            grants AS (
                SELECT
                    COUNT(*)                                          AS total_grants,
                    COUNT(*) FILTER (WHERE d.created_at >= NOW() - INTERVAL '30 days')  AS gr_30d,
                    COUNT(*) FILTER (WHERE d.created_at >= NOW() - INTERVAL '90 days')  AS gr_90d,
                    COUNT(*) FILTER (WHERE d.created_at >= NOW() - INTERVAL '365 days') AS gr_365d,
                    COALESCE(SUM(d.principal), 0)                    AS total_gr_amount,
                    COALESCE(AVG(d.principal), 0)                    AS avg_gr_amount
                FROM debts d
                JOIN loan_requests lr ON lr.id = d.loan_request_id
                WHERE lr.user_id = $1
            ),
            credit AS (
                SELECT COALESCE(credit_limit, 0) AS lim
                FROM user_credit_profile
                WHERE user_id = $1
            )
            SELECT
                r.total_requests, r.req_30d, r.req_90d, r.req_365d,
                r.total_req_amount, r.avg_req_amount,
                g.total_grants, g.gr_30d, g.gr_90d, g.gr_365d,
                g.total_gr_amount, g.avg_gr_amount,
                CASE WHEN r.total_requests > 0
                     THEN g.total_grants::NUMERIC / r.total_requests
                     ELSE 0 END AS grant_rate,
                CASE WHEN c.lim > 0
                     THEN g.avg_gr_amount / c.lim
                     ELSE 0 END AS avg_util,
                CASE WHEN c.lim > 0
                     THEN g.total_gr_amount / c.lim
                     ELSE 0 END AS max_util
            FROM requests r, grants g, credit c
            """,
            user_id,
        )
        if not row:
            return
        await self.upsert_loan_stats(
            user_id,
            requests_last_30d=row["req_30d"] or 0,
            requests_last_90d=row["req_90d"] or 0,
            requests_last_365d=row["req_365d"] or 0,
            grants_last_30d=row["gr_30d"] or 0,
            grants_last_90d=row["gr_90d"] or 0,
            grants_last_365d=row["gr_365d"] or 0,
            grant_rate=Decimal(str(row["grant_rate"] or 0)),
            total_requests_count=row["total_requests"] or 0,
            total_grants_count=row["total_grants"] or 0,
            total_requested_amount=Decimal(str(row["total_req_amount"] or 0)),
            total_granted_amount=Decimal(str(row["total_gr_amount"] or 0)),
            avg_requested_amount=Decimal(str(row["avg_req_amount"] or 0)),
            avg_granted_amount=Decimal(str(row["avg_gr_amount"] or 0)),
            avg_utilization_rate=Decimal(str(row["avg_util"] or 0)),
            max_utilization_ever=Decimal(str(row["max_util"] or 0)),
        )

    # ── Payment stats ──────────────────────────────────────────────────────────

    async def upsert_payment_stats(
        self,
        user_id: int,
        *,
        on_time_rate: Decimal = _ZERO,
        early_rate: Decimal = _ZERO,
        late_rate: Decimal = _ZERO,
        avg_days_early: Decimal = _ZERO,
        avg_days_late: Decimal = _ZERO,
        payment_variance_days: Decimal = _ZERO,
        consecutive_on_time: int = 0,
        max_consecutive_on_time: int = 0,
        active_defaults_count: int = 0,
        total_defaults_count: int = 0,
        total_installments_paid: int = 0,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO user_payment_stats (
                user_id,
                on_time_rate, early_rate, late_rate,
                avg_days_early, avg_days_late, payment_variance_days,
                consecutive_on_time, max_consecutive_on_time,
                active_defaults_count, total_defaults_count,
                total_installments_paid,
                calculated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW()
            )
            ON CONFLICT (user_id) DO UPDATE SET
                on_time_rate            = EXCLUDED.on_time_rate,
                early_rate              = EXCLUDED.early_rate,
                late_rate               = EXCLUDED.late_rate,
                avg_days_early          = EXCLUDED.avg_days_early,
                avg_days_late           = EXCLUDED.avg_days_late,
                payment_variance_days   = EXCLUDED.payment_variance_days,
                consecutive_on_time     = EXCLUDED.consecutive_on_time,
                max_consecutive_on_time = EXCLUDED.max_consecutive_on_time,
                active_defaults_count   = EXCLUDED.active_defaults_count,
                total_defaults_count    = EXCLUDED.total_defaults_count,
                total_installments_paid = EXCLUDED.total_installments_paid,
                calculated_at           = NOW()
            """,
            user_id,
            on_time_rate, early_rate, late_rate,
            avg_days_early, avg_days_late, payment_variance_days,
            consecutive_on_time, max_consecutive_on_time,
            active_defaults_count, total_defaults_count,
            total_installments_paid,
        )

    async def get_payment_stats(self, user_id: int) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM user_payment_stats WHERE user_id = $1", user_id
        )
        return dict(row) if row else None

    async def compute_and_upsert_payment_stats(self, user_id: int) -> None:
        """
        Compute payment stats directly from raw installment data and upsert.
        """
        row = await self._db.fetch_one(
            """
            WITH installments AS (
                SELECT
                    di.status,
                    di.due_date,
                    p.paid_at::DATE AS paid_date,
                    (p.paid_at::DATE - di.due_date) AS days_diff
                FROM debt_installments di
                LEFT JOIN payments p ON p.debt_installment_id = di.id
                JOIN debts d  ON d.id  = di.debt_id
                JOIN loan_requests lr ON lr.id = d.loan_request_id
                WHERE lr.user_id = $1
                  AND di.status IN ('paid','overdue','partially_paid','pending')
            ),
            paid AS (
                SELECT * FROM installments WHERE status = 'paid'
            ),
            totals AS (
                SELECT
                    COUNT(*) FILTER (WHERE days_diff <= 0)  AS on_time_count,
                    COUNT(*) FILTER (WHERE days_diff < 0)   AS early_count,
                    COUNT(*) FILTER (WHERE days_diff > 0)   AS late_count,
                    COUNT(*)                                 AS total_paid,
                    COALESCE(AVG(days_diff) FILTER (WHERE days_diff < 0), 0) AS avg_early,
                    COALESCE(AVG(days_diff) FILTER (WHERE days_diff > 0), 0) AS avg_late,
                    COALESCE(STDDEV(days_diff), 0)           AS variance_days
                FROM paid
            ),
            defaults AS (
                SELECT
                    COUNT(*) FILTER (WHERE status = 'overdue') AS active_def,
                    COUNT(*) FILTER (WHERE status IN ('overdue','paid') AND days_diff > 30) AS total_def
                FROM installments
            )
            SELECT
                t.on_time_count, t.early_count, t.late_count, t.total_paid,
                t.avg_early, t.avg_late, t.variance_days,
                d.active_def, d.total_def,
                CASE WHEN t.total_paid > 0 THEN t.on_time_count::NUMERIC / t.total_paid ELSE 0 END AS on_time_rate,
                CASE WHEN t.total_paid > 0 THEN t.early_count::NUMERIC  / t.total_paid ELSE 0 END AS early_rate,
                CASE WHEN t.total_paid > 0 THEN t.late_count::NUMERIC   / t.total_paid ELSE 0 END AS late_rate
            FROM totals t, defaults d
            """,
            user_id,
        )
        if not row:
            return

        # Compute consecutive_on_time streak from ordered installments
        streak_row = await self._db.fetch_one(
            """
            WITH ordered AS (
                SELECT
                    (p.paid_at::DATE - di.due_date) <= 0 AS on_time,
                    di.due_date
                FROM debt_installments di
                LEFT JOIN payments p ON p.debt_installment_id = di.id
                JOIN debts d ON d.id = di.debt_id
                JOIN loan_requests lr ON lr.id = d.loan_request_id
                WHERE lr.user_id = $1 AND di.status = 'paid'
                ORDER BY di.due_date DESC
            ),
            streaks AS (
                SELECT on_time,
                       ROW_NUMBER() OVER () AS rn
                FROM ordered
            )
            SELECT COUNT(*) AS streak
            FROM streaks
            WHERE rn <= (
                SELECT COALESCE(MIN(rn) - 1, COUNT(*))
                FROM streaks
                WHERE NOT on_time
            )
            AND on_time
            """,
            user_id,
        )
        streak = int(streak_row["streak"]) if streak_row else 0

        max_streak_row = await self._db.fetch_one(
            "SELECT COALESCE(max_consecutive_on_time, 0) AS m FROM user_payment_stats WHERE user_id = $1",
            user_id,
        )
        prev_max = int(max_streak_row["m"]) if max_streak_row else 0

        await self.upsert_payment_stats(
            user_id,
            on_time_rate=Decimal(str(row["on_time_rate"] or 0)),
            early_rate=Decimal(str(row["early_rate"] or 0)),
            late_rate=Decimal(str(row["late_rate"] or 0)),
            avg_days_early=Decimal(str(abs(row["avg_early"] or 0))),
            avg_days_late=Decimal(str(row["avg_late"] or 0)),
            payment_variance_days=Decimal(str(row["variance_days"] or 0)),
            consecutive_on_time=streak,
            max_consecutive_on_time=max(streak, prev_max),
            active_defaults_count=int(row["active_def"] or 0),
            total_defaults_count=int(row["total_def"] or 0),
            total_installments_paid=int(row["total_paid"] or 0),
        )

    # ── Investment stats ───────────────────────────────────────────────────────

    async def upsert_investment_stats(
        self,
        user_id: int,
        *,
        has_ever_invested: bool = False,
        offers_received_count: int = 0,
        offers_accepted_count: int = 0,
        acceptance_rate: Decimal = _ZERO,
        total_invested_amount: Decimal = _ZERO,
        active_invested_amount: Decimal = _ZERO,
        avg_investment_amount: Decimal = _ZERO,
        investments_active_count: int = 0,
        investments_matured_count: int = 0,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO user_investment_stats (
                user_id,
                has_ever_invested,
                offers_received_count, offers_accepted_count, acceptance_rate,
                total_invested_amount, active_invested_amount, avg_investment_amount,
                investments_active_count, investments_matured_count,
                calculated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW()
            )
            ON CONFLICT (user_id) DO UPDATE SET
                has_ever_invested        = EXCLUDED.has_ever_invested,
                offers_received_count    = EXCLUDED.offers_received_count,
                offers_accepted_count    = EXCLUDED.offers_accepted_count,
                acceptance_rate          = EXCLUDED.acceptance_rate,
                total_invested_amount    = EXCLUDED.total_invested_amount,
                active_invested_amount   = EXCLUDED.active_invested_amount,
                avg_investment_amount    = EXCLUDED.avg_investment_amount,
                investments_active_count = EXCLUDED.investments_active_count,
                investments_matured_count= EXCLUDED.investments_matured_count,
                calculated_at            = NOW()
            """,
            user_id,
            has_ever_invested,
            offers_received_count, offers_accepted_count, acceptance_rate,
            total_invested_amount, active_invested_amount, avg_investment_amount,
            investments_active_count, investments_matured_count,
        )

    async def get_investment_stats(self, user_id: int) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM user_investment_stats WHERE user_id = $1", user_id
        )
        return dict(row) if row else None

    async def compute_and_upsert_investment_stats(self, user_id: int) -> None:
        row = await self._db.fetch_one(
            """
            WITH inv AS (
                SELECT
                    COUNT(*)                                          AS total_count,
                    COUNT(*) FILTER (WHERE status = 'active')        AS active_count,
                    COUNT(*) FILTER (WHERE status = 'matured')       AS matured_count,
                    COALESCE(SUM(amount_invested), 0)                AS total_amount,
                    COALESCE(SUM(amount_invested) FILTER (WHERE status = 'active'), 0) AS active_amount,
                    COALESCE(AVG(amount_invested), 0)                AS avg_amount
                FROM investments
                WHERE investor_user_id = $1
            ),
            offers AS (
                SELECT
                    COUNT(*)                                          AS received,
                    COUNT(*) FILTER (WHERE status IN ('accepted','invested')) AS accepted
                FROM investment_offers
                WHERE user_id = $1
            )
            SELECT
                inv.total_count, inv.active_count, inv.matured_count,
                inv.total_amount, inv.active_amount, inv.avg_amount,
                offers.received, offers.accepted,
                CASE WHEN offers.received > 0
                     THEN offers.accepted::NUMERIC / offers.received
                     ELSE 0 END AS acceptance_rate
            FROM inv, offers
            """,
            user_id,
        )
        if not row:
            return
        await self.upsert_investment_stats(
            user_id,
            has_ever_invested=(row["total_count"] or 0) > 0,
            offers_received_count=int(row["received"] or 0),
            offers_accepted_count=int(row["accepted"] or 0),
            acceptance_rate=Decimal(str(row["acceptance_rate"] or 0)),
            total_invested_amount=Decimal(str(row["total_amount"] or 0)),
            active_invested_amount=Decimal(str(row["active_amount"] or 0)),
            avg_investment_amount=Decimal(str(row["avg_amount"] or 0)),
            investments_active_count=int(row["active_count"] or 0),
            investments_matured_count=int(row["matured_count"] or 0),
        )

    # ── Credit profile ─────────────────────────────────────────────────────────

    async def upsert_credit_profile(
        self,
        user_id: int,
        *,
        credit_limit: Decimal,
        personal_risk_rate: Decimal,
        default_rate: Decimal,
        valid_days: int = 7,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO user_credit_profile (
                user_id, credit_limit, personal_risk_rate, default_rate,
                calculated_at, valid_until
            ) VALUES (
                $1, $2, $3, $4, NOW(), NOW() + ($5 || ' days')::INTERVAL
            )
            ON CONFLICT (user_id) DO UPDATE SET
                credit_limit        = EXCLUDED.credit_limit,
                personal_risk_rate  = EXCLUDED.personal_risk_rate,
                default_rate        = EXCLUDED.default_rate,
                calculated_at       = NOW(),
                valid_until         = NOW() + ($5 || ' days')::INTERVAL
            """,
            user_id, credit_limit, personal_risk_rate, default_rate, valid_days,
        )

    async def get_credit_profile(self, user_id: int) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM user_credit_profile WHERE user_id = $1", user_id
        )
        return dict(row) if row else None

    async def compute_and_upsert_credit_profile(
        self,
        user_id: int,
        level_id: int,
    ) -> dict | None:
        """
        Compute credit_limit, personal_risk_rate, default_rate from stats
        and level_scoring_rules, then persist to user_credit_profile.

        Returns the computed profile dict, or None if no scoring rules exist.
        """
        rules = await self._db.fetch_one(
            "SELECT * FROM level_scoring_rules WHERE level_id = $1", level_id
        )
        if not rules:
            logger.warning("No scoring rules for level %s — skipping credit profile", level_id)
            return None

        loan    = await self.get_loan_stats(user_id)
        payment = await self.get_payment_stats(user_id)

        on_time_rate  = Decimal(str(payment["on_time_rate"]  if payment else 0))
        default_rate  = Decimal(str(payment["default_rate_computed"] if payment else 0))
        avg_util      = Decimal(str(loan["avg_utilization_rate"] if loan else 0))

        base    = Decimal(str(rules["credit_limit_base"]))
        maximum = Decimal(str(rules["credit_limit_max"]))
        formula = rules["credit_limit_formula"]

        if formula == "linear":
            credit_limit = base + on_time_rate * (maximum - base)
        elif formula == "utilization_based":
            credit_limit = base + (1 - avg_util) * (maximum - base)
        elif formula == "payment_weighted":
            credit_limit = base + (on_time_rate * (1 - default_rate)) * (maximum - base)
        else:
            credit_limit = base

        credit_limit = max(base, min(maximum, credit_limit))

        risk_multiplier = Decimal(str(rules["risk_rate_multiplier"]))
        max_premium     = Decimal(str(rules["max_risk_premium"]))
        personal_risk   = min(default_rate * risk_multiplier, max_premium)

        profile = {
            "credit_limit":       credit_limit,
            "personal_risk_rate": personal_risk,
            "default_rate":       default_rate,
        }
        await self.upsert_credit_profile(user_id, **profile)
        return profile

    # ── Bulk recalculation (called by job) ────────────────────────────────────

    async def recalculate_all_stats(self, user_id: int, level_id: int) -> None:
        """
        Full recalculation pipeline for one user.
        Order matters: credit profile depends on loan stats being fresh.
        """
        await self.compute_and_upsert_loan_stats(user_id)
        await self.compute_and_upsert_payment_stats(user_id)
        await self.compute_and_upsert_investment_stats(user_id)
        await self.compute_and_upsert_credit_profile(user_id, level_id)
        logger.info("Stats recalculated for user %s (level %s)", user_id, level_id)

    # ── Read helpers ───────────────────────────────────────────────────────────

    async def get_full_profile(self, user_id: int) -> dict:
        """Return all stats for a user in one dict."""
        loan       = await self.get_loan_stats(user_id)
        payment    = await self.get_payment_stats(user_id)
        investment = await self.get_investment_stats(user_id)
        credit     = await self.get_credit_profile(user_id)
        return {
            "loan":       loan,
            "payment":    payment,
            "investment": investment,
            "credit":     credit,
        }
