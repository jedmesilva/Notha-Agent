-- ============================================================
-- NOTHA — P2P Architecture Migration
-- SEP (Sociedade de Empréstimo entre Pessoas) compliance
--
-- Golden rule: no credit instrument exists before real creditor
-- capital is committed. Platform is a pure intermediary.
--
-- Apply via Supabase SQL Editor after schema.sql
-- ============================================================

-- ============================================================
-- Extend wallet_transactions allowed types for P2P operations
-- ============================================================

ALTER TABLE wallet_transactions
    DROP CONSTRAINT IF EXISTS chk_wallet_tx_type;

ALTER TABLE wallet_transactions
    ADD CONSTRAINT chk_wallet_tx_type CHECK (type IN (
        -- legacy pool-based types (preserved for backward compatibility)
        'loan_disbursement', 'loan_repayment', 'investment_deposit',
        'investment_withdrawal', 'interest_payout', 'fee', 'adjustment',
        -- P2P types
        'p2p_creditor_reserve',      -- creditor commits funds (reserved → escrow)
        'p2p_creditor_reserve_revert', -- position reverted (escrow → creditor)
        'p2p_capture_disbursement',  -- funds released to debtor after capture
        'p2p_origination_fee',       -- origination fee charged to debtor
        'p2p_installment_passthrough', -- debtor payment distributed to creditor
        'p2p_servicing_fee',         -- servicing fee charged to creditor
        'p2p_assignment_settlement'  -- secondary market position transfer
    ));

-- ============================================================
-- MODULE P2P-1 — CAPTURE REQUESTS (borrower intent, no capital)
-- ============================================================
--
-- Entry point for the P2P flow. Represents the borrower's request
-- before any creditor capital is committed. No debt exists yet.
--
-- Status machine:
--   draft → in_capture → captured | partial_expired | cancelled

CREATE TABLE IF NOT EXISTS capture_requests (
    id                      BIGSERIAL    PRIMARY KEY,
    user_id                 INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level_id                SMALLINT     NOT NULL REFERENCES levels(id),
    requested_amount        NUMERIC(15,2) NOT NULL,
    term_days               INT          NOT NULL,
    -- payment_plan: [{sequence, due_date, amount_due}]
    payment_plan            JSONB        NOT NULL DEFAULT '[]',
    -- proposed_rate: borrower-requested rate (may differ from final approved rate)
    proposed_rate           NUMERIC(8,6),
    -- snapshot of credit score at request creation
    credit_score_at_request NUMERIC(7,4),
    -- rejection reason if the request cannot proceed to capture
    rejection_reason        TEXT,
    status                  VARCHAR(30)  NOT NULL DEFAULT 'draft',
        -- draft | in_capture | captured | partial_expired | cancelled
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_capture_req_status CHECK (
        status IN ('draft', 'in_capture', 'captured', 'partial_expired', 'cancelled')
    ),
    CONSTRAINT chk_capture_req_amount  CHECK (requested_amount > 0),
    CONSTRAINT chk_capture_req_term    CHECK (term_days > 0)
);

CREATE INDEX IF NOT EXISTS idx_capture_requests_user   ON capture_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_capture_requests_level  ON capture_requests(level_id);
CREATE INDEX IF NOT EXISTS idx_capture_requests_status ON capture_requests(status);

-- ============================================================
-- MODULE P2P-2 — CAPTURE ORDERS (offer distributed to creditors)
-- ============================================================
--
-- One CaptureOrder per active CaptureRequest.
-- Tracks how much creditor capital has been committed.
--
-- Status machine:
--   open → complete | partial_expired | cancelled

