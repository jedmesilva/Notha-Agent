-- Migração 004: localização do usuário para busca por região
-- Aplique via Supabase SQL Editor ou psql

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS cidade VARCHAR(100),
    ADD COLUMN IF NOT EXISTS bairro VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_users_cidade ON users(cidade);

-- Também adiciona bairro_vendedor e bairro_comprador aos listings para buscas granulares
ALTER TABLE listings
    ADD COLUMN IF NOT EXISTS bairro_vendedor VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_listings_cidade_vendedor ON listings(cidade_vendedor);
CREATE INDEX IF NOT EXISTS idx_listings_bairro_vendedor ON listings(bairro_vendedor);
