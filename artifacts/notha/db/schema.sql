-- ============================================================
-- NOTHA — PostgreSQL Schema (English Standard)
-- Apply via Supabase SQL Editor or psql
-- ============================================================

-- 1. Users (identity)
CREATE TABLE IF NOT EXISTS users (
    id               SERIAL PRIMARY KEY,
    tax_id           VARCHAR(14) UNIQUE,
    full_name        VARCHAR(200),
    nickname         VARCHAR(60),
    identity_status  VARCHAR(20) NOT NULL DEFAULT 'unverified',
        -- unverified | under_review | verified | rejected
    city             VARCHAR(100),
    neighborhood     VARCHAR(100),
    created_at       TIMESTAMP DEFAULT now(),
    updated_at       TIMESTAMP DEFAULT now(),
    CONSTRAINT chk_identity_status
        CHECK (identity_status IN ('unverified', 'under_review', 'verified', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_users_identity_status ON users(identity_status);

CREATE TABLE IF NOT EXISTS user_phone_numbers (
    id           SERIAL PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone        VARCHAR(20) UNIQUE NOT NULL,
    active       BOOLEAN DEFAULT TRUE,
    -- Parsed by phonenumbers (Google libphonenumber) on first contact
    country_code SMALLINT,                -- e.g. 55
    country_iso  VARCHAR(2),              -- ISO 3166-1 alpha-2, e.g. 'BR'
    country_name VARCHAR(100),            -- in Portuguese, e.g. 'Brasil'
    region       VARCHAR(150),            -- state/province, e.g. 'São Paulo'
    carrier      VARCHAR(100),            -- mobile operator, e.g. 'Vivo'
    timezone     VARCHAR(60),             -- IANA, e.g. 'America/Sao_Paulo'
    number_type  VARCHAR(30),             -- 'MOBILE', 'FIXED_LINE', etc.
    is_valid     BOOLEAN,
    parsed_at    TIMESTAMPTZ,
    created_at   TIMESTAMP DEFAULT now()
);

-- One active number per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_phone_per_user
    ON user_phone_numbers(user_id) WHERE active = TRUE;

-- Identity documents submitted for verification
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

-- 2. Role profiles
CREATE TABLE IF NOT EXISTS seller_profile (
    user_id         INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    pickup_address  TEXT,
    available_hours JSONB,
    pix_key         VARCHAR(150),
    pix_holder_name VARCHAR(200),
    created_at      TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS buyer_profile (
    user_id          INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    delivery_address TEXT,
    created_at       TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS courier_profile (
    user_id         INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    pix_key         VARCHAR(150),
    pix_holder_name VARCHAR(200),
    service_area    TEXT,
    created_at      TIMESTAMP DEFAULT now()
);

-- 3. Product listings
CREATE TABLE IF NOT EXISTS listings (
    id                   SERIAL PRIMARY KEY,
    seller_id            INT NOT NULL REFERENCES users(id),
    description          TEXT NOT NULL,
    category             VARCHAR(100),
    photos               JSONB,
    seller_asking_price  NUMERIC,
    suggested_price      NUMERIC,
    listed_price         NUMERIC NOT NULL DEFAULT 0,
    floor_price          NUMERIC NOT NULL DEFAULT 0,
    appraisal_data       JSONB,
    status               VARCHAR(30) NOT NULL DEFAULT 'available',
        -- available | in_negotiation | sold | cancelled
    created_at           TIMESTAMP DEFAULT now(),
    updated_at           TIMESTAMP DEFAULT now(),
    brand                VARCHAR(100),
    model                VARCHAR(200),
    version              VARCHAR(100),
    usage_state          VARCHAR(20),
        -- new | used
    condition            VARCHAR(50),
        -- like_new | good | fair | worn | defective
    has_receipt          BOOLEAN,
    info_photos          JSONB,
    seller_minimum_price NUMERIC,
    web_info             JSONB,
    seller_city          VARCHAR(100),
    seller_neighborhood  VARCHAR(100),
    vision_analysis      TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_status              ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_category            ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_seller              ON listings(seller_id);
CREATE INDEX IF NOT EXISTS idx_listings_seller_city         ON listings(seller_city);
CREATE INDEX IF NOT EXISTS idx_listings_seller_neighborhood ON listings(seller_neighborhood);

-- Product listing state machine
CREATE TABLE IF NOT EXISTS listing_flows (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone      VARCHAR(20) NOT NULL,
    step       VARCHAR(50) NOT NULL DEFAULT 'product',
        -- product | brand_model | usage_state | condition | receipt |
        -- photos_upload | address | price | processing | review_condition | confirm | done
    data       JSONB NOT NULL DEFAULT '{}',
    photos     JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one active flow per phone number
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_flows_phone_active
    ON listing_flows(phone) WHERE step != 'done';

CREATE INDEX IF NOT EXISTS idx_listing_flows_user ON listing_flows(user_id);

-- 4. Interest queue
CREATE TABLE IF NOT EXISTS interest_queue (
    id            SERIAL PRIMARY KEY,
    listing_id    INT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    buyer_id      INT NOT NULL REFERENCES users(id),
    initial_offer NUMERIC,
    created_at    TIMESTAMP DEFAULT now(),
    UNIQUE(listing_id, buyer_id)
);

CREATE INDEX IF NOT EXISTS idx_interest_queue_listing
    ON interest_queue(listing_id, created_at);

-- 5. Negotiations
CREATE TABLE IF NOT EXISTS negotiations (
    id              SERIAL PRIMARY KEY,
    listing_id      INT NOT NULL REFERENCES listings(id),
    buyer_id        INT NOT NULL REFERENCES users(id),
    mode            VARCHAR(20) NOT NULL DEFAULT 'proxy',
        -- proxy | direct
    status          VARCHAR(30) NOT NULL DEFAULT 'active',
        -- active | pending_seller | pending_buyer
        -- accepted | awaiting_payment | paid
        -- rejected | expired | timed_out | cancelled | no_deal
    current_price   NUMERIC,
    buyer_limits    JSONB,
    seller_limits   JSONB,
    responder_until TIMESTAMP,
    expires_at      TIMESTAMP,
    human_attempts  INT DEFAULT 0,
    created_at      TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_negotiations_status  ON negotiations(status);
CREATE INDEX IF NOT EXISTS idx_negotiations_listing ON negotiations(listing_id);
CREATE INDEX IF NOT EXISTS idx_negotiations_buyer   ON negotiations(buyer_id);
CREATE INDEX IF NOT EXISTS idx_negotiations_responder_until
    ON negotiations(responder_until) WHERE status = 'active';

-- 6. Direct negotiation offer history
CREATE TABLE IF NOT EXISTS negotiation_offers (
    id             SERIAL PRIMARY KEY,
    negotiation_id INT NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
    author         VARCHAR(20) NOT NULL,
        -- buyer | seller | system
    proposed_value NUMERIC NOT NULL,
    extra_context  TEXT,
    created_at     TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_offers_negotiation ON negotiation_offers(negotiation_id);

-- 7. Proxy round history
CREATE TABLE IF NOT EXISTS proxy_negotiation_rounds (
    id                  SERIAL PRIMARY KEY,
    negotiation_id      INT NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
    round_number        INT NOT NULL,
    proposed_value      NUMERIC NOT NULL,
    seller_argument     TEXT,
    buyer_argument      TEXT,
    confirmed_by_seller BOOLEAN DEFAULT NULL,
    confirmed_by_buyer  BOOLEAN DEFAULT NULL,
    created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proxy_rounds_negotiation ON proxy_negotiation_rounds(negotiation_id);

-- 8. Financial transactions
CREATE TABLE IF NOT EXISTS transactions (
    id                        SERIAL PRIMARY KEY,
    negotiation_id            INT NOT NULL REFERENCES negotiations(id),
    product_amount            NUMERIC NOT NULL,
    delivery_amount           NUMERIC NOT NULL DEFAULT 0,
    notha_fee                 NUMERIC NOT NULL DEFAULT 0,
    delivery_mode             VARCHAR(20) NOT NULL DEFAULT 'pickup',
        -- pickup | notha_delivery
    seller_pix_key            VARCHAR(150) NOT NULL,
    courier_pix_key           VARCHAR(150),
    courier_id                INT REFERENCES users(id),
    asaas_charge_id           VARCHAR(100),
    asaas_transfer_id_seller  VARCHAR(100),
    asaas_transfer_id_courier VARCHAR(100),
    status                    VARCHAR(30) NOT NULL DEFAULT 'pending',
        -- pending | charge_created | paid | failed
    retention_status          VARCHAR(50) NOT NULL DEFAULT 'held_pending_delivery',
        -- held_pending_delivery | released
        -- held_pending_decision | auto_refunded | manually_refunded
    auto_refund_deadline      TIMESTAMP,
    created_at                TIMESTAMP DEFAULT now(),
    updated_at                TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transactions_retention_status ON transactions(retention_status);
CREATE INDEX IF NOT EXISTS idx_transactions_negotiation      ON transactions(negotiation_id);

-- Financial reconciliation view
CREATE OR REPLACE VIEW retained_balance_total AS
SELECT COALESCE(SUM(product_amount + delivery_amount), 0) AS total_retained
FROM transactions
WHERE retention_status IN ('held_pending_delivery', 'held_pending_decision');

-- 9. Delivery / pickup confirmations
CREATE TABLE IF NOT EXISTS delivery_confirmations (
    id                    SERIAL PRIMARY KEY,
    negotiation_id        INT NOT NULL REFERENCES negotiations(id),
    delivery_mode         VARCHAR(20) NOT NULL,
        -- pickup | notha_delivery
    courier_id            INT REFERENCES users(id),
    scheduled_date        DATE,
    scheduled_time        VARCHAR(50),
    confirmation_deadline TIMESTAMP,
    confirmed_by_seller   BOOLEAN DEFAULT FALSE,
    confirmed_by_buyer    BOOLEAN DEFAULT FALSE,
    confirmed_at          TIMESTAMP,
    status                VARCHAR(30) NOT NULL DEFAULT 'scheduled',
        -- scheduled | confirmed | unconfirmed | converted | cancelled
    relisted_at           TIMESTAMP,
    created_at            TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_confirmations_deadline
    ON delivery_confirmations(confirmation_deadline) WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS idx_delivery_negotiation
    ON delivery_confirmations(negotiation_id);

-- 10. Agent state store (generic key-value)
CREATE TABLE IF NOT EXISTS agent_store (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 11. Restricted items (dynamic admin-managed list)
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

-- 12. LLM conversation history per user
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

-- 13. Saved searches / interest alerts
CREATE TABLE IF NOT EXISTS saved_searches (
    id                  SERIAL PRIMARY KEY,
    user_id             INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone               VARCHAR(20) NOT NULL,
    search_description  TEXT NOT NULL,
    category            VARCHAR(100),
    search_city         VARCHAR(100),
    search_neighborhood VARCHAR(100),
    status              VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | cancelled
    last_notified_at    TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_saved_searches_user   ON saved_searches(user_id);
CREATE INDEX IF NOT EXISTS idx_saved_searches_status ON saved_searches(status);
CREATE INDEX IF NOT EXISTS idx_saved_searches_phone  ON saved_searches(phone);

-- Seed: restricted item categories
INSERT INTO restricted_items (category, keywords, description, reason, created_by) VALUES
('weapons',
 ARRAY['arma','arma de fogo','pistola','revólver','espingarda','fuzil','munição','bala calibre','explosivo','granada','bomba'],
 'Firearms, ammunition and explosives',
 'Prohibited by federal law — Lei 10.826/2003 (Estatuto do Desarmamento)', 'system'),
('drugs',
 ARRAY['droga','cocaína','maconha','crack','heroína','lsd','mdma','ecstasy','anfetamina','metanfetamina','entorpecente','narcótico','psicotrópico'],
 'Illicit drugs and narcotics',
 'Drug trafficking — Lei 11.343/2006', 'system'),
('controlled_medications',
 ARRAY['remédio controlado','receituário azul','receituário amarelo','benzodiazepínico','opioide','morfina','codeína','ritalina','adderall'],
 'Prescription-only controlled medications',
 'Illegal sale without prescription — RDC Anvisa', 'system'),
('illegal_animals',
 ARRAY['animal silvestre','ave silvestre','papagaio silvestre','onça','tatu','capivara','cobra peçonhenta','tráfico de animais','espécie ameaçada'],
 'Wildlife and protected species',
 'Environmental crimes — Lei 9.605/1998', 'system'),
('counterfeit_goods',
 ARRAY['falsificado','pirata','réplica','imitação','fake','contrabandeado','produto adulterado'],
 'Counterfeit, pirated or smuggled goods',
 'Intellectual property law — Lei 9.279/1996', 'system'),
('false_documents',
 ARRAY['documento falso','identidade falsa','rg falso','cnh falsa','passaporte falso','cartão clonado','dado pessoal roubado','cpf de terceiro'],
 'Forged documents and stolen personal data',
 'Penal Code — falsidade ideológica e estelionato', 'system'),
('illegal_content',
 ARRAY['conteúdo adulto infantil','csam','exploração sexual','pornografia infantil','tráfico humano'],
 'Illegal content and exploitation',
 'Lei 8.069/1990 (ECA) and Penal Code', 'system'),
('human_organs',
 ARRAY['órgão humano','rim à venda','fígado à venda','sangue ilegal','plasma ilegal'],
 'Human organs and body parts',
 'Organ trafficking — Lei 9.434/1997', 'system')
ON CONFLICT DO NOTHING;

-- 14. User sessions
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

-- Only one active/pending_reauth session per phone at a time
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_phone_live
    ON sessions(phone)
    WHERE status IN ('active', 'pending_reauth');

CREATE INDEX IF NOT EXISTS idx_sessions_phone  ON sessions(phone);
CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

-- 15. Pending facial verifications (link tier re-auth)
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
    -- one active (pending) verification per phone enforced via partial index below
);

CREATE INDEX IF NOT EXISTS idx_pv_token  ON pending_verifications(token);
CREATE INDEX IF NOT EXISTS idx_pv_phone  ON pending_verifications(phone);
CREATE INDEX IF NOT EXISTS idx_pv_status ON pending_verifications(status);

-- ============================================================
-- End of schema
-- ============================================================
