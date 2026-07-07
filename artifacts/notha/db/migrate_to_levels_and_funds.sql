-- ============================================================
-- Migration: groups → levels + segments + funds
-- Safe to run multiple times (all operations are idempotent).
-- Does NOT drop old group_* tables — kept for safety.
-- ============================================================

-- ── 1. users: add current_level ────────────────────────────
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS current_level SMALLINT NOT NULL DEFAULT 1
        CONSTRAINT chk_user_level CHECK (current_level BETWEEN 1 AND 10);

-- ── 2. levels table ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS levels (
    id          SMALLINT PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    description TEXT
);

INSERT INTO levels (id, name, description) VALUES
(1,  'Nível 1 — Iniciante',          'Limite mínimo de crédito. Usuários sem histórico.'),
(2,  'Nível 2 — Básico',             'Pequeno histórico positivo. Risco elevado.'),
(3,  'Nível 3 — Em desenvolvimento', 'Histórico inicial construído. Risco moderado-alto.'),
(4,  'Nível 4 — Regular',            'Comportamento consistente. Risco moderado.'),
(5,  'Nível 5 — Intermediário',      'Bom histórico de pagamentos. Risco mediano.'),
(6,  'Nível 6 — Confiável',          'Histórico sólido. Risco baixo-moderado.'),
(7,  'Nível 7 — Avançado',           'Excelente histórico. Risco baixo.'),
(8,  'Nível 8 — Premium',            'Histórico excepcional. Risco muito baixo.'),
(9,  'Nível 9 — Elite',              'Top performers. Risco mínimo.'),
(10, 'Nível 10 — Máximo',            'Perfil máximo de confiança e capacidade.')
ON CONFLICT (id) DO UPDATE SET
    name        = EXCLUDED.name,
    description = EXCLUDED.description;

-- ── 3. user_level_history ──────────────────────────────────
CREATE TABLE IF NOT EXISTS user_level_history (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_level SMALLINT REFERENCES levels(id),
    to_level   SMALLINT NOT NULL REFERENCES levels(id),
    reason     TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_level_history_user
    ON user_level_history(user_id);
CREATE INDEX IF NOT EXISTS idx_user_level_history_changed
    ON user_level_history(user_id, changed_at DESC);

-- ── 4. level_policies ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS level_policies (
    id                        SERIAL PRIMARY KEY,
    level_id                  SMALLINT NOT NULL REFERENCES levels(id),
    min_invested_total        NUMERIC(15,2) NOT NULL DEFAULT 0,
    max_per_user_limit        NUMERIC(15,2) NOT NULL DEFAULT 0,
    base_interest_rate        NUMERIC(8,6)  NOT NULL DEFAULT 0,
    max_term_days             INT           NOT NULL DEFAULT 30,
    credit_multiplier         NUMERIC(6,4)  NOT NULL DEFAULT 1.0,
    UNIQUE (level_id)
);

CREATE INDEX IF NOT EXISTS idx_level_policies_level ON level_policies(level_id);

-- ── 5. level_term_curve ────────────────────────────────────
CREATE TABLE IF NOT EXISTS level_term_curve (
    id         SERIAL PRIMARY KEY,
    level_id   SMALLINT      NOT NULL REFERENCES levels(id),
    term_days  INT           NOT NULL,
    rate       NUMERIC(8,6)  NOT NULL,
    UNIQUE (level_id, term_days)
);

CREATE INDEX IF NOT EXISTS idx_level_term_curve_level ON level_term_curve(level_id);

-- ── 6. liquidity_snapshots ─────────────────────────────────
CREATE TABLE IF NOT EXISTS liquidity_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    level_id      SMALLINT      NOT NULL REFERENCES levels(id),
    total_capital NUMERIC(15,2) NOT NULL,
    committed     NUMERIC(15,2) NOT NULL DEFAULT 0,
    snapshot_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_liquidity_snapshots_level
    ON liquidity_snapshots(level_id);

-- ── 7. segments ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS segments (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(200) NOT NULL,
    description    TEXT,
    criteria_type  VARCHAR(20) NOT NULL DEFAULT 'manual',
    status         VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS segment_parameters (
    id           SERIAL PRIMARY KEY,
    segment_id   INT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    key          VARCHAR(100) NOT NULL,
    value        TEXT NOT NULL,
    value_type   VARCHAR(20) NOT NULL DEFAULT 'string',
    UNIQUE (segment_id, key)
);

CREATE INDEX IF NOT EXISTS idx_segment_parameters_segment
    ON segment_parameters(segment_id);

CREATE TABLE IF NOT EXISTS user_segments (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    segment_id INT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    reason     TEXT,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, segment_id)
);

CREATE INDEX IF NOT EXISTS idx_user_segments_user
    ON user_segments(user_id);
CREATE INDEX IF NOT EXISTS idx_user_segments_segment
    ON user_segments(segment_id);

-- ── 8. Add level_id to existing tables (keep group_id) ────
ALTER TABLE loan_requests
    ADD COLUMN IF NOT EXISTS level_id SMALLINT REFERENCES levels(id);

ALTER TABLE investment_opportunities
    ADD COLUMN IF NOT EXISTS level_id SMALLINT REFERENCES levels(id);

ALTER TABLE investments
    ADD COLUMN IF NOT EXISTS level_id SMALLINT REFERENCES levels(id);

ALTER TABLE liquidity_snapshots
    ADD COLUMN IF NOT EXISTS snapshot_at TIMESTAMPTZ;

-- ── 9. funds ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS funds (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_fund_status CHECK (status IN ('active', 'paused', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_funds_status ON funds(status);

-- ── 10. fund_policies ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS fund_policies (
    id             SERIAL PRIMARY KEY,
    fund_id        INT NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    criteria_type  VARCHAR(50) NOT NULL,
    criteria_value TEXT NOT NULL DEFAULT '',
    operator       VARCHAR(10) NOT NULL DEFAULT 'eq',
    logic_group    SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT chk_fp_operator CHECK (operator IN ('eq', 'gte', 'lte', 'in'))
);

CREATE INDEX IF NOT EXISTS idx_fund_policies_fund ON fund_policies(fund_id);

-- ── 11. fund_users ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fund_users (
    id             BIGSERIAL PRIMARY KEY,
    fund_id        INT NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    user_id        INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by       VARCHAR(50) NOT NULL DEFAULT 'system',
    removed_at     TIMESTAMPTZ,
    removal_reason TEXT,
    CONSTRAINT chk_fu_added_by CHECK (added_by IN ('system', 'admin', 'auto'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fund_users_active
    ON fund_users(fund_id, user_id)
    WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_fund_users_fund ON fund_users(fund_id);
CREATE INDEX IF NOT EXISTS idx_fund_users_user ON fund_users(user_id);

-- ── 12. fund_id on loan_requests and investment_opportunities
ALTER TABLE loan_requests
    ADD COLUMN IF NOT EXISTS fund_id INT REFERENCES funds(id) ON DELETE SET NULL;

ALTER TABLE investment_opportunities
    ADD COLUMN IF NOT EXISTS fund_id INT REFERENCES funds(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_loan_requests_fund
    ON loan_requests(fund_id) WHERE fund_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_investment_opp_fund
    ON investment_opportunities(fund_id) WHERE fund_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_loan_requests_level
    ON loan_requests(level_id) WHERE level_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_investment_opp_level
    ON investment_opportunities(level_id) WHERE level_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_investments_level
    ON investments(level_id) WHERE level_id IS NOT NULL;
