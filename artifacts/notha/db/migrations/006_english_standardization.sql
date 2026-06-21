-- ============================================================
-- Migration 006 — English standardization
-- Renames all Portuguese columns, tables, and status values
-- to English. Apply via Supabase SQL Editor or psql.
--
-- SAFE TO RUN MULTIPLE TIMES (uses IF EXISTS / IF NOT EXISTS).
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. users table
-- ─────────────────────────────────────────────────────────────

ALTER TABLE users RENAME COLUMN nome    TO full_name;
ALTER TABLE users RENAME COLUMN apelido TO nickname;
ALTER TABLE users RENAME COLUMN cidade  TO city;
ALTER TABLE users RENAME COLUMN bairro  TO neighborhood;

-- Rename identity_status column (was status_identidade)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='users' AND column_name='status_identidade') THEN
        ALTER TABLE users RENAME COLUMN status_identidade TO identity_status;
    END IF;
END $$;

-- Rename cpf → tax_id
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='users' AND column_name='cpf') THEN
        ALTER TABLE users RENAME COLUMN cpf TO tax_id;
    END IF;
END $$;

-- Drop old constraint and recreate with new column name and English values
ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_status_identidade;
ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_identity_status;

-- Migrate existing status values
UPDATE users SET identity_status = CASE identity_status
    WHEN 'nao_verificado' THEN 'unverified'
    WHEN 'em_analise'     THEN 'under_review'
    WHEN 'verificado'     THEN 'verified'
    WHEN 'rejeitado'      THEN 'rejected'
    ELSE identity_status
END;

ALTER TABLE users
    ALTER COLUMN identity_status SET DEFAULT 'unverified',
    ADD CONSTRAINT chk_identity_status
        CHECK (identity_status IN ('unverified', 'under_review', 'verified', 'rejected'));

DROP INDEX IF EXISTS idx_users_status_identidade;
CREATE INDEX IF NOT EXISTS idx_users_identity_status ON users(identity_status);

-- ─────────────────────────────────────────────────────────────
-- 2. user_phone_numbers table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='user_phone_numbers' AND column_name='telefone') THEN
        ALTER TABLE user_phone_numbers RENAME COLUMN telefone TO phone;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='user_phone_numbers' AND column_name='ativo') THEN
        ALTER TABLE user_phone_numbers RENAME COLUMN ativo TO active;
    END IF;
END $$;

-- Recreate partial unique index using new column name
DROP INDEX IF EXISTS idx_one_active_phone_per_user;
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_phone_per_user
    ON user_phone_numbers(user_id) WHERE active = TRUE;

-- ─────────────────────────────────────────────────────────────
-- 3. documentos_identidade → identity_documents
-- ─────────────────────────────────────────────────────────────

ALTER TABLE IF EXISTS documentos_identidade RENAME TO identity_documents;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='identity_documents' AND column_name='tipo') THEN
        ALTER TABLE identity_documents RENAME COLUMN tipo TO doc_type;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='identity_documents' AND column_name='url_imagem') THEN
        ALTER TABLE identity_documents RENAME COLUMN url_imagem TO image_url;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='identity_documents' AND column_name='motivo_rejeicao') THEN
        ALTER TABLE identity_documents RENAME COLUMN motivo_rejeicao TO rejection_reason;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='identity_documents' AND column_name='criado_em') THEN
        ALTER TABLE identity_documents RENAME COLUMN criado_em TO created_at;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='identity_documents' AND column_name='analisado_em') THEN
        ALTER TABLE identity_documents RENAME COLUMN analisado_em TO reviewed_at;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='identity_documents' AND column_name='analisado_por') THEN
        ALTER TABLE identity_documents RENAME COLUMN analisado_por TO reviewed_by;
    END IF;
END $$;

