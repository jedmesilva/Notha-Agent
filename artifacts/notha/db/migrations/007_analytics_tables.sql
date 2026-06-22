-- ============================================================
-- Migration 007 — Analytics & Observability Tables
-- ============================================================

-- 1. Product searches — every search executed by the agent
CREATE TABLE IF NOT EXISTS product_searches (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             INT NOT NULL REFERENCES users(id),
    phone               VARCHAR(20) NOT NULL,
    query               TEXT NOT NULL,
    category            VARCHAR(100),
    search_city         VARCHAR(100),
    search_neighborhood VARCHAR(100),
    results_count       INT NOT NULL DEFAULT 0,
    results_listing_ids JSONB DEFAULT '[]',
    had_fallback        BOOLEAN NOT NULL DEFAULT FALSE,
    fallback_level      VARCHAR(20),
        -- 'neighborhood' | 'city' | 'national' | null (no fallback needed)
    objective           TEXT,
    intent              VARCHAR(50),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_product_searches_user    ON product_searches(user_id);
CREATE INDEX IF NOT EXISTS idx_product_searches_created ON product_searches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_searches_city    ON product_searches(search_city);

-- 2. Tool execution logs — every tool call with result summary
CREATE TABLE IF NOT EXISTS tool_execution_logs (
    id             BIGSERIAL PRIMARY KEY,
    user_id        INT REFERENCES users(id),
    phone          VARCHAR(20) NOT NULL,
    tool_name      VARCHAR(100) NOT NULL,
    args           JSONB,
    result_summary TEXT,
    success        BOOLEAN NOT NULL DEFAULT TRUE,
    error_message  TEXT,
    duration_ms    INT,
    step_number    INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_logs_user    ON tool_execution_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_tool_logs_tool    ON tool_execution_logs(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_logs_created ON tool_execution_logs(created_at DESC);

-- 3. Restriction checks — every check_restriction call and its outcome
CREATE TABLE IF NOT EXISTS restriction_checks (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              INT REFERENCES users(id),
    phone                VARCHAR(20) NOT NULL,
    product_description  TEXT NOT NULL,
    result               VARCHAR(20) NOT NULL,
        -- 'ALLOWED' | 'RESTRICTED' | 'ERROR' | 'DB_UNAVAILABLE'
    restriction_category VARCHAR(100),
    restriction_reason   TEXT,
    state                VARCHAR(10),
    municipality         VARCHAR(100),
    intent               VARCHAR(50),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_restriction_checks_user    ON restriction_checks(user_id);
CREATE INDEX IF NOT EXISTS idx_restriction_checks_result  ON restriction_checks(result);
CREATE INDEX IF NOT EXISTS idx_restriction_checks_created ON restriction_checks(created_at DESC);

-- 4. Guardrail events — when the guardrail rejects or corrects a response
CREATE TABLE IF NOT EXISTS guardrail_events (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INT REFERENCES users(id),
    phone         VARCHAR(20),
    category      VARCHAR(50),
        -- incoherence | out_of_scope | data_leak | forbidden_term | nonsense
    reason        TEXT,
    was_corrected BOOLEAN NOT NULL DEFAULT FALSE,
    used_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    objective     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_guardrail_events_category ON guardrail_events(category);
CREATE INDEX IF NOT EXISTS idx_guardrail_events_created  ON guardrail_events(created_at DESC);

-- 5. Pipeline events — one row per user message processed by the 4-phase pipeline
CREATE TABLE IF NOT EXISTS pipeline_events (
    id             BIGSERIAL PRIMARY KEY,
    user_id        INT REFERENCES users(id),
    phone          VARCHAR(20) NOT NULL,
    objective      TEXT,
    intent         VARCHAR(50),
    flow           VARCHAR(50),
    needs_tools    BOOLEAN,
    steps_planned  INT NOT NULL DEFAULT 0,
    steps_executed INT NOT NULL DEFAULT 0,
    outcome        VARCHAR(20),
        -- 'done' | 'abort' | 'no_tools' | 'error'
    duration_ms    INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_user    ON pipeline_events(user_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_intent  ON pipeline_events(intent);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_created ON pipeline_events(created_at DESC);
