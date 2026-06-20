-- ============================================================
-- NOTHA — Schema PostgreSQL (Supabase)
-- Versão 1.0 — Aplique via Supabase SQL Editor ou psql
-- ============================================================

-- Extensão para UUIDs (não usada nas PKs por ora, mas disponível)
-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- -----------------------------------------------------------
-- 1. Identidade de usuários
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id                 SERIAL PRIMARY KEY,
    cpf                VARCHAR(14) UNIQUE,
    nome               VARCHAR(200),
    apelido            VARCHAR(60),
        -- Como o usuário quer ser chamado — editável a qualquer momento
    status_identidade  VARCHAR(20) NOT NULL DEFAULT 'nao_verificado',
        -- nao_verificado | em_analise | verificado | rejeitado
    created_at         TIMESTAMP DEFAULT now(),
    updated_at         TIMESTAMP DEFAULT now(),
    CONSTRAINT chk_status_identidade
        CHECK (status_identidade IN ('nao_verificado', 'em_analise', 'verificado', 'rejeitado'))
);

CREATE INDEX IF NOT EXISTS idx_users_status_identidade ON users(status_identidade);

CREATE TABLE IF NOT EXISTS user_phone_numbers (
    id        SERIAL PRIMARY KEY,
    user_id   INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telefone  VARCHAR(20) UNIQUE NOT NULL,
    ativo     BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT now()
);

-- Garante um único número ativo por usuário
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_phone_per_user
    ON user_phone_numbers(user_id) WHERE ativo = TRUE;

-- Documentos de identidade enviados pelo usuário para verificação
CREATE TABLE IF NOT EXISTS documentos_identidade (
    id                  SERIAL PRIMARY KEY,
    user_id             INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tipo                VARCHAR(30) NOT NULL DEFAULT 'desconhecido',
        -- rg | cnh | passaporte | desconhecido
    url_imagem          TEXT NOT NULL,
    whatsapp_media_id   TEXT,
    status              VARCHAR(20) NOT NULL DEFAULT 'em_analise',
        -- em_analise | aprovado | rejeitado
    motivo_rejeicao     TEXT,
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    analisado_em        TIMESTAMPTZ,
    analisado_por       TEXT
);

CREATE INDEX IF NOT EXISTS idx_documentos_identidade_user   ON documentos_identidade(user_id);
CREATE INDEX IF NOT EXISTS idx_documentos_identidade_status ON documentos_identidade(status);

