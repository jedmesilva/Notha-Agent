-- ============================================================
-- NOTHA — PostgreSQL Schema v3
-- Plataforma de empréstimos e investimentos via WhatsApp
-- Apply via Supabase SQL Editor
-- ============================================================

-- ============================================================
-- MÓDULO 1 — IDENTIDADE E AUTENTICAÇÃO
-- ============================================================

-- 1.1 Users (identity)
CREATE TABLE IF NOT EXISTS users (
    id                  SERIAL PRIMARY KEY,
    tax_id              VARCHAR(14) UNIQUE,
    full_name           VARCHAR(200),
    nickname            VARCHAR(60),
    identity_status     VARCHAR(20) NOT NULL DEFAULT 'unverified',
        -- unverified | under_review | verified | rejected
    current_level       SMALLINT NOT NULL DEFAULT 1,
        -- nível de risco/crédito atual (1 = menor, 10 = maior)
    city                VARCHAR(100),
    neighborhood        VARCHAR(100),
    street              VARCHAR(200),
    street_number       VARCHAR(20),
    state               VARCHAR(100),
    country             VARCHAR(100),
    zip_code            VARCHAR(20),
    gender              VARCHAR(30),
    date_of_birth       DATE,
    preferred_language  VARCHAR(10) DEFAULT 'pt',
    pix_key             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_identity_status
        CHECK (identity_status IN ('unverified', 'under_review', 'verified', 'rejected')),
    CONSTRAINT chk_user_level
        CHECK (current_level BETWEEN 1 AND 10)
);

CREATE INDEX IF NOT EXISTS idx_users_identity_status ON users(identity_status);
CREATE INDEX IF NOT EXISTS idx_users_level           ON users(current_level);