-- Migrate doc_type values
UPDATE identity_documents SET doc_type = CASE doc_type
    WHEN 'rg'          THEN 'national_id'
    WHEN 'cnh'         THEN 'drivers_license'
    WHEN 'passaporte'  THEN 'passport'
    WHEN 'desconhecido' THEN 'unknown'
    ELSE doc_type
END;

-- Migrate status values
UPDATE identity_documents SET status = CASE status
    WHEN 'em_analise' THEN 'under_review'
    WHEN 'aprovado'   THEN 'approved'
    WHEN 'rejeitado'  THEN 'rejected'
    ELSE status
END;

ALTER TABLE identity_documents ALTER COLUMN doc_type SET DEFAULT 'unknown';
ALTER TABLE identity_documents ALTER COLUMN status   SET DEFAULT 'under_review';

DROP INDEX IF EXISTS idx_documentos_identidade_user;
DROP INDEX IF EXISTS idx_documentos_identidade_status;
CREATE INDEX IF NOT EXISTS idx_identity_documents_user   ON identity_documents(user_id);
CREATE INDEX IF NOT EXISTS idx_identity_documents_status ON identity_documents(status);

-- ─────────────────────────────────────────────────────────────
-- 4. seller_profile table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='seller_profile' AND column_name='endereco_retirada') THEN
        ALTER TABLE seller_profile RENAME COLUMN endereco_retirada TO pickup_address;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='seller_profile' AND column_name='horarios_disponiveis') THEN
        ALTER TABLE seller_profile RENAME COLUMN horarios_disponiveis TO available_hours;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='seller_profile' AND column_name='chave_pix') THEN
        ALTER TABLE seller_profile RENAME COLUMN chave_pix TO pix_key;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='seller_profile' AND column_name='chave_pix_titular_confirmado') THEN
        ALTER TABLE seller_profile RENAME COLUMN chave_pix_titular_confirmado TO pix_holder_name;
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────
-- 5. buyer_profile table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='buyer_profile' AND column_name='endereco_entrega') THEN
        ALTER TABLE buyer_profile RENAME COLUMN endereco_entrega TO delivery_address;
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────
-- 6. courier_profile table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='courier_profile' AND column_name='chave_pix') THEN
        ALTER TABLE courier_profile RENAME COLUMN chave_pix TO pix_key;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='courier_profile' AND column_name='chave_pix_titular_confirmado') THEN
        ALTER TABLE courier_profile RENAME COLUMN chave_pix_titular_confirmado TO pix_holder_name;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='courier_profile' AND column_name='regiao_atuacao') THEN
        ALTER TABLE courier_profile RENAME COLUMN regiao_atuacao TO service_area;
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────
-- 7. listings table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='descricao') THEN
        ALTER TABLE listings RENAME COLUMN descricao TO description;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='categoria') THEN
        ALTER TABLE listings RENAME COLUMN categoria TO category;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='fotos') THEN
        ALTER TABLE listings RENAME COLUMN fotos TO photos;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='preco_informado_vendedor') THEN
        ALTER TABLE listings RENAME COLUMN preco_informado_vendedor TO seller_asking_price;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='preco_sugerido') THEN
        ALTER TABLE listings RENAME COLUMN preco_sugerido TO suggested_price;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='preco_anunciado') THEN
        ALTER TABLE listings RENAME COLUMN preco_anunciado TO listed_price;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='preco_minimo') THEN
        ALTER TABLE listings RENAME COLUMN preco_minimo TO floor_price;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='marca') THEN
        ALTER TABLE listings RENAME COLUMN marca TO brand;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='modelo') THEN
        ALTER TABLE listings RENAME COLUMN modelo TO model;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='versao') THEN
        ALTER TABLE listings RENAME COLUMN versao TO version;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='estado_uso') THEN
        ALTER TABLE listings RENAME COLUMN estado_uso TO usage_state;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='condicao') THEN
        ALTER TABLE listings RENAME COLUMN condicao TO condition;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='tem_nota_fiscal') THEN
        ALTER TABLE listings RENAME COLUMN tem_nota_fiscal TO has_receipt;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='fotos_info') THEN
        ALTER TABLE listings RENAME COLUMN fotos_info TO info_photos;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='preco_minimo_vendedor') THEN
        ALTER TABLE listings RENAME COLUMN preco_minimo_vendedor TO seller_minimum_price;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='info_web') THEN
        ALTER TABLE listings RENAME COLUMN info_web TO web_info;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='cidade_vendedor') THEN
        ALTER TABLE listings RENAME COLUMN cidade_vendedor TO seller_city;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listings' AND column_name='bairro_vendedor') THEN
        ALTER TABLE listings RENAME COLUMN bairro_vendedor TO seller_neighborhood;
    END IF;
