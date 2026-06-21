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
    updated_at                TIMESTAMP DEFAULT now(),
    -- Campos do fluxo completo de cadastro
    marca                     VARCHAR(100),
    modelo                    VARCHAR(200),
    versao                    VARCHAR(100),
    estado_uso                VARCHAR(20),
        -- novo | usado
    condicao                  VARCHAR(50),
        -- como_novo | bom | conservado | desgastado | com_defeito
    tem_nota_fiscal           BOOLEAN,
    fotos_info                JSONB,
        -- fotos de etiquetas, embalagem ou nota fiscal
    preco_minimo_vendedor     NUMERIC,
        -- piso definido pelo vendedor (sigiloso)
    info_web                  JSONB,
        -- resultado da busca web (preços, specs)
    cidade_vendedor           VARCHAR(100),
        -- para precificação geográfica
    vision_analysis           TEXT
        -- análise visual pelo GPT-4o Vision
);

CREATE INDEX IF NOT EXISTS idx_listings_status    ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_categoria ON listings(categoria);
CREATE INDEX IF NOT EXISTS idx_listings_seller    ON listings(seller_id);

-- -----------------------------------------------------------
-- Fluxo de cadastro de produto (máquina de estados)
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS listing_flows (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone       VARCHAR(20) NOT NULL,
    step        VARCHAR(50) NOT NULL DEFAULT 'produto',
        -- produto | marca_modelo | estado_uso | condicao | nota_fiscal |
        -- fotos | endereco | preco | processando | confirmar | concluido
    dados       JSONB NOT NULL DEFAULT '{}',
    fotos       JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Apenas um fluxo ativo por telefone
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_flows_phone_active
    ON listing_flows(phone) WHERE step != 'concluido';

CREATE INDEX IF NOT EXISTS idx_listing_flows_user ON listing_flows(user_id);

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

-- -----------------------------------------------------------
-- 11. Itens restritos (lista dinâmica gerenciada por admins)
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS restricted_items (
    id              SERIAL PRIMARY KEY,
    category        VARCHAR(100) NOT NULL,
        -- e.g. 'weapons', 'drugs', 'counterfeit_goods', 'illegal_animals'
    keywords        TEXT[]       NOT NULL DEFAULT '{}',
        -- Terms that trigger the restriction (e.g. {'pistol','firearm','gun'})
    description     TEXT,
        -- Human-readable description of what is restricted
    reason          TEXT         NOT NULL,
        -- Legal / regulatory basis for the restriction
    scope           VARCHAR(20)  NOT NULL DEFAULT 'national'
        CHECK (scope IN ('national', 'state', 'municipal')),
        -- national | state | municipal
    state_code      CHAR(2),
        -- Filled when scope = 'state' (e.g. 'SP', 'RJ')
    municipality    VARCHAR(100),
        -- Filled when scope = 'municipal'
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by      TEXT
        -- Email or identifier of the admin who created/last edited this record
);

CREATE INDEX IF NOT EXISTS idx_restricted_items_category  ON restricted_items(category);
CREATE INDEX IF NOT EXISTS idx_restricted_items_is_active ON restricted_items(is_active);
CREATE INDEX IF NOT EXISTS idx_restricted_items_keywords  ON restricted_items USING GIN(keywords);

-- Seed: base categories — adjust and expand as needed
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

-- ============================================================
-- End of schema
-- ============================================================