CREATE TABLE IF NOT EXISTS capture_orders (
    id                  BIGSERIAL    PRIMARY KEY,
    capture_request_id  BIGINT       NOT NULL REFERENCES capture_requests(id),
    target_amount       NUMERIC(15,2) NOT NULL,
    -- dynamic sum of CONFIRMED creditor positions only (not reserved)
    committed_amount    NUMERIC(15,2) NOT NULL DEFAULT 0,
    -- minimum threshold to consider capture viable (e.g. 80% of target)
    minimum_threshold   NUMERIC(15,2) NOT NULL,
    -- approved interest rate for this capture (locked at order creation)
    approved_rate       NUMERIC(8,6)  NOT NULL,
    -- rate to be paid to creditors (= approved_rate minus platform spread)
    creditor_rate       NUMERIC(8,6)  NOT NULL,
    -- tarifa de originação % charged to debtor at disbursement
    origination_fee_pct NUMERIC(8,6)  NOT NULL DEFAULT 0,
    -- tarifa de servicing % charged to creditor per installment passthrough
    servicing_fee_pct   NUMERIC(8,6)  NOT NULL DEFAULT 0,
    -- capture window: if not completed by this time, order expires
    capture_deadline    TIMESTAMPTZ   NOT NULL,
    status              VARCHAR(30)   NOT NULL DEFAULT 'open',
        -- open | complete | partial_expired | cancelled
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_capture_order_status CHECK (
        status IN ('open', 'complete', 'partial_expired', 'cancelled')
    ),
    CONSTRAINT chk_capture_order_threshold CHECK (minimum_threshold <= target_amount),
    CONSTRAINT chk_capture_order_committed CHECK (committed_amount >= 0)
);

CREATE INDEX IF NOT EXISTS idx_capture_orders_request ON capture_orders(capture_request_id);
CREATE INDEX IF NOT EXISTS idx_capture_orders_status  ON capture_orders(status);
CREATE INDEX IF NOT EXISTS idx_capture_orders_deadline ON capture_orders(capture_deadline)
    WHERE status = 'open';