END $$;

-- Migrate listings.status values
UPDATE listings SET status = CASE status
    WHEN 'disponivel'     THEN 'available'
    WHEN 'em_negociacao'  THEN 'in_negotiation'
    WHEN 'vendido'        THEN 'sold'
    WHEN 'cancelado'      THEN 'cancelled'
    ELSE status
END;
ALTER TABLE listings ALTER COLUMN status SET DEFAULT 'available';

-- Migrate usage_state values
UPDATE listings SET usage_state = CASE usage_state
    WHEN 'novo'   THEN 'new'
    WHEN 'usado'  THEN 'used'
    ELSE usage_state
END;

-- Migrate condition values
UPDATE listings SET condition = CASE condition
    WHEN 'como_novo'   THEN 'like_new'
    WHEN 'bom'         THEN 'good'
    WHEN 'conservado'  THEN 'fair'
    WHEN 'desgastado'  THEN 'worn'
    WHEN 'com_defeito' THEN 'defective'
    ELSE condition
END;

DROP INDEX IF EXISTS idx_listings_categoria;
CREATE INDEX IF NOT EXISTS idx_listings_category            ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_seller_city         ON listings(seller_city);
CREATE INDEX IF NOT EXISTS idx_listings_seller_neighborhood ON listings(seller_neighborhood);

-- ─────────────────────────────────────────────────────────────
-- 8. listing_flows table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listing_flows' AND column_name='dados') THEN
        ALTER TABLE listing_flows RENAME COLUMN dados TO data;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='listing_flows' AND column_name='fotos') THEN
        ALTER TABLE listing_flows RENAME COLUMN fotos TO photos;
    END IF;
END $$;

-- Migrate step values
UPDATE listing_flows SET step = CASE step
    WHEN 'produto'          THEN 'product'
    WHEN 'marca_modelo'     THEN 'brand_model'
    WHEN 'estado_uso'       THEN 'usage_state'
    WHEN 'condicao'         THEN 'condition'
    WHEN 'nota_fiscal'      THEN 'receipt'
    WHEN 'fotos'            THEN 'photos_upload'
    WHEN 'endereco'         THEN 'address'
    WHEN 'preco'            THEN 'price'
    WHEN 'processando'      THEN 'processing'
    WHEN 'revisar_condicao' THEN 'review_condition'
    WHEN 'confirmar'        THEN 'confirm'
    WHEN 'concluido'        THEN 'done'
    ELSE step
END;
ALTER TABLE listing_flows ALTER COLUMN step SET DEFAULT 'product';

-- Recreate partial unique index with new terminal step value
DROP INDEX IF EXISTS idx_listing_flows_phone_active;
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_flows_phone_active
    ON listing_flows(phone) WHERE step != 'done';

-- ─────────────────────────────────────────────────────────────
-- 9. interest_queue table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='interest_queue' AND column_name='oferta_inicial') THEN
        ALTER TABLE interest_queue RENAME COLUMN oferta_inicial TO initial_offer;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='interest_queue' AND column_name='timestamp') THEN
        ALTER TABLE interest_queue RENAME COLUMN timestamp TO created_at;
    END IF;
END $$;

DROP INDEX IF EXISTS idx_interest_queue_listing;
CREATE INDEX IF NOT EXISTS idx_interest_queue_listing ON interest_queue(listing_id, created_at);

