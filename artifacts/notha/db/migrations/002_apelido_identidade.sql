-- ============================================================
-- Migration 002 — Apelido e Verificação de Identidade
-- Aplique via: psql $DATABASE_URL -f migrations/002_apelido_identidade.sql
-- ============================================================

-- 1. Adiciona apelido e status_identidade à tabela users
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS apelido VARCHAR(60),
    ADD COLUMN IF NOT EXISTS status_identidade VARCHAR(20) NOT NULL DEFAULT 'nao_verificado';
    -- status_identidade: nao_verificado | em_analise | verificado | rejeitado

-- Constraint para garantir valores válidos
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_status_identidade'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT chk_status_identidade
            CHECK (status_identidade IN ('nao_verificado', 'em_analise', 'verificado', 'rejeitado'));
    END IF;
END$$;

-- Índice para consultas por status de verificação
CREATE INDEX IF NOT EXISTS idx_users_status_identidade ON users(status_identidade);

-- 2. Tabela de documentos de identidade enviados pelo usuário
CREATE TABLE IF NOT EXISTS documentos_identidade (
    id                  SERIAL PRIMARY KEY,
    user_id             INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Tipo do documento enviado
    tipo                VARCHAR(30) NOT NULL DEFAULT 'desconhecido',
        -- rg | cnh | passaporte | desconhecido

    -- URL da imagem armazenada (Supabase Storage, S3, etc.)
    url_imagem          TEXT NOT NULL,

    -- Mensagem original do WhatsApp (id da mídia) para rastreabilidade
    whatsapp_media_id   TEXT,

    -- Status da análise
    status              VARCHAR(20) NOT NULL DEFAULT 'em_analise',
        -- em_analise | aprovado | rejeitado

    motivo_rejeicao     TEXT,

    -- Auditoria
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    analisado_em        TIMESTAMPTZ,
    analisado_por       TEXT
        -- 'auto' (análise automática futura) ou ID do admin
);

CREATE INDEX IF NOT EXISTS idx_documentos_identidade_user
    ON documentos_identidade(user_id);
CREATE INDEX IF NOT EXISTS idx_documentos_identidade_status
    ON documentos_identidade(status);

-- ============================================================
-- Fim da migration 002
-- ============================================================