-- 1.2 Phone numbers
CREATE TABLE IF NOT EXISTS user_phone_numbers (
    id            SERIAL PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone         VARCHAR(20) UNIQUE NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    country_code  SMALLINT,
    country_iso   VARCHAR(2),
    country_name  VARCHAR(100),
    region        VARCHAR(150),
    carrier       VARCHAR(100),
    timezone      VARCHAR(60),
    number_type   VARCHAR(30),
    is_valid      BOOLEAN,
    parsed_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_phone_per_user
    ON user_phone_numbers(user_id) WHERE active = TRUE;

-- 1.3 Identity documents
CREATE TABLE IF NOT EXISTS identity_documents (
    id                SERIAL PRIMARY KEY,
    user_id           INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    doc_type          VARCHAR(30) NOT NULL DEFAULT 'unknown',
        -- national_id | drivers_license | passport | unknown
    image_url         TEXT NOT NULL,
    whatsapp_media_id TEXT,
    status            VARCHAR(20) NOT NULL DEFAULT 'under_review',
        -- under_review | approved | rejected
    rejection_reason  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at       TIMESTAMPTZ,
    reviewed_by       TEXT
);

CREATE INDEX IF NOT EXISTS idx_identity_documents_user   ON identity_documents(user_id);
CREATE INDEX IF NOT EXISTS idx_identity_documents_status ON identity_documents(status);

-- 1.4 Sessions
CREATE TABLE IF NOT EXISTS sessions (
    id               SERIAL PRIMARY KEY,
    user_id          INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone            VARCHAR(20) NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | pending_reauth | revoked
    reauth_tier      VARCHAR(20),
        -- cpf | selfie | link
    reauth_attempts  INT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reauthed_at      TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_phone_live
    ON sessions(phone)
    WHERE status IN ('active', 'pending_reauth');

CREATE INDEX IF NOT EXISTS idx_sessions_phone  ON sessions(phone);
CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

-- 1.5 Pending verifications (link-tier re-auth)
CREATE TABLE IF NOT EXISTS pending_verifications (
    id         SERIAL PRIMARY KEY,
    session_id INT  NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id    INT  NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    phone      VARCHAR(20) NOT NULL,
    token      TEXT UNIQUE NOT NULL,
    status     VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | completed | failed | expired
    result     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pv_token  ON pending_verifications(token);
CREATE INDEX IF NOT EXISTS idx_pv_phone  ON pending_verifications(phone);
CREATE INDEX IF NOT EXISTS idx_pv_status ON pending_verifications(status);

-- 1.6 Webhook deduplication
CREATE TABLE IF NOT EXISTS processed_webhook_msgs (
    msg_id       TEXT        PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pwm_processed_at ON processed_webhook_msgs(processed_at);

-- ============================================================
-- MÓDULO 2 — OPEN FINANCE (PLUGGY)
-- ============================================================

-- 2.1 Pluggy connections
CREATE TABLE IF NOT EXISTS pluggy_connections (
    id                   SERIAL PRIMARY KEY,
    user_id              INT REFERENCES users(id) ON DELETE SET NULL,
    phone                VARCHAR(20) NOT NULL,
    token                TEXT NOT NULL UNIQUE,
    pluggy_item_id       TEXT,
    pluggy_connect_token TEXT,
    status               VARCHAR(30) NOT NULL DEFAULT 'pending',
        -- pending | connected | error | expired
    connectors           JSONB,
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ NOT NULL,
    completed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pluggy_connections_user   ON pluggy_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_pluggy_connections_phone  ON pluggy_connections(phone);
CREATE INDEX IF NOT EXISTS idx_pluggy_connections_status ON pluggy_connections(status);
CREATE INDEX IF NOT EXISTS idx_pluggy_connections_item   ON pluggy_connections(pluggy_item_id);

-- 2.2 Bank accounts
CREATE TABLE IF NOT EXISTS bank_accounts (
    id                   SERIAL PRIMARY KEY,
    user_id              INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pluggy_connection_id INT NOT NULL REFERENCES pluggy_connections(id) ON DELETE CASCADE,
    pluggy_account_id    TEXT NOT NULL UNIQUE,
    institution_name     VARCHAR(200),
    institution_number   VARCHAR(10),
    account_type         VARCHAR(30),
        -- checking | savings | credit | investment | other
    account_number       VARCHAR(30),
    branch_number        VARCHAR(10),
    owner_name           TEXT,
    tax_number           VARCHAR(14),
    balance              NUMERIC(15,2),
    currency_code        VARCHAR(3) DEFAULT 'BRL',
    pix_key              TEXT,
    raw_data             JSONB,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bank_accounts_user       ON bank_accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_bank_accounts_connection ON bank_accounts(pluggy_connection_id);

-- 2.3 Bank transactions cache
CREATE TABLE IF NOT EXISTS bank_transactions_cache (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bank_account_id      INT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    pluggy_tx_id         TEXT NOT NULL UNIQUE,
    amount               NUMERIC(15,2) NOT NULL,
    type                 VARCHAR(30),
        -- credit | debit
    description          TEXT,
    category             VARCHAR(100),
    date                 DATE NOT NULL,
    balance_after        NUMERIC(15,2),
    raw_data             JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bank_tx_user    ON bank_transactions_cache(user_id);
CREATE INDEX IF NOT EXISTS idx_bank_tx_account ON bank_transactions_cache(bank_account_id);
CREATE INDEX IF NOT EXISTS idx_bank_tx_date    ON bank_transactions_cache(date DESC);

-- ============================================================
-- MÓDULO 3 — NÍVEIS, SEGMENTOS E ALOCAÇÃO DE USUÁRIOS
-- ============================================================

-- 3.1 Níveis de risco/crédito (1 = menor risco/limite, 10 = maior risco/limite)
--     Um usuário só pode estar em um nível por vez (users.current_level).
--     Cada nível possui políticas financeiras definidas em level_policies.
CREATE TABLE IF NOT EXISTS levels (
    id          SMALLINT PRIMARY KEY CHECK (id BETWEEN 1 AND 10),
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | inactive
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3.2 Histórico de transições de nível do usuário
--     Toda mudança de nível é registrada aqui (imutável — append only).
CREATE TABLE IF NOT EXISTS user_level_history (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_level    SMALLINT REFERENCES levels(id),
    to_level      SMALLINT NOT NULL REFERENCES levels(id),
    trigger_score NUMERIC(6,2),
    reason        TEXT,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by    TEXT NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_user_level_history_user    ON user_level_history(user_id);
CREATE INDEX IF NOT EXISTS idx_user_level_history_changed ON user_level_history(user_id, changed_at DESC);

-- 3.3 Segmentos — agrupam usuários com parâmetros em comum.
--     Um usuário pode estar em múltiplos segmentos ao mesmo tempo.
--     Segmentos NÃO controlam limite financeiro (isso é papel do nível).
--     Segmentos aplicam ajustes e contexto sobre as políticas do nível.
CREATE TABLE IF NOT EXISTS segments (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(200) NOT NULL,
    description    TEXT,
    criteria_type  VARCHAR(20) NOT NULL DEFAULT 'manual',
        -- manual | auto
    status         VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | inactive
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3.4 Parâmetros de segmento (key-value flexíveis por segmento)
--     Ex: rate_adjustment_bps = -50, region = "nordeste", profile = "agricultor"
CREATE TABLE IF NOT EXISTS segment_parameters (
    id           SERIAL PRIMARY KEY,
    segment_id   INT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    key          VARCHAR(100) NOT NULL,
    value        TEXT NOT NULL,
    value_type   VARCHAR(20) NOT NULL DEFAULT 'string',
        -- string | number | boolean | json
    UNIQUE (segment_id, key)
);

CREATE INDEX IF NOT EXISTS idx_segment_parameters_segment ON segment_parameters(segment_id);

-- 3.5 Membros de segmento (M×N — usuário pode estar em vários segmentos)
CREATE TABLE IF NOT EXISTS user_segments (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    segment_id INT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason     TEXT,
    UNIQUE (user_id, segment_id)
);

CREATE INDEX IF NOT EXISTS idx_user_segments_user    ON user_segments(user_id);
CREATE INDEX IF NOT EXISTS idx_user_segments_segment ON user_segments(segment_id);

-- ============================================================
-- MÓDULO 4 — WALLETS (CARTEIRAS)
-- ============================================================

-- Polimórfico: owner_type = 'user' | 'level' | 'platform'
-- Cada nível tem sua própria carteira (pool de empréstimos).
CREATE TABLE IF NOT EXISTS wallets (
    id            SERIAL PRIMARY KEY,
    owner_type    VARCHAR(20) NOT NULL,
        -- user | level | platform
    owner_id      INT NOT NULL,
    balance_cache NUMERIC(15,2) NOT NULL DEFAULT 0,
        -- cache — fonte de verdade é SUM(wallet_transactions.amount)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_wallet_owner_type CHECK (owner_type IN ('user', 'level', 'platform')),
    CONSTRAINT uq_wallet_owner UNIQUE (owner_type, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_wallets_owner ON wallets(owner_type, owner_id);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id             BIGSERIAL PRIMARY KEY,
    wallet_id      INT NOT NULL REFERENCES wallets(id) ON DELETE RESTRICT,
    amount         NUMERIC(15,2) NOT NULL,
        -- positivo = crédito, negativo = débito
    type           VARCHAR(40) NOT NULL,
        -- loan_disbursement | loan_repayment | investment_deposit |
        -- investment_withdrawal | interest_payout | fee | adjustment
    reference_id   TEXT,
    reference_type VARCHAR(50),
    description    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_wallet_tx_type CHECK (type IN (
        'loan_disbursement', 'loan_repayment', 'investment_deposit',
        'investment_withdrawal', 'interest_payout', 'fee', 'adjustment'
    ))
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_wallet  ON wallet_transactions(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_created ON wallet_transactions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_ref     ON wallet_transactions(reference_type, reference_id);

-- ============================================================
-- MÓDULO 5 — SCORING DE RISCO MULTI-FATOR
-- ============================================================

-- 5.1 Localização do usuário
CREATE TABLE IF NOT EXISTS user_locations (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    city       VARCHAR(100),
    state      VARCHAR(100),
    country    VARCHAR(100) DEFAULT 'BR',
    geohash    VARCHAR(12),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_locations_user    ON user_locations(user_id);
CREATE INDEX IF NOT EXISTS idx_user_locations_geohash ON user_locations(geohash);

-- 5.2 Incidentes de risco geográfico (feed externo)
CREATE TABLE IF NOT EXISTS location_risk_events (
    id          SERIAL PRIMARY KEY,
    geohash     VARCHAR(12) NOT NULL,
    event_type  VARCHAR(30) NOT NULL,
        -- climate | economic | social | other
    severity    SMALLINT NOT NULL DEFAULT 1 CHECK (severity BETWEEN 1 AND 5),
    description TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_location_risk_geohash ON location_risk_events(geohash);
CREATE INDEX IF NOT EXISTS idx_location_risk_type    ON location_risk_events(event_type);

-- 5.3 Métricas de mercado por região (recalculadas por job periódico)
CREATE TABLE IF NOT EXISTS location_market_metrics (
    id                         SERIAL PRIMARY KEY,
    geohash                    VARCHAR(12) NOT NULL,
    active_loan_demand_local   NUMERIC(15,2) DEFAULT 0,
    avg_requested_amount_local NUMERIC(15,2) DEFAULT 0,
    active_investors_count     INT DEFAULT 0,
    available_investment_local NUMERIC(15,2) DEFAULT 0,
    calculated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_location_mkt_geohash ON location_market_metrics(geohash);

-- 5.4 Métricas comportamentais do usuário (recalculadas por job diário)
CREATE TABLE IF NOT EXISTS user_behavior_metrics (
    id                          SERIAL PRIMARY KEY,
    user_id                     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_borrowed_amount       NUMERIC(15,2) DEFAULT 0,
    total_repaid_amount         NUMERIC(15,2) DEFAULT 0,
    loan_request_frequency_90d  INT DEFAULT 0,
    payment_frequency_score     NUMERIC(5,4) DEFAULT 0,
        -- 0.0 a 1.0 — proporção de pagamentos em dia
    late_payments_count         INT DEFAULT 0,
    defaults_count              INT DEFAULT 0,
    total_invested_amount       NUMERIC(15,2) DEFAULT 0,
    investment_frequency_90d    INT DEFAULT 0,
    -- Dados derivados do Open Finance (Pluggy)
    avg_monthly_income_pluggy   NUMERIC(15,2),
    avg_monthly_expenses_pluggy NUMERIC(15,2),
    bank_account_age_days       INT,
    calculated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until                 TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_behavior_metrics_user ON user_behavior_metrics(user_id);

-- 5.5 Modelos de scoring (versionados)
CREATE TABLE IF NOT EXISTS risk_score_models (
    id          SERIAL PRIMARY KEY,
    version     VARCHAR(20) NOT NULL,
    description TEXT,
    active      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS risk_score_weights (
    id          SERIAL PRIMARY KEY,
    model_id    INT NOT NULL REFERENCES risk_score_models(id) ON DELETE CASCADE,
    factor_name VARCHAR(100) NOT NULL,
    weight      NUMERIC(8,4) NOT NULL,
    UNIQUE (model_id, factor_name)
);

-- 5.6 Scores de risco calculados (nunca sobrescreve — append only)
CREATE TABLE IF NOT EXISTS user_risk_scores (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id      INT NOT NULL REFERENCES risk_score_models(id),
    score         NUMERIC(7,4) NOT NULL,
    factors_json  JSONB NOT NULL DEFAULT '{}',
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_risk_scores_user       ON user_risk_scores(user_id);
CREATE INDEX IF NOT EXISTS idx_risk_scores_calculated ON user_risk_scores(user_id, calculated_at DESC);

-- ============================================================
-- MÓDULO 6 — POLÍTICAS DE NÍVEL (TAXAS E LIMITES)
-- ============================================================

-- 6.1 Política financeira por nível (substitui group_rate_policies + group_pool_limits)
--     Cada nível tem uma política ativa (a mais recente por effective_from).
--     Contém: taxas, spread, fórmula de ajuste por prazo e limites de exposição.
CREATE TABLE IF NOT EXISTS level_policies (
    id                        SERIAL PRIMARY KEY,
    level_id                  SMALLINT NOT NULL REFERENCES levels(id),
    -- Taxas base
    base_borrowing_rate       NUMERIC(8,6) NOT NULL,  -- taxa base cobrada de tomadores
    base_investment_rate      NUMERIC(8,6) NOT NULL,  -- taxa base paga a investidores
    min_spread                NUMERIC(8,6) NOT NULL,  -- margem mínima obrigatória
    spread_violation_strategy VARCHAR(30) NOT NULL DEFAULT 'reject_investment',
        -- reject_investment | raise_borrowing_rate
    -- Fórmula de ajuste por prazo
    term_rate_formula         VARCHAR(20) NOT NULL DEFAULT 'bands',
        -- bands | linear | log | sqrt
    term_rate_base_bps        NUMERIC(8,2),           -- base para fórmulas linear/log/sqrt
    term_rate_scale           NUMERIC(8,4),           -- coeficiente de escala
    -- Limites de exposição do pool deste nível
    max_aggregate_exposure    NUMERIC(15,2) NOT NULL, -- exposição total máxima do nível
    max_per_user_limit        NUMERIC(15,2),          -- teto máximo por usuário individual
    current_exposure_cache    NUMERIC(15,2) NOT NULL DEFAULT 0,
        -- cache — reconciliável via SUM(debts.principal) ativas no nível
    -- Limite individual padrão (fallback quando não há credit_limit individual)
    default_individual_limit  NUMERIC(15,2),
    effective_from            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_level_policies_level ON level_policies(level_id);

-- 6.2 Curva de ajuste por prazo por nível (usada quando term_rate_formula = 'bands')
CREATE TABLE IF NOT EXISTS level_term_curve (
    id             SERIAL PRIMARY KEY,
    level_id       SMALLINT NOT NULL REFERENCES levels(id),
    min_term_days  INT NOT NULL,
    max_term_days  INT NOT NULL,
    adjustment_bps INT NOT NULL DEFAULT 0  -- basis points (100 bps = 1%)
);

CREATE INDEX IF NOT EXISTS idx_level_term_curve_level ON level_term_curve(level_id);

-- 6.3 Snapshot de liquidez por nível (atualizado por job periódico a cada 5–15 min)
CREATE TABLE IF NOT EXISTS liquidity_snapshots (
    id                           SERIAL PRIMARY KEY,
    level_id                     SMALLINT NOT NULL REFERENCES levels(id),
    total_available_investment   NUMERIC(15,2) NOT NULL DEFAULT 0,
    total_active_loan_demand     NUMERIC(15,2) NOT NULL DEFAULT 0,
    captured_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_liquidity_snapshots_level ON liquidity_snapshots(level_id);

-- ============================================================
-- MÓDULO 7 — LIMITES DE CRÉDITO INDIVIDUAIS
-- ============================================================

-- Limite individual por usuário no nível credor.
-- mode='fixed'      → usa limit_amount diretamente
-- mode='percentage' → usa limit_percentage × level_policies.max_per_user_limit
-- mode='score_band' → resolve via scoring (padrão para novos usuários)
CREATE TABLE IF NOT EXISTS credit_limits (
    id               SERIAL PRIMARY KEY,
    borrower_type    VARCHAR(20) NOT NULL DEFAULT 'user',
        -- user (expansível futuramente)
    borrower_id      INT NOT NULL,
    lender_level_id  SMALLINT NOT NULL REFERENCES levels(id),
    mode             VARCHAR(20) NOT NULL DEFAULT 'score_band',
        -- fixed | percentage | score_band
    limit_amount     NUMERIC(15,2),          -- obrigatório se mode='fixed'
    limit_percentage NUMERIC(5,4),           -- obrigatório se mode='percentage'
    effective_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_credit_limit_borrower_type CHECK (borrower_type IN ('user')),
    CONSTRAINT chk_credit_limit_mode          CHECK (mode IN ('fixed', 'percentage', 'score_band'))
);

CREATE INDEX IF NOT EXISTS idx_credit_limits_borrower ON credit_limits(borrower_type, borrower_id);
CREATE INDEX IF NOT EXISTS idx_credit_limits_level    ON credit_limits(lender_level_id);

-- ============================================================
-- MÓDULO 8 — SOLICITAÇÃO E PROPOSTA DE EMPRÉSTIMO
-- ============================================================

CREATE TABLE IF NOT EXISTS loan_requests (
    id               SERIAL PRIMARY KEY,
    user_id          INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level_id         SMALLINT NOT NULL REFERENCES levels(id),
    requested_amount NUMERIC(15,2) NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | approved | rejected | expired
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at       TIMESTAMPTZ,
    decided_by       TEXT,
    rejection_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_loan_requests_user   ON loan_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_loan_requests_level  ON loan_requests(level_id);
CREATE INDEX IF NOT EXISTS idx_loan_requests_status ON loan_requests(status);

-- Plano proposto pelo usuário
CREATE TABLE IF NOT EXISTS proposed_installments (
    id                 SERIAL PRIMARY KEY,
    loan_request_id    INT NOT NULL REFERENCES loan_requests(id) ON DELETE CASCADE,
    sequence           INT NOT NULL,
    proposed_due_date  DATE NOT NULL,
    proposed_amount    NUMERIC(15,2) NOT NULL,
    distribution_type  VARCHAR(20) NOT NULL DEFAULT 'equal'
        -- equal | custom
);

CREATE INDEX IF NOT EXISTS idx_proposed_installments_request ON proposed_installments(loan_request_id);

-- ============================================================
-- MÓDULO 9 — COTAÇÃO DE TAXA
-- ============================================================

CREATE TABLE IF NOT EXISTS loan_rate_quotes (
    id                   SERIAL PRIMARY KEY,
    loan_request_id      INT NOT NULL REFERENCES loan_requests(id) ON DELETE CASCADE,
    base_rate            NUMERIC(8,6) NOT NULL,
    risk_premium         NUMERIC(8,6) NOT NULL DEFAULT 0,
    term_adjustment      NUMERIC(8,6) NOT NULL DEFAULT 0,
    liquidity_multiplier NUMERIC(8,6) NOT NULL DEFAULT 1,
    final_rate           NUMERIC(8,6) NOT NULL,
    breakdown_json       JSONB NOT NULL DEFAULT '{}',
    quoted_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_loan_rate_quotes_request ON loan_rate_quotes(loan_request_id);

CREATE TABLE IF NOT EXISTS investment_rate_quotes (
    id                   SERIAL PRIMARY KEY,
    investment_id        INT,
    base_rate            NUMERIC(8,6) NOT NULL,
    liquidity_multiplier NUMERIC(8,6) NOT NULL DEFAULT 1,
    final_rate           NUMERIC(8,6) NOT NULL,
    quoted_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- MÓDULO 10 — DÍVIDAS, PARCELAS, PAGAMENTOS E ALOCAÇÕES
-- ============================================================

CREATE TABLE IF NOT EXISTS debts (
    id                      SERIAL PRIMARY KEY,
    loan_request_id         INT NOT NULL REFERENCES loan_requests(id),
    from_wallet_id          INT NOT NULL REFERENCES wallets(id),
    to_wallet_id            INT NOT NULL REFERENCES wallets(id),
    principal               NUMERIC(15,2) NOT NULL,
    interest_rate_applied   NUMERIC(8,6) NOT NULL,
    term_days               INT NOT NULL,
    status                  VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | paid_off | defaulted | renegotiated
    overpayment_strategy    VARCHAR(30) NOT NULL DEFAULT 'advance_installments',
        -- advance_installments | reduce_principal
    disbursed_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_debts_loan_request ON debts(loan_request_id);
CREATE INDEX IF NOT EXISTS idx_debts_status       ON debts(status);
CREATE INDEX IF NOT EXISTS idx_debts_wallets      ON debts(from_wallet_id, to_wallet_id);

CREATE TABLE IF NOT EXISTS debt_installments (
    id               SERIAL PRIMARY KEY,
    debt_id          INT NOT NULL REFERENCES debts(id) ON DELETE CASCADE,
    sequence         INT NOT NULL,
    due_date         DATE NOT NULL,
    amount_due       NUMERIC(15,2) NOT NULL,
    remaining_amount NUMERIC(15,2) NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | partially_paid | paid | overdue
    CONSTRAINT chk_installment_status
        CHECK (status IN ('pending', 'partially_paid', 'paid', 'overdue'))
);

CREATE INDEX IF NOT EXISTS idx_debt_installments_debt   ON debt_installments(debt_id);
CREATE INDEX IF NOT EXISTS idx_debt_installments_status ON debt_installments(status);
CREATE INDEX IF NOT EXISTS idx_debt_installments_due    ON debt_installments(due_date) WHERE status != 'paid';

CREATE TABLE IF NOT EXISTS payments (
    id              SERIAL PRIMARY KEY,
    debt_id         INT NOT NULL REFERENCES debts(id) ON DELETE CASCADE,
    paid_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    amount_paid     NUMERIC(15,2) NOT NULL,
    payment_method  VARCHAR(30) NOT NULL DEFAULT 'pix',
    asaas_charge_id TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_payments_debt ON payments(debt_id);

CREATE TABLE IF NOT EXISTS payment_allocations (
    id               SERIAL PRIMARY KEY,
    payment_id       INT NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    installment_id   INT NOT NULL REFERENCES debt_installments(id) ON DELETE RESTRICT,
    amount_allocated NUMERIC(15,2) NOT NULL,
    CONSTRAINT chk_amount_positive CHECK (amount_allocated > 0)
);

CREATE INDEX IF NOT EXISTS idx_payment_allocations_payment     ON payment_allocations(payment_id);
CREATE INDEX IF NOT EXISTS idx_payment_allocations_installment ON payment_allocations(installment_id);

-- ============================================================
-- MÓDULO 10b — INVESTIMENTOS E OPORTUNIDADES DE CAPTAÇÃO
-- ============================================================

-- Oportunidade de captação — criada após aprovação de empréstimo
-- para repor o saldo retirado do nível. Também criada manualmente.
CREATE TABLE IF NOT EXISTS investment_opportunities (
    id               BIGSERIAL PRIMARY KEY,
    level_id         SMALLINT      NOT NULL REFERENCES levels(id),
    debt_id          BIGINT        REFERENCES debts(id),
        -- NULL = captação geral de liquidez, sem empréstimo subjacente
    amount_needed    NUMERIC(15,2) NOT NULL,
    amount_committed NUMERIC(15,2) NOT NULL DEFAULT 0,
    expected_rate    NUMERIC(8,6)  NOT NULL,
    status           VARCHAR(20)   NOT NULL DEFAULT 'open',
        -- open | partially_funded | fully_funded | expired | cancelled
    expires_at       TIMESTAMPTZ   NOT NULL,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_opp_status
        CHECK (status IN ('open','partially_funded','fully_funded','expired','cancelled')),
    CONSTRAINT chk_committed_lte_needed
        CHECK (amount_committed <= amount_needed)
);

CREATE INDEX IF NOT EXISTS idx_investment_opp_level  ON investment_opportunities(level_id);
CREATE INDEX IF NOT EXISTS idx_investment_opp_debt   ON investment_opportunities(debt_id) WHERE debt_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_opp_status ON investment_opportunities(status);

-- Posição de um investidor em um nível/fundo.
CREATE TABLE IF NOT EXISTS investments (
    id               BIGSERIAL PRIMARY KEY,
    investor_user_id INT           NOT NULL REFERENCES users(id),
    level_id         SMALLINT      NOT NULL REFERENCES levels(id),
    opportunity_id   BIGINT        REFERENCES investment_opportunities(id),
    debt_id          BIGINT        REFERENCES debts(id),
    amount_invested  NUMERIC(15,2) NOT NULL,
    rate_agreed      NUMERIC(8,6)  NOT NULL,
    status           VARCHAR(20)   NOT NULL DEFAULT 'active',
        -- active | matured | withdrawn
    invested_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    maturity_date    DATE,
    maturity_at      TIMESTAMPTZ,
    withdrawn_at     TIMESTAMPTZ,
    CONSTRAINT chk_investment_status
        CHECK (status IN ('active','matured','withdrawn'))
);

CREATE INDEX IF NOT EXISTS idx_investments_investor ON investments(investor_user_id);
CREATE INDEX IF NOT EXISTS idx_investments_level    ON investments(level_id);
CREATE INDEX IF NOT EXISTS idx_investments_opp      ON investments(opportunity_id) WHERE opportunity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investments_debt     ON investments(debt_id) WHERE debt_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investments_status   ON investments(status);

-- Payout único no vencimento do investimento (juros simples: I = P × r × t).
CREATE TABLE IF NOT EXISTS investment_payouts (
    id             BIGSERIAL PRIMARY KEY,
    investment_id  BIGINT        NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
    amount         NUMERIC(15,2) NOT NULL,
    period_start   DATE          NOT NULL,
    period_end     DATE          NOT NULL,
    scheduled_date DATE          NOT NULL,
    scheduled_at   TIMESTAMPTZ,
    status         VARCHAR(20)   NOT NULL DEFAULT 'scheduled',
        -- scheduled | paid | cancelled
    paid_at        TIMESTAMPTZ,
    CONSTRAINT chk_payout_status CHECK (status IN ('scheduled','paid','cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_investment_payouts_investment ON investment_payouts(investment_id);
CREATE INDEX IF NOT EXISTS idx_investment_payouts_due
    ON investment_payouts(scheduled_date) WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS idx_investment_payouts_due_at
    ON investment_payouts(scheduled_at) WHERE status = 'scheduled' AND scheduled_at IS NOT NULL;

-- Ofertas de investimento enviadas a investidores específicos via WhatsApp.
-- Restrição parcial: apenas UMA oferta PENDING por (opportunity, user).
CREATE TABLE IF NOT EXISTS investment_offers (
    id               SERIAL PRIMARY KEY,
    opportunity_id   INT           NOT NULL REFERENCES investment_opportunities(id),
    user_id          INT           NOT NULL REFERENCES users(id),
    level_id         SMALLINT      NOT NULL REFERENCES levels(id),
    suggested_amount NUMERIC(15,2) NOT NULL,
    maturity_at      TIMESTAMPTZ   NOT NULL,
    status           VARCHAR(20)   NOT NULL DEFAULT 'pending',
        -- pending | processing | accepted | declined | expired
    message_sent_at  TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
    responded_at     TIMESTAMPTZ,
    final_amount     NUMERIC(15,2),
    investment_id    INT REFERENCES investments(id),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_offer_status
        CHECK (status IN ('pending','processing','accepted','declined','expired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_offers_pending_unique
    ON investment_offers(opportunity_id, user_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_investment_offers_lookup
    ON investment_offers(user_id, status, expires_at);

-- Perfis de preferência de investidor.
-- level_id = NULL → aceita qualquer nível.
CREATE TABLE IF NOT EXISTS investor_profiles (
    id                      SERIAL PRIMARY KEY,
    user_id                 INT           NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    level_id                SMALLINT      REFERENCES levels(id) ON DELETE SET NULL,
        -- NULL = aceita qualquer nível
    risk_tolerance          VARCHAR(20)   NOT NULL DEFAULT 'moderate',
        -- conservative | moderate | aggressive
    min_investment_amount   NUMERIC(15,2) NOT NULL DEFAULT 50,
    max_investment_amount   NUMERIC(15,2),
    min_term_days           INT           NOT NULL DEFAULT 1,
    max_term_days           INT           NOT NULL DEFAULT 365,
    auto_invest             BOOLEAN       NOT NULL DEFAULT FALSE,
    is_active               BOOLEAN       NOT NULL DEFAULT TRUE,
    avg_investment_amount   NUMERIC(15,2),
    avg_term_days           INT,
    total_invested_lifetime NUMERIC(15,2) NOT NULL DEFAULT 0,
    active_investment_count INT           NOT NULL DEFAULT 0,
    last_metrics_at         TIMESTAMPTZ,
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_risk_tolerance
        CHECK (risk_tolerance IN ('conservative','moderate','aggressive'))
);

CREATE INDEX IF NOT EXISTS idx_investor_profiles_active ON investor_profiles(is_active, level_id);

-- ============================================================
-- MÓDULO 11 — INFRAESTRUTURA DO AGENTE CONVERSACIONAL
-- ============================================================

-- 11.1 Histórico de conversas LLM
CREATE TABLE IF NOT EXISTS conversation_messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       VARCHAR(20) NOT NULL,
        -- user | assistant | system
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_role CHECK (role IN ('user', 'assistant', 'system'))
);

CREATE INDEX IF NOT EXISTS idx_conv_messages_user_created
    ON conversation_messages(user_id, created_at DESC);

-- 11.2 Estado de turno (field-by-field dialog state)
CREATE TABLE IF NOT EXISTS turn_state (
    phone         VARCHAR(20) PRIMARY KEY,
    pending_field VARCHAR(100) NOT NULL,
    operation     VARCHAR(100) NOT NULL DEFAULT '',
    context_data  JSONB        NOT NULL DEFAULT '{}',
    asked_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '30 minutes',
    attempt_count INTEGER      NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_turn_state_expires ON turn_state(expires_at);

-- 11.3 Confirmações pendentes (decisões sim/não aguardando resposta)
CREATE TABLE IF NOT EXISTS pending_confirmations (
    phone      VARCHAR(20) PRIMARY KEY,
    type       VARCHAR(100) NOT NULL,
    data       JSONB        NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '2 hours'
);

CREATE INDEX IF NOT EXISTS idx_pending_confirmations_expires ON pending_confirmations(expires_at);

-- ============================================================
-- MÓDULO 12 — CONTROLE E COMPLIANCE
-- ============================================================

-- 12.1 Itens restritos (lista dinâmica gerida por admin)
CREATE TABLE IF NOT EXISTS restricted_items (
    id           SERIAL PRIMARY KEY,
    category     VARCHAR(100) NOT NULL,
    keywords     TEXT[]       NOT NULL DEFAULT '{}',
    description  TEXT,
    reason       TEXT         NOT NULL,
    scope        VARCHAR(20)  NOT NULL DEFAULT 'national'
        CHECK (scope IN ('national', 'state', 'municipal')),
    state_code   CHAR(2),
    municipality VARCHAR(100),
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by   TEXT
);

CREATE INDEX IF NOT EXISTS idx_restricted_items_category  ON restricted_items(category);
CREATE INDEX IF NOT EXISTS idx_restricted_items_is_active ON restricted_items(is_active);
CREATE INDEX IF NOT EXISTS idx_restricted_items_keywords  ON restricted_items USING GIN(keywords);

-- ============================================================
-- VIEWS DE RECONCILIAÇÃO
-- ============================================================

-- Saldo real de wallet (fonte de verdade; use para auditoria)
CREATE OR REPLACE VIEW wallet_true_balance AS
SELECT
    w.id            AS wallet_id,
    w.owner_type,
    w.owner_id,
    w.balance_cache AS cached_balance,
    COALESCE(SUM(wt.amount), 0) AS true_balance,
    w.balance_cache - COALESCE(SUM(wt.amount), 0) AS cache_drift
FROM wallets w
LEFT JOIN wallet_transactions wt ON wt.wallet_id = w.id
GROUP BY w.id;

-- Parcelas em atraso
CREATE OR REPLACE VIEW overdue_installments AS
SELECT
    di.*,
    d.loan_request_id,
    d.interest_rate_applied,
    lr.user_id,
    lr.level_id
FROM debt_installments di
JOIN debts d ON d.id = di.debt_id
JOIN loan_requests lr ON lr.id = d.loan_request_id
WHERE di.status IN ('pending', 'partially_paid')
  AND di.due_date < CURRENT_DATE;

-- ============================================================
-- MÓDULO 11 — FUNDOS DE CRÉDITO
-- ============================================================
--
-- Funds are credit pools from which borrowers take loans.
-- Investors have NO direct relationship with funds.
-- Investors see investment_opportunities filtered by the borrower's segment.
--
-- Flow:
--   user → added to fund (fund_users) → takes loan from fund (loan_requests.fund_id)
--       → debt created → investment_opportunity created (opportunity.fund_id)
--           → investor sees opportunity (filtered by borrower's segment) → invests

-- 11.1 Fund — credit pool entity
CREATE TABLE IF NOT EXISTS funds (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | paused | closed
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_fund_status CHECK (status IN ('active', 'paused', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_funds_status ON funds(status);

-- 11.2 Fund policies — eligibility criteria to add a user to a fund
--
-- Multiple rows per fund define a boolean expression:
--   rows sharing the same logic_group are AND-ed together;
--   rows in different logic_groups are OR-ed.
--
-- Example: (min_level >= 3 AND segment = 'rural') OR (min_level >= 7)
--   group 0: criteria_type='min_level',        criteria_value='3', operator='gte'
--   group 0: criteria_type='segment_membership', criteria_value='rural', operator='eq'
--   group 1: criteria_type='min_level',        criteria_value='7', operator='gte'
--
-- Supported criteria_type values:
--   'min_level'          — users.current_level
--   'segment_membership' — user must belong to segment (criteria_value = segment name or id)
--   'min_kyc_tier'       — users.identity_status (criteria_value = tier number)
--   'min_account_age_days' — days since users.created_at
--   'manual_only'        — no automatic rule; admin adds manually (criteria_value ignored)
CREATE TABLE IF NOT EXISTS fund_policies (
    id             SERIAL PRIMARY KEY,
    fund_id        INT NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    criteria_type  VARCHAR(50) NOT NULL,
    criteria_value TEXT NOT NULL DEFAULT '',
    operator       VARCHAR(10) NOT NULL DEFAULT 'eq',
        -- eq | gte | lte | in
    logic_group    SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT chk_fp_operator CHECK (operator IN ('eq', 'gte', 'lte', 'in'))
);

CREATE INDEX IF NOT EXISTS idx_fund_policies_fund ON fund_policies(fund_id);

-- 11.3 Fund users — users allocated to a fund (soft delete for removal history)
--
-- A user may belong to multiple funds simultaneously.
-- removed_at IS NULL  → currently active in fund
-- removed_at IS NOT NULL → removed (historical record kept)
CREATE TABLE IF NOT EXISTS fund_users (
    id             BIGSERIAL PRIMARY KEY,
    fund_id        INT NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    user_id        INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by       VARCHAR(50) NOT NULL DEFAULT 'system',
        -- system | admin | auto
    removed_at     TIMESTAMPTZ,
    removal_reason TEXT,
    CONSTRAINT chk_fu_added_by CHECK (added_by IN ('system', 'admin', 'auto'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fund_users_active
    ON fund_users(fund_id, user_id)
    WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_fund_users_fund   ON fund_users(fund_id);
CREATE INDEX IF NOT EXISTS idx_fund_users_user   ON fund_users(user_id);
CREATE INDEX IF NOT EXISTS idx_fund_users_active_bool
    ON fund_users(fund_id, removed_at);

-- 11.4 Attach fund_id to loan_requests and investment_opportunities
--      (nullable: existing rows are unaffected)
ALTER TABLE loan_requests
    ADD COLUMN IF NOT EXISTS fund_id INT REFERENCES funds(id) ON DELETE SET NULL;

ALTER TABLE investment_opportunities
    ADD COLUMN IF NOT EXISTS fund_id INT REFERENCES funds(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_loan_requests_fund ON loan_requests(fund_id) WHERE fund_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_opp_fund ON investment_opportunities(fund_id) WHERE fund_id IS NOT NULL;

-- ============================================================
-- SEED: restricted_items
-- ============================================================

INSERT INTO restricted_items (category, keywords, description, reason, created_by) VALUES
('weapons',
 ARRAY['arma','arma de fogo','pistola','revólver','espingarda','fuzil','munição','explosivo','granada'],
 'Firearms, ammunition and explosives',
 'Lei 10.826/2003 (Estatuto do Desarmamento)', 'system'),
('drugs',
 ARRAY['droga','cocaína','maconha','crack','heroína','lsd','mdma','ecstasy','anfetamina'],
 'Illicit drugs and narcotics',
 'Lei 11.343/2006', 'system'),
('illegal_content',
 ARRAY['conteúdo adulto infantil','csam','exploração sexual','pornografia infantil','tráfico humano'],
 'Illegal content and exploitation',
 'Lei 8.069/1990 (ECA) e Código Penal', 'system')
ON CONFLICT DO NOTHING;

-- ============================================================
-- SEED: risk_score_models (modelo v1 inicial)
-- ============================================================

INSERT INTO risk_score_models (version, description, active) VALUES
('v1.0', 'Scorecard ponderado inicial — comportamental + geográfico', TRUE)
ON CONFLICT DO NOTHING;

-- ============================================================
-- SEED: levels (1 a 10)
-- ============================================================

INSERT INTO levels (id, name, description) VALUES
(1,  'Nível 1 — Iniciante',        'Limite mínimo de crédito. Usuários sem histórico.'),
(2,  'Nível 2 — Básico',           'Pequeno histórico positivo. Risco elevado.'),
(3,  'Nível 3 — Em desenvolvimento','Histórico inicial construído. Risco moderado-alto.'),
(4,  'Nível 4 — Regular',          'Comportamento consistente. Risco moderado.'),
(5,  'Nível 5 — Intermediário',    'Bom histórico de pagamentos. Risco mediano.'),
(6,  'Nível 6 — Confiável',        'Histórico sólido. Risco baixo-moderado.'),
(7,  'Nível 7 — Avançado',         'Excelente histórico. Risco baixo.'),
(8,  'Nível 8 — Premium',          'Histórico excepcional. Risco muito baixo.'),
(9,  'Nível 9 — Elite',            'Top performers. Risco mínimo.'),
(10, 'Nível 10 — Máximo',          'Perfil máximo de confiança e capacidade.')
ON CONFLICT (id) DO UPDATE SET
    name        = EXCLUDED.name,
    description = EXCLUDED.description;

-- ============================================================
-- Fim do schema
-- ============================================================
