-- Migração 005: alertas de interesse em produtos (buscas salvas)
-- Aplique via Supabase SQL Editor ou psql

CREATE TABLE IF NOT EXISTS buscas_salvas (
    id               SERIAL PRIMARY KEY,
    user_id          INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone            VARCHAR(20) NOT NULL,
    descricao_busca  TEXT NOT NULL,
    categoria        VARCHAR(100),
    cidade_busca     VARCHAR(100),
    bairro_busca     VARCHAR(100),
    status           VARCHAR(20) NOT NULL DEFAULT 'ativa',
        -- ativa | cancelada
    ultima_notificacao TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_buscas_salvas_user    ON buscas_salvas(user_id);
CREATE INDEX IF NOT EXISTS idx_buscas_salvas_status  ON buscas_salvas(status);
CREATE INDEX IF NOT EXISTS idx_buscas_salvas_phone   ON buscas_salvas(phone);