-- ─────────────────────────────────────────────────────────────
-- 10. negotiations table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiations' AND column_name='modo') THEN
        ALTER TABLE negotiations RENAME COLUMN modo TO mode;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiations' AND column_name='preco_atual_proposto') THEN
        ALTER TABLE negotiations RENAME COLUMN preco_atual_proposto TO current_price;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiations' AND column_name='limite_comprador') THEN
        ALTER TABLE negotiations RENAME COLUMN limite_comprador TO buyer_limits;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiations' AND column_name='limite_vendedor') THEN
        ALTER TABLE negotiations RENAME COLUMN limite_vendedor TO seller_limits;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiations' AND column_name='tentativas_humanas') THEN
        ALTER TABLE negotiations RENAME COLUMN tentativas_humanas TO human_attempts;
    END IF;
END $$;

-- Migrate mode values
UPDATE negotiations SET mode = 'direct' WHERE mode = 'direta';
ALTER TABLE negotiations ALTER COLUMN mode SET DEFAULT 'proxy';

-- Migrate status values
UPDATE negotiations SET status = CASE status
    WHEN 'ativa'                   THEN 'active'
    WHEN 'proposta_ao_vendedor'    THEN 'pending_seller'
    WHEN 'proposta_ao_comprador'   THEN 'pending_buyer'
    WHEN 'aceita'                  THEN 'accepted'
    WHEN 'aguardando_pagamento'    THEN 'awaiting_payment'
    WHEN 'paga'                    THEN 'paid'
    WHEN 'recusada'                THEN 'rejected'
    WHEN 'expirada'                THEN 'expired'
    WHEN 'expirada_por_timeout'    THEN 'timed_out'
    WHEN 'cancelada_pelo_usuario'  THEN 'cancelled'
    WHEN 'sem_acordo'              THEN 'no_deal'
    ELSE status
END;
ALTER TABLE negotiations ALTER COLUMN status SET DEFAULT 'active';

-- Recreate partial index with new status value
DROP INDEX IF EXISTS idx_negotiations_responder_until;
CREATE INDEX IF NOT EXISTS idx_negotiations_responder_until
    ON negotiations(responder_until) WHERE status = 'active';

-- ─────────────────────────────────────────────────────────────
-- 11. negotiation_offers table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiation_offers' AND column_name='autor') THEN
        ALTER TABLE negotiation_offers RENAME COLUMN autor TO author;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiation_offers' AND column_name='valor_proposto') THEN
        ALTER TABLE negotiation_offers RENAME COLUMN valor_proposto TO proposed_value;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiation_offers' AND column_name='contexto_extra') THEN
        ALTER TABLE negotiation_offers RENAME COLUMN contexto_extra TO extra_context;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='negotiation_offers' AND column_name='timestamp') THEN
        ALTER TABLE negotiation_offers RENAME COLUMN timestamp TO created_at;
    END IF;
END $$;

UPDATE negotiation_offers SET author = 'system' WHERE author = 'sistema';

-- ─────────────────────────────────────────────────────────────
-- 12. proxy_negotiation_rounds table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proxy_negotiation_rounds' AND column_name='rodada') THEN
        ALTER TABLE proxy_negotiation_rounds RENAME COLUMN rodada TO round_number;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proxy_negotiation_rounds' AND column_name='valor_proposto') THEN
        ALTER TABLE proxy_negotiation_rounds RENAME COLUMN valor_proposto TO proposed_value;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proxy_negotiation_rounds' AND column_name='argumento_vendedor') THEN
        ALTER TABLE proxy_negotiation_rounds RENAME COLUMN argumento_vendedor TO seller_argument;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proxy_negotiation_rounds' AND column_name='argumento_comprador') THEN
        ALTER TABLE proxy_negotiation_rounds RENAME COLUMN argumento_comprador TO buyer_argument;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proxy_negotiation_rounds' AND column_name='confirmado_pelo_vendedor') THEN
        ALTER TABLE proxy_negotiation_rounds RENAME COLUMN confirmado_pelo_vendedor TO confirmed_by_seller;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='proxy_negotiation_rounds' AND column_name='confirmado_pelo_comprador') THEN
        ALTER TABLE proxy_negotiation_rounds RENAME COLUMN confirmado_pelo_comprador TO confirmed_by_buyer;
    END IF;