-- -----------------------------------------------------------
-- 2. Perfis por papel
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS seller_profile (
    user_id                     INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    endereco_retirada           TEXT,
    horarios_disponiveis        JSONB,
    chave_pix                   VARCHAR(150),
    chave_pix_titular_confirmado VARCHAR(200),
    created_at                  TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS buyer_profile (
    user_id          INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    endereco_entrega TEXT,
    created_at       TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS courier_profile (
    user_id                     INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    chave_pix                   VARCHAR(150),
    chave_pix_titular_confirmado VARCHAR(200),
    regiao_atuacao              TEXT,
    created_at                  TIMESTAMP DEFAULT now()
);

-- -----------------------------------------------------------
-- 3. Catálogo de produtos (listings)
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS listings (
    id                        SERIAL PRIMARY KEY,
    seller_id                 INT NOT NULL REFERENCES users(id),
    descricao                 TEXT NOT NULL,
    categoria                 VARCHAR(100),
    fotos                     JSONB,
    preco_informado_vendedor  NUMERIC,
    preco_sugerido            NUMERIC,
    preco_anunciado           NUMERIC NOT NULL DEFAULT 0,
    preco_minimo              NUMERIC NOT NULL DEFAULT 0,
    appraisal_data            JSONB,
    status                    VARCHAR(30) NOT NULL DEFAULT 'disponivel',
        -- disponivel | em_negociacao | vendido | cancelado
    created_at                TIMESTAMP DEFAULT now(),
    updated_at                TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listings_status    ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_categoria ON listings(categoria);
CREATE INDEX IF NOT EXISTS idx_listings_seller    ON listings(seller_id);

-- -----------------------------------------------------------
-- 4. Fila de interessados
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS interest_queue (
    id            SERIAL PRIMARY KEY,
    listing_id    INT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    buyer_id      INT NOT NULL REFERENCES users(id),
    oferta_inicial NUMERIC,
    timestamp     TIMESTAMP DEFAULT now(),
    UNIQUE(listing_id, buyer_id)
);

CREATE INDEX IF NOT EXISTS idx_interest_queue_listing
    ON interest_queue(listing_id, timestamp);

-- -----------------------------------------------------------
-- 5. Negociações
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS negotiations (
    id                   SERIAL PRIMARY KEY,
    listing_id           INT NOT NULL REFERENCES listings(id),
    buyer_id             INT NOT NULL REFERENCES users(id),
    modo                 VARCHAR(20) NOT NULL DEFAULT 'proxy',
        -- proxy | direta
    status               VARCHAR(30) NOT NULL DEFAULT 'ativa',
        -- ativa | proposta_ao_vendedor | proposta_ao_comprador
        -- aceita | aguardando_pagamento | paga
        -- recusada | expirada | expirada_por_timeout | cancelada_pelo_usuario | sem_acordo
    preco_atual_proposto  NUMERIC,
    limite_comprador     JSONB,
    limite_vendedor      JSONB,
    responder_until      TIMESTAMP,
    expires_at           TIMESTAMP,
    tentativas_humanas   INT DEFAULT 0,
    created_at           TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_negotiations_status        ON negotiations(status);
CREATE INDEX IF NOT EXISTS idx_negotiations_listing       ON negotiations(listing_id);
CREATE INDEX IF NOT EXISTS idx_negotiations_buyer         ON negotiations(buyer_id);
CREATE INDEX IF NOT EXISTS idx_negotiations_responder_until
    ON negotiations(responder_until) WHERE status = 'ativa';

-- -----------------------------------------------------------
-- 6. Histórico de propostas (modo direto)
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS negotiation_offers (
    id             SERIAL PRIMARY KEY,
    negotiation_id INT NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
    autor          VARCHAR(20) NOT NULL,
        -- buyer | seller | sistema
    valor_proposto NUMERIC NOT NULL,
    contexto_extra TEXT,
    timestamp      TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_offers_negotiation
    ON negotiation_offers(negotiation_id);

-- -----------------------------------------------------------
-- 7. Histórico de rodadas entre proxies (modo proxy)
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS proxy_negotiation_rounds (
    id                       SERIAL PRIMARY KEY,
    negotiation_id           INT NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
    rodada                   INT NOT NULL,
    valor_proposto           NUMERIC NOT NULL,
    argumento_vendedor       TEXT,
    argumento_comprador      TEXT,
    confirmado_pelo_vendedor  BOOLEAN DEFAULT NULL,
    confirmado_pelo_comprador BOOLEAN DEFAULT NULL,
    created_at               TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proxy_rounds_negotiation
    ON proxy_negotiation_rounds(negotiation_id);

-- -----------------------------------------------------------
-- 8. Transações financeiras
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions (
    id                      SERIAL PRIMARY KEY,
    negotiation_id          INT NOT NULL REFERENCES negotiations(id),

    valor_produto           NUMERIC NOT NULL,
    valor_entrega           NUMERIC NOT NULL DEFAULT 0,
    taxa_notha              NUMERIC NOT NULL DEFAULT 0,

    modalidade_entrega      VARCHAR(20) NOT NULL DEFAULT 'retirada',
        -- retirada | entrega_notha

    chave_pix_vendedor      VARCHAR(150) NOT NULL,
    chave_pix_entregador    VARCHAR(150),
    entregador_id           INT REFERENCES users(id),

    asaas_charge_id         VARCHAR(100),
    asaas_transfer_id_vendedor   VARCHAR(100),
    asaas_transfer_id_entregador VARCHAR(100),

    status                  VARCHAR(30) NOT NULL DEFAULT 'pendente',
        -- pendente | cobranca_criada | pago | falhou
    status_retencao         VARCHAR(50) NOT NULL DEFAULT 'retido_aguardando_entrega',
        -- retido_aguardando_entrega | liberado
        -- retido_aguardando_decisao_pos_falha
        -- estornado_automaticamente | estornado_manualmente

    prazo_estorno_automatico TIMESTAMP,

    created_at              TIMESTAMP DEFAULT now(),
    updated_at              TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transactions_status_retencao
    ON transactions(status_retencao);
CREATE INDEX IF NOT EXISTS idx_transactions_negotiation
    ON transactions(negotiation_id);

-- View de conciliação financeira
CREATE OR REPLACE VIEW saldo_retido_total AS
SELECT COALESCE(SUM(valor_produto + valor_entrega), 0) AS total_retido
FROM transactions
WHERE status_retencao IN (
    'retido_aguardando_entrega',
    'retido_aguardando_decisao_pos_falha'
);

-- -----------------------------------------------------------
-- 9. Confirmações de entrega / retirada
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS delivery_confirmations (
    id                        SERIAL PRIMARY KEY,
    negotiation_id            INT NOT NULL REFERENCES negotiations(id),
    modalidade                VARCHAR(20) NOT NULL,
        -- retirada | entrega_notha
    entregador_id             INT REFERENCES users(id),

    data_agendada             DATE,
    horario_agendado          VARCHAR(50),
    prazo_confirmacao         TIMESTAMP,

    confirmado_pelo_vendedor  BOOLEAN DEFAULT FALSE,
    confirmado_pelo_comprador BOOLEAN DEFAULT FALSE,
    confirmado_em             TIMESTAMP,

    status                    VARCHAR(30) NOT NULL DEFAULT 'agendada',
        -- agendada | confirmada | nao_confirmada | convertida_entrega | cancelada
    relisted_at               TIMESTAMP,

    created_at                TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_confirmations_prazo
    ON delivery_confirmations(prazo_confirmacao) WHERE status = 'agendada';

CREATE INDEX IF NOT EXISTS idx_delivery_negotiation
    ON delivery_confirmations(negotiation_id);

-- -----------------------------------------------------------
-- 10. Armazenamento genérico de estado do agente (legado)
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_store (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Fim do schema
-- ============================================================
