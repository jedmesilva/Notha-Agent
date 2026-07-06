-- ============================================================
-- NOTHA — PostgreSQL Schema v2
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
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_identity_status
        CHECK (identity_status IN ('unverified', 'under_review', 'verified', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_users_identity_status ON users(identity_status);

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

-- 2.1 Pluggy connections — link entre usuário e conta bancária via Open Finance
CREATE TABLE IF NOT EXISTS pluggy_connections (
    id                   SERIAL PRIMARY KEY,
    user_id              INT REFERENCES users(id) ON DELETE SET NULL,
    phone                VARCHAR(20) NOT NULL,
    token                TEXT NOT NULL UNIQUE,  -- internal token (link sent via WhatsApp)
    pluggy_item_id       TEXT,                  -- Pluggy item ID after successful connection
    pluggy_connect_token TEXT,                  -- Pluggy connect token (short-lived)
    status               VARCHAR(30) NOT NULL DEFAULT 'pending',
        -- pending | connected | error | expired
    connectors           JSONB,                 -- list of connected institution connectors
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ NOT NULL,
    completed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pluggy_connections_user   ON pluggy_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_pluggy_connections_phone  ON pluggy_connections(phone);
CREATE INDEX IF NOT EXISTS idx_pluggy_connections_status ON pluggy_connections(status);
CREATE INDEX IF NOT EXISTS idx_pluggy_connections_item   ON pluggy_connections(pluggy_item_id);

-- 2.2 Bank accounts — dados de contas bancárias obtidos via Pluggy
CREATE TABLE IF NOT EXISTS bank_accounts (
    id                   SERIAL PRIMARY KEY,
    user_id              INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pluggy_connection_id INT NOT NULL REFERENCES pluggy_connections(id) ON DELETE CASCADE,
    pluggy_account_id    TEXT NOT NULL UNIQUE,  -- Pluggy account ID
    institution_name     VARCHAR(200),
    institution_number   VARCHAR(10),           -- ISPB ou código COMPE
    account_type         VARCHAR(30),
        -- checking | savings | credit | investment | other
    account_number       VARCHAR(30),
    branch_number        VARCHAR(10),
    owner_name           TEXT,
    tax_number           VARCHAR(14),           -- CPF/CNPJ do titular
    balance              NUMERIC(15,2),
    currency_code        VARCHAR(3) DEFAULT 'BRL',
    pix_key              TEXT,                  -- chave Pix, se disponível
    raw_data             JSONB,                 -- snapshot bruto da API Pluggy
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bank_accounts_user       ON bank_accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_bank_accounts_connection ON bank_accounts(pluggy_connection_id);

-- 2.3 Bank transactions cache — extrato bancário importado via Pluggy (para scoring)
CREATE TABLE IF NOT EXISTS bank_transactions_cache (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bank_account_id      INT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    pluggy_tx_id         TEXT NOT NULL UNIQUE,
    amount               NUMERIC(15,2) NOT NULL,  -- positivo = crédito, negativo = débito
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
-- MÓDULO 3 — GRUPOS E ALOCAÇÃO DE USUÁRIOS
-- ============================================================

CREATE TABLE IF NOT EXISTS groups (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | inactive
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Junction com histórico: left_at = NULL significa membro atual
CREATE TABLE IF NOT EXISTS user_groups (
    id                SERIAL PRIMARY KEY,
    user_id           INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id          INT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    joined_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    left_at           TIMESTAMPTZ,  -- NULL = alocação ativa
    allocation_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_groups_user    ON user_groups(user_id);
CREATE INDEX IF NOT EXISTS idx_user_groups_group   ON user_groups(group_id);
CREATE INDEX IF NOT EXISTS idx_user_groups_active  ON user_groups(user_id, group_id) WHERE left_at IS NULL;

-- ============================================================
-- MÓDULO 4 — WALLETS (CARTEIRAS)
-- ============================================================

-- Polimórfico: owner_type = 'user' | 'group' | 'platform'
CREATE TABLE IF NOT EXISTS wallets (
    id            SERIAL PRIMARY KEY,
    owner_type    VARCHAR(20) NOT NULL,
        -- user | group | platform
    owner_id      INT NOT NULL,
    balance_cache NUMERIC(15,2) NOT NULL DEFAULT 0,
        -- cache — fonte de verdade é SUM(wallet_transactions.amount)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_wallet_owner_type CHECK (owner_type IN ('user', 'group', 'platform')),
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
    factors_json  JSONB NOT NULL DEFAULT '{}',  -- snapshot de cada input usado
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_risk_scores_user       ON user_risk_scores(user_id);
CREATE INDEX IF NOT EXISTS idx_risk_scores_calculated ON user_risk_scores(user_id, calculated_at DESC);

-- ============================================================
-- MÓDULO 6 — POLÍTICAS E TAXAS DE JUROS
-- ============================================================

-- 6.1 Política base por grupo (muda raramente)
CREATE TABLE IF NOT EXISTS group_rate_policies (
    id                        SERIAL PRIMARY KEY,
    group_id                  INT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    base_borrowing_rate       NUMERIC(8,6) NOT NULL,  -- taxa base cobrada de tomadores
    base_investment_rate      NUMERIC(8,6) NOT NULL,  -- taxa base paga a investidores
    min_spread                NUMERIC(8,6) NOT NULL,  -- margem mínima obrigatória
    spread_violation_strategy VARCHAR(30) NOT NULL DEFAULT 'reject_investment',
        -- reject_investment | raise_borrowing_rate
    effective_from            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_group_rate_policies_group ON group_rate_policies(group_id);

-- 6.2 Curva de ajuste por prazo
CREATE TABLE IF NOT EXISTS term_rate_curve (
    id             SERIAL PRIMARY KEY,
    group_id       INT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    min_term_days  INT NOT NULL,
    max_term_days  INT NOT NULL,
    adjustment_bps INT NOT NULL DEFAULT 0  -- basis points (100 bps = 1%)
);

CREATE INDEX IF NOT EXISTS idx_term_rate_curve_group ON term_rate_curve(group_id);

-- 6.3 Snapshot de liquidez (atualizado por job periódico a cada 5–15 min)
CREATE TABLE IF NOT EXISTS liquidity_snapshots (
    id                           SERIAL PRIMARY KEY,
    group_id                     INT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    total_available_investment   NUMERIC(15,2) NOT NULL DEFAULT 0,
    total_active_loan_demand     NUMERIC(15,2) NOT NULL DEFAULT 0,
    captured_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_liquidity_snapshots_group ON liquidity_snapshots(group_id);

-- ============================================================
-- MÓDULO 7 — LIMITES DE CRÉDITO
-- ============================================================

-- Tipo A: limite individual (borrower → group)
CREATE TABLE IF NOT EXISTS credit_limits (
    id               SERIAL PRIMARY KEY,
    borrower_type    VARCHAR(20) NOT NULL,
        -- user | group
    borrower_id      INT NOT NULL,
    lender_group_id  INT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    limit_amount     NUMERIC(15,2) NOT NULL,
    effective_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_credit_limit_borrower_type CHECK (borrower_type IN ('user', 'group'))
);

CREATE INDEX IF NOT EXISTS idx_credit_limits_borrower ON credit_limits(borrower_type, borrower_id);
CREATE INDEX IF NOT EXISTS idx_credit_limits_group    ON credit_limits(lender_group_id);

-- Tipo B: exposição agregada do grupo credor
CREATE TABLE IF NOT EXISTS group_pool_limits (
    id                       SERIAL PRIMARY KEY,
    group_id                 INT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    max_aggregate_exposure   NUMERIC(15,2) NOT NULL,
    current_exposure_cache   NUMERIC(15,2) NOT NULL DEFAULT 0,
        -- cache — reconciliável via SUM(debts.principal) ativas
    effective_from           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_group_pool_limits_group ON group_pool_limits(group_id);

-- ============================================================
-- MÓDULO 8 — SOLICITAÇÃO E PROPOSTA DE EMPRÉSTIMO
-- ============================================================

CREATE TABLE IF NOT EXISTS loan_requests (
    id               SERIAL PRIMARY KEY,
    user_id          INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id         INT NOT NULL REFERENCES groups(id),
    requested_amount NUMERIC(15,2) NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | approved | rejected | expired
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at       TIMESTAMPTZ,
    decided_by       TEXT,
    rejection_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_loan_requests_user   ON loan_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_loan_requests_group  ON loan_requests(group_id);
CREATE INDEX IF NOT EXISTS idx_loan_requests_status ON loan_requests(status);

-- Plano proposto pelo usuário (separado de debt_installments — se rejeitado, não polui a realidade)
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
    investment_id        INT,  -- FK adicionada quando tabela investments for criada
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
    from_wallet_id          INT NOT NULL REFERENCES wallets(id),  -- carteira credora
    to_wallet_id            INT NOT NULL REFERENCES wallets(id),  -- carteira devedora
    principal               NUMERIC(15,2) NOT NULL,
    interest_rate_applied   NUMERIC(8,6) NOT NULL,
        -- SNAPSHOT congelado da cotação final — imutável após concessão
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
    remaining_amount NUMERIC(15,2) NOT NULL,  -- amount_due - soma das alocações recebidas
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | partially_paid | paid | overdue
    CONSTRAINT chk_installment_status
        CHECK (status IN ('pending', 'partially_paid', 'paid', 'overdue'))
);

CREATE INDEX IF NOT EXISTS idx_debt_installments_debt   ON debt_installments(debt_id);
CREATE INDEX IF NOT EXISTS idx_debt_installments_status ON debt_installments(status);
CREATE INDEX IF NOT EXISTS idx_debt_installments_due    ON debt_installments(due_date) WHERE status != 'paid';

CREATE TABLE IF NOT EXISTS payments (
    id             SERIAL PRIMARY KEY,
    debt_id        INT NOT NULL REFERENCES debts(id) ON DELETE CASCADE,
    paid_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    amount_paid    NUMERIC(15,2) NOT NULL,
    payment_method VARCHAR(30) NOT NULL DEFAULT 'pix',
    asaas_charge_id TEXT,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_payments_debt ON payments(debt_id);

-- Resolve pagamentos parciais, totais e antecipados
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

-- 11.4 Key-value store genérico para o agente
CREATE TABLE IF NOT EXISTS agent_store (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
    lr.group_id
FROM debt_installments di
JOIN debts d ON d.id = di.debt_id
JOIN loan_requests lr ON lr.id = d.loan_request_id
WHERE di.status IN ('pending', 'partially_paid')
  AND di.due_date < CURRENT_DATE;

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
-- Fim do schema
-- ============================================================
