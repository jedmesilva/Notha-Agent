-- ============================================================
-- MIGRATION: Fluxo de cadastro de produto (listing_flow)
-- Aplique via Supabase SQL Editor
-- ============================================================

-- 1. Nova tabela para persistir o estado do fluxo de cadastro
CREATE TABLE IF NOT EXISTS listing_flows (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone       VARCHAR(20) NOT NULL,
    step        VARCHAR(50) NOT NULL DEFAULT 'produto',
    dados       JSONB NOT NULL DEFAULT '{}',
    fotos       JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Apenas um fluxo ativo por telefone
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_flows_phone_active
    ON listing_flows(phone)
    WHERE step != 'concluido';

CREATE INDEX IF NOT EXISTS idx_listing_flows_user
    ON listing_flows(user_id);

-- 2. Novos campos na tabela listings
ALTER TABLE listings ADD COLUMN IF NOT EXISTS marca                VARCHAR(100);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS modelo               VARCHAR(200);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS versao               VARCHAR(100);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS estado_uso           VARCHAR(20);
    -- novo | usado
ALTER TABLE listings ADD COLUMN IF NOT EXISTS condicao             VARCHAR(50);
    -- como_novo | bom | conservado | desgastado | com_defeito
ALTER TABLE listings ADD COLUMN IF NOT EXISTS tem_nota_fiscal      BOOLEAN;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS fotos_info           JSONB;
    -- fotos de etiquetas, embalagem, nota fiscal
ALTER TABLE listings ADD COLUMN IF NOT EXISTS preco_minimo_vendedor NUMERIC;
    -- piso definido pelo vendedor (diferente do preco_minimo do agente)
ALTER TABLE listings ADD COLUMN IF NOT EXISTS info_web             JSONB;
    -- resultado da busca web (preços, specs)
ALTER TABLE listings ADD COLUMN IF NOT EXISTS cidade_vendedor      VARCHAR(100);
    -- para contextualizar precificação por região
ALTER TABLE listings ADD COLUMN IF NOT EXISTS vision_analysis      TEXT;
    -- resultado da análise visual pelo GPT-4o

-- ============================================================
-- Fim da migration
-- ============================================================