END $$;

-- ─────────────────────────────────────────────────────────────
-- 13. transactions table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='valor_produto') THEN
        ALTER TABLE transactions RENAME COLUMN valor_produto TO product_amount;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='valor_entrega') THEN
        ALTER TABLE transactions RENAME COLUMN valor_entrega TO delivery_amount;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='taxa_notha') THEN
        ALTER TABLE transactions RENAME COLUMN taxa_notha TO notha_fee;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='modalidade_entrega') THEN
        ALTER TABLE transactions RENAME COLUMN modalidade_entrega TO delivery_mode;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='chave_pix_vendedor') THEN
        ALTER TABLE transactions RENAME COLUMN chave_pix_vendedor TO seller_pix_key;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='chave_pix_entregador') THEN
        ALTER TABLE transactions RENAME COLUMN chave_pix_entregador TO courier_pix_key;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='entregador_id') THEN
        ALTER TABLE transactions RENAME COLUMN entregador_id TO courier_id;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='asaas_transfer_id_vendedor') THEN
        ALTER TABLE transactions RENAME COLUMN asaas_transfer_id_vendedor TO asaas_transfer_id_seller;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='asaas_transfer_id_entregador') THEN
        ALTER TABLE transactions RENAME COLUMN asaas_transfer_id_entregador TO asaas_transfer_id_courier;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='status_retencao') THEN
        ALTER TABLE transactions RENAME COLUMN status_retencao TO retention_status;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='transactions' AND column_name='prazo_estorno_automatico') THEN
        ALTER TABLE transactions RENAME COLUMN prazo_estorno_automatico TO auto_refund_deadline;
    END IF;
END $$;

-- Migrate delivery_mode values
UPDATE transactions SET delivery_mode = CASE delivery_mode
    WHEN 'retirada'       THEN 'pickup'
    WHEN 'entrega_notha'  THEN 'notha_delivery'
    ELSE delivery_mode
END;
ALTER TABLE transactions ALTER COLUMN delivery_mode SET DEFAULT 'pickup';

-- Migrate status values
UPDATE transactions SET status = CASE status
    WHEN 'pendente'         THEN 'pending'
    WHEN 'cobranca_criada'  THEN 'charge_created'
    WHEN 'pago'             THEN 'paid'
    WHEN 'falhou'           THEN 'failed'
    ELSE status
END;
ALTER TABLE transactions ALTER COLUMN status SET DEFAULT 'pending';

-- Migrate retention_status values
UPDATE transactions SET retention_status = CASE retention_status
    WHEN 'retido_aguardando_entrega'           THEN 'held_pending_delivery'
    WHEN 'liberado'                             THEN 'released'
    WHEN 'retido_aguardando_decisao_pos_falha' THEN 'held_pending_decision'
    WHEN 'estornado_automaticamente'            THEN 'auto_refunded'
    WHEN 'estornado_manualmente'                THEN 'manually_refunded'
    ELSE retention_status
END;
ALTER TABLE transactions ALTER COLUMN retention_status SET DEFAULT 'held_pending_delivery';

DROP INDEX IF EXISTS idx_transactions_status_retencao;
CREATE INDEX IF NOT EXISTS idx_transactions_retention_status ON transactions(retention_status);

-- Recreate reconciliation view
DROP VIEW IF EXISTS saldo_retido_total;
CREATE OR REPLACE VIEW retained_balance_total AS
SELECT COALESCE(SUM(product_amount + delivery_amount), 0) AS total_retained
FROM transactions
WHERE retention_status IN ('held_pending_delivery', 'held_pending_decision');

