-- ============================================================
-- Migration 003 — Histórico de Mensagens de Conversa
-- Persiste o histórico de mensagens LLM por usuário no banco.
-- ============================================================

CREATE TABLE IF NOT EXISTS conversation_messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       VARCHAR(20) NOT NULL,
        -- user | assistant | system
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_role CHECK (role IN ('user', 'assistant', 'system'))
);

-- Índice principal: busca das últimas N mensagens de um usuário
CREATE INDEX IF NOT EXISTS idx_conv_messages_user_created
    ON conversation_messages(user_id, created_at DESC);

-- ============================================================
-- Fim da migration 003
-- ============================================================