-- ============================================================
-- MODULE P2P-3 — CREDITOR POSITIONS (each creditor's commitment)
-- ============================================================
--
-- Created when a creditor commits funds to a CaptureOrder.
-- reserved  = intent expressed, funds debited from creditor wallet to escrow
-- confirmed = funds confirmed (Pix received) — counts toward committed_amount
-- reverted  = order did not close; funds returned to creditor
--
-- IMPORTANT: only 'confirmed' positions count toward closing the order.

CREATE TABLE IF NOT EXISTS creditor_positions (
    id               BIGSERIAL    PRIMARY KEY,
    capture_order_id BIGINT       NOT NULL REFERENCES capture_orders(id),
    creditor_user_id INT          NOT NULL REFERENCES users(id),
    committed_amount NUMERIC(15,2) NOT NULL,
    origin           VARCHAR(30)   NOT NULL DEFAULT 'manual',
        -- manual | auto_mandate
    status           VARCHAR(20)   NOT NULL DEFAULT 'reserved',
        -- reserved | confirmed | reverted
    -- participation fraction (set when credit instrument is emitted)
    participation_fraction NUMERIC(10,8),
    reserved_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    confirmed_at     TIMESTAMPTZ,
    reverted_at      TIMESTAMPTZ,
    CONSTRAINT chk_creditor_pos_status CHECK (
        status IN ('reserved', 'confirmed', 'reverted')
    ),
    CONSTRAINT chk_creditor_pos_origin CHECK (
        origin IN ('manual', 'auto_mandate')
    ),
    CONSTRAINT chk_creditor_pos_amount CHECK (committed_amount > 0)
);

CREATE INDEX IF NOT EXISTS idx_creditor_positions_order    ON creditor_positions(capture_order_id);
CREATE INDEX IF NOT EXISTS idx_creditor_positions_creditor ON creditor_positions(creditor_user_id);
CREATE INDEX IF NOT EXISTS idx_creditor_positions_status   ON creditor_positions(status);

-- prevent duplicate active positions per creditor per order
CREATE UNIQUE INDEX IF NOT EXISTS idx_creditor_positions_unique_active
    ON creditor_positions(capture_order_id, creditor_user_id)
    WHERE status IN ('reserved', 'confirmed');

-- ============================================================
-- MODULE P2P-4 — CREDIT INSTRUMENTS (the actual debt contract)
-- ============================================================
--
-- Created ONLY when CaptureOrder.status = 'complete'.
-- This is the "contract" the user sees (UX: "emitir dívida").
-- Legally: instrumento representativo do crédito emitido pela SEP.
--
-- creditors[] is stored via CreditorPosition references.
-- allows_assignment must be TRUE from creation to enable secondary market.

CREATE TABLE IF NOT EXISTS credit_instruments (
    id                  BIGSERIAL    PRIMARY KEY,
    capture_order_id    BIGINT       NOT NULL UNIQUE REFERENCES capture_orders(id),
    debtor_id           INT          NOT NULL REFERENCES users(id),
    total_amount        NUMERIC(15,2) NOT NULL,  -- gross amount (base for interest)
    interest_rate       NUMERIC(8,6)  NOT NULL,  -- approved rate (debtor pays this)
    creditor_rate       NUMERIC(8,6)  NOT NULL,  -- rate paid to creditors
    term_days           INT           NOT NULL,
    -- payment_plan_final: [{sequence, due_date, amount_due}] (locked at emission)
    payment_plan_final  JSONB         NOT NULL DEFAULT '[]',
    -- tarifa de originação: charged to debtor at disbursement (separate line)
    origination_fee     NUMERIC(15,2) NOT NULL DEFAULT 0,
    origination_fee_pct NUMERIC(8,6)  NOT NULL DEFAULT 0,
    -- tarifa de servicing: % charged to creditor per installment passthrough
    servicing_fee_pct   NUMERIC(8,6)  NOT NULL DEFAULT 0,
    -- net amount disbursed to debtor (= total_amount - origination_fee)
    net_disbursed_amount NUMERIC(15,2) NOT NULL,
    allows_assignment   BOOLEAN       NOT NULL DEFAULT TRUE,
    issue_date          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    status              VARCHAR(20)   NOT NULL DEFAULT 'active',
        -- active | paid_off | defaulted | renegotiated
    CONSTRAINT chk_credit_instr_status CHECK (
        status IN ('active', 'paid_off', 'defaulted', 'renegotiated')
    ),
    CONSTRAINT chk_credit_instr_amount CHECK (total_amount > 0),
    CONSTRAINT chk_credit_instr_net    CHECK (net_disbursed_amount >= 0)
);

CREATE INDEX IF NOT EXISTS idx_credit_instruments_debtor ON credit_instruments(debtor_id);
CREATE INDEX IF NOT EXISTS idx_credit_instruments_status ON credit_instruments(status);
CREATE INDEX IF NOT EXISTS idx_credit_instruments_order  ON credit_instruments(capture_order_id);

-- ============================================================
-- MODULE P2P-4b — CREDIT INSTRUMENT INSTALLMENTS
-- ============================================================

CREATE TABLE IF NOT EXISTS credit_instrument_installments (
    id                   BIGSERIAL    PRIMARY KEY,
    credit_instrument_id BIGINT       NOT NULL
        REFERENCES credit_instruments(id) ON DELETE CASCADE,
    sequence             INT          NOT NULL,
    due_date             DATE         NOT NULL,
    amount_due           NUMERIC(15,2) NOT NULL,
    remaining_amount     NUMERIC(15,2) NOT NULL,
    status               VARCHAR(20)   NOT NULL DEFAULT 'pending',
        -- pending | partially_paid | paid | overdue
    CONSTRAINT chk_cii_status CHECK (
        status IN ('pending', 'partially_paid', 'paid', 'overdue')
    ),
    CONSTRAINT chk_cii_amount CHECK (amount_due > 0)
);

CREATE INDEX IF NOT EXISTS idx_cii_instrument ON credit_instrument_installments(credit_instrument_id);
CREATE INDEX IF NOT EXISTS idx_cii_status     ON credit_instrument_installments(status);
CREATE INDEX IF NOT EXISTS idx_cii_due        ON credit_instrument_installments(due_date)
    WHERE status != 'paid';

-- ============================================================
-- MODULE P2P-5 — ASSIGNMENT TRANSACTIONS (secondary market)
-- ============================================================
--
-- Creditor sells their position fraction to another eligible creditor.
-- Pricing MUST go through the pricing engine (never free-text price).
-- Debtor notification is mandatory (Art. 290 CC) before settlement.
--
-- Status machine: proposed → accepted → settled | refused

CREATE TABLE IF NOT EXISTS assignment_transactions (
    id                       BIGSERIAL    PRIMARY KEY,
    credit_instrument_id     BIGINT       NOT NULL REFERENCES credit_instruments(id),
    seller_position_id       BIGINT       NOT NULL REFERENCES creditor_positions(id),
    buyer_user_id            INT          NOT NULL REFERENCES users(id),
    -- negotiated_price must be engine-validated — no free-text prices
    negotiated_price         NUMERIC(15,2) NOT NULL,
    -- pricing_breakdown: {debtor_score, days_late, remaining_term_days,
    --                      market_reference_rate, discount_factor, final_price}
    pricing_breakdown        JSONB         NOT NULL DEFAULT '{}',
    -- debtor_notification_date: mandatory per Art. 290 CC
    debtor_notification_date TIMESTAMPTZ,
    status                   VARCHAR(20)   NOT NULL DEFAULT 'proposed',
        -- proposed | accepted | settled | refused
    proposed_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    accepted_at              TIMESTAMPTZ,
    settled_at               TIMESTAMPTZ,
    refused_at               TIMESTAMPTZ,
    CONSTRAINT chk_assign_tx_status CHECK (
        status IN ('proposed', 'accepted', 'settled', 'refused')
    ),
    CONSTRAINT chk_assign_tx_price CHECK (negotiated_price > 0)
);

CREATE INDEX IF NOT EXISTS idx_assign_tx_instrument ON assignment_transactions(credit_instrument_id);
CREATE INDEX IF NOT EXISTS idx_assign_tx_seller     ON assignment_transactions(seller_position_id);
CREATE INDEX IF NOT EXISTS idx_assign_tx_buyer      ON assignment_transactions(buyer_user_id);
CREATE INDEX IF NOT EXISTS idx_assign_tx_status     ON assignment_transactions(status);

-- ============================================================
-- MODULE P2P-6 — INSTALLMENT PASSTHROUGHS (payment distribution)
-- ============================================================
--
-- Records each debtor payment being distributed to current creditors.
-- Creditors at time of payment (not original creditors if assignment occurred).
-- Platform fee is always a separate, traceable line — never buried in yield.
--
-- distribution: [{creditor_user_id, position_id, gross_amount,
--                  servicing_fee, net_amount}]

CREATE TABLE IF NOT EXISTS installment_passthroughs (
    id                   BIGSERIAL    PRIMARY KEY,
    credit_instrument_id BIGINT       NOT NULL REFERENCES credit_instruments(id),
    installment_id       BIGINT       NOT NULL
        REFERENCES credit_instrument_installments(id),
    installment_number   INT          NOT NULL,
    total_amount_received NUMERIC(15,2) NOT NULL,
    -- origination_fee already deducted at emission; only servicing applies here
    total_servicing_fee  NUMERIC(15,2) NOT NULL DEFAULT 0,
    -- net distributed to all creditors combined
    total_net_distributed NUMERIC(15,2) NOT NULL DEFAULT 0,
    -- distribution JSON: individual creditor breakdown
    distribution         JSONB         NOT NULL DEFAULT '[]',
    processed_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_passthrough_amount CHECK (total_amount_received > 0)
);

CREATE INDEX IF NOT EXISTS idx_passthrough_instrument  ON installment_passthroughs(credit_instrument_id);
CREATE INDEX IF NOT EXISTS idx_passthrough_installment ON installment_passthroughs(installment_id);

-- ============================================================
-- Compliance views
-- ============================================================

-- Active P2P capture orders with coverage progress
CREATE OR REPLACE VIEW p2p_capture_order_coverage AS
SELECT
    co.id                                                     AS order_id,
    cr.id                                                     AS request_id,
    cr.user_id                                                AS debtor_id,
    co.target_amount,
    co.committed_amount,
    co.minimum_threshold,
    ROUND(co.committed_amount / NULLIF(co.target_amount,0) * 100, 2) AS coverage_pct,
    co.capture_deadline,
    co.status,
    co.approved_rate,
    co.creditor_rate,
    co.origination_fee_pct,
    co.servicing_fee_pct
FROM capture_orders co
JOIN capture_requests cr ON cr.id = co.capture_request_id;

-- Active creditor positions with instrument linkage
CREATE OR REPLACE VIEW p2p_creditor_portfolio AS
SELECT
    cp.id                           AS position_id,
    cp.creditor_user_id,
    cp.capture_order_id,
    cp.committed_amount,
    cp.participation_fraction,
    cp.status                       AS position_status,
    ci.id                           AS instrument_id,
    ci.debtor_id,
    ci.total_amount,
    ci.status                       AS instrument_status,
    ci.interest_rate,
    ci.creditor_rate,
    ci.issue_date,
    ci.allows_assignment
FROM creditor_positions cp
LEFT JOIN credit_instruments ci ON ci.capture_order_id = cp.capture_order_id
WHERE cp.status IN ('confirmed', 'reserved');

-- ============================================================
-- End of P2P migration
-- ============================================================