-- ─────────────────────────────────────────────────────────────
-- 14. delivery_confirmations table
-- ─────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='modalidade') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN modalidade TO delivery_mode;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='entregador_id') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN entregador_id TO courier_id;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='data_agendada') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN data_agendada TO scheduled_date;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='horario_agendado') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN horario_agendado TO scheduled_time;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='prazo_confirmacao') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN prazo_confirmacao TO confirmation_deadline;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='confirmado_pelo_vendedor') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN confirmado_pelo_vendedor TO confirmed_by_seller;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='confirmado_pelo_comprador') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN confirmado_pelo_comprador TO confirmed_by_buyer;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='delivery_confirmations' AND column_name='confirmado_em') THEN
        ALTER TABLE delivery_confirmations RENAME COLUMN confirmado_em TO confirmed_at;
    END IF;
END $$;

-- Migrate delivery_mode values
UPDATE delivery_confirmations SET delivery_mode = CASE delivery_mode
    WHEN 'retirada'      THEN 'pickup'
    WHEN 'entrega_notha' THEN 'notha_delivery'
    ELSE delivery_mode
END;

-- Migrate status values
UPDATE delivery_confirmations SET status = CASE status
    WHEN 'agendada'           THEN 'scheduled'
    WHEN 'confirmada'         THEN 'confirmed'
    WHEN 'nao_confirmada'     THEN 'unconfirmed'
    WHEN 'convertida_entrega' THEN 'converted'
    WHEN 'cancelada'          THEN 'cancelled'
    ELSE status
END;
ALTER TABLE delivery_confirmations ALTER COLUMN status SET DEFAULT 'scheduled';

DROP INDEX IF EXISTS idx_delivery_confirmations_prazo;
CREATE INDEX IF NOT EXISTS idx_delivery_confirmations_deadline
    ON delivery_confirmations(confirmation_deadline) WHERE status = 'scheduled';

-- ─────────────────────────────────────────────────────────────
-- 15. buscas_salvas → saved_searches
-- ─────────────────────────────────────────────────────────────

ALTER TABLE IF EXISTS buscas_salvas RENAME TO saved_searches;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='saved_searches' AND column_name='descricao_busca') THEN
        ALTER TABLE saved_searches RENAME COLUMN descricao_busca TO search_description;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='saved_searches' AND column_name='cidade_busca') THEN
        ALTER TABLE saved_searches RENAME COLUMN cidade_busca TO search_city;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='saved_searches' AND column_name='bairro_busca') THEN
        ALTER TABLE saved_searches RENAME COLUMN bairro_busca TO search_neighborhood;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='saved_searches' AND column_name='ultima_notificacao') THEN
        ALTER TABLE saved_searches RENAME COLUMN ultima_notificacao TO last_notified_at;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='saved_searches' AND column_name='categoria') THEN
        ALTER TABLE saved_searches RENAME COLUMN categoria TO category;
    END IF;
END $$;

UPDATE saved_searches SET status = CASE status
    WHEN 'ativa'     THEN 'active'
    WHEN 'cancelada' THEN 'cancelled'
    ELSE status
END;
ALTER TABLE saved_searches ALTER COLUMN status SET DEFAULT 'active';

DROP INDEX IF EXISTS idx_buscas_salvas_user;
DROP INDEX IF EXISTS idx_buscas_salvas_status;
DROP INDEX IF EXISTS idx_buscas_salvas_phone;
CREATE INDEX IF NOT EXISTS idx_saved_searches_user   ON saved_searches(user_id);
CREATE INDEX IF NOT EXISTS idx_saved_searches_status ON saved_searches(status);
CREATE INDEX IF NOT EXISTS idx_saved_searches_phone  ON saved_searches(phone);

-- ============================================================
-- End of migration 006
-- ============================================================
