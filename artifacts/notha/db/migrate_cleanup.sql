-- ============================================================
-- NOTHA — Legacy Cleanup Migration
-- Removes pool-lending model; keeps P2P architecture only.
-- Run once on the live database (safe: all DROPs use IF EXISTS).
-- ============================================================

-- ── 1. Migrate investment_offers: swap opportunity_id → capture_order_id ─────

ALTER TABLE investment_offers
    ADD COLUMN IF NOT EXISTS capture_order_id BIGINT REFERENCES capture_orders(id) ON DELETE CASCADE;

ALTER TABLE investment_offers
    ADD COLUMN IF NOT EXISTS position_id BIGINT REFERENCES creditor_positions(id) ON DELETE SET NULL;

-- Drop old FK to investment_opportunities (may not exist if table already dropped)
ALTER TABLE investment_offers
    DROP CONSTRAINT IF EXISTS investment_offers_opportunity_id_fkey;

ALTER TABLE investment_offers
    DROP CONSTRAINT IF EXISTS investment_offers_opportunity_id_user_id_key;

DROP INDEX IF EXISTS idx_investment_offers_pending_unique;

-- Recreate unique index on capture_order_id + user_id (pending only)
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_offers_capture_pending_unique
    ON investment_offers(capture_order_id, user_id)
    WHERE status = 'pending' AND capture_order_id IS NOT NULL;

-- Remove legacy columns from investment_offers
ALTER TABLE investment_offers DROP COLUMN IF EXISTS opportunity_id;
ALTER TABLE investment_offers DROP COLUMN IF EXISTS investment_id;

-- ── 2. Drop legacy tables (order matters — FK dependencies first) ─────────────

-- Leaves no orphan FKs: payment_allocations → payments → debt_installments → debts → loan_requests
DROP TABLE IF EXISTS payment_allocations   CASCADE;
DROP TABLE IF EXISTS payments              CASCADE;
DROP TABLE IF EXISTS investments           CASCADE;
DROP TABLE IF EXISTS debt_installments     CASCADE;
DROP TABLE IF EXISTS proposed_installments CASCADE;
DROP TABLE IF EXISTS debts                 CASCADE;
DROP TABLE IF EXISTS investment_opportunities CASCADE;
DROP TABLE IF EXISTS loan_requests         CASCADE;
DROP TABLE IF EXISTS loan_rate_quotes      CASCADE;
DROP TABLE IF EXISTS investment_rate_quotes CASCADE;
DROP TABLE IF EXISTS investment_payouts    CASCADE;

-- Fund tables (confirmed: no funds, only levels)
DROP TABLE IF EXISTS fund_users   CASCADE;
DROP TABLE IF EXISTS fund_policies CASCADE;
DROP TABLE IF EXISTS funds        CASCADE;

-- ── 3. Update overdue_installments view to P2P tables ──────────────────────────

DROP VIEW IF EXISTS overdue_installments;

CREATE VIEW overdue_installments AS
SELECT
    cii.id,
    cii.credit_instrument_id,
    cii.sequence,
    cii.due_date,
    cii.principal_amount,
    cii.interest_amount,
    cii.total_amount,
    cii.remaining_amount,
    cii.status,
    ci.debtor_id   AS user_id,
    ci.capture_order_id
FROM credit_instrument_installments cii
JOIN credit_instruments ci ON ci.id = cii.credit_instrument_id
WHERE cii.status IN ('pending', 'partially_paid')
  AND cii.due_date < CURRENT_DATE;

-- ── 4. Rebuild investment_offers table definition inside _migrate_ if needed ───
-- (handled at application startup by the updated _migrate_investor_profile_tables)

-- ── Done ──────────────────────────────────────────────────────────────────────
-- Tables retained (P2P core):
--   capture_requests, capture_orders, creditor_positions, credit_instruments,
--   credit_instrument_installments, assignment_transactions, installment_passthroughs,
--   wallets, wallet_transactions, investor_profiles, investment_offers
-- Tables retained (levels/scoring):
--   levels, user_level_history, level_scoring_rules, level_policies, segments,
--   segment_parameters, user_segments, user_behavior_metrics, user_risk_scores,
--   user_credit_profile, user_loan_stats, user_payment_stats, user_investment_stats,
--   credit_limits, restrictions, rates, risk_score_models, risk_score_weights,
--   liquidity_snapshots, location_risk_events, location_market_metrics
