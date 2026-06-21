---
name: NOTHA padronização inglês
description: Mapeamento de decisões da padronização para inglês de todo o código interno do NOTHA
---

## Regra central
Todo código interno (colunas DB, status values, nomes de métodos, variáveis, chaves de dict) está em inglês.
Textos ao usuário final (system prompts, mensagens WhatsApp, labels de UI) ficam em português.

## Status values (negociations)
`active` | `pending_seller` | `pending_buyer` | `accepted` | `no_deal` | `timed_out` | `expired` | `paid` | `cancelled`

## Status values (identity_documents)
`under_review` | `approved` | `rejected`

## Status values (users.identity_status)
`unverified` | `under_review` | `verified` | `rejected`

## Status values (listings)
`available` | `sold` | `cancelled` | `under_review`

## Status values (transactions.retention_status)
`held_pending_decision` | `auto_refunded`

## Tool names (LLM function calling — NOTHA_TOOLS)
`update_name` | `update_nickname` | `update_tax_id` | `list_product` | `search_product`
`save_interest` | `cancel_alerts` | `update_location` | `update_pix_key` | `update_address`

## Listing flow step names
`product` | `brand_model` | `usage_state` | `condition` | `receipt` | `photos_upload`
`address` | `price` | `processing` | `review_condition` | `confirm` | `done`

## Condition values (CONDITION_LABEL keys)
`like_new` | `good` | `fair` | `worn` | `defective`

## Document type values
`national_id` | `drivers_license` | `passport` | `unknown`

## Key column renames (schema)
- `nome` → `full_name`, `apelido` → `nickname`, `cpf` → `tax_id`
- `preco_anunciado` → `listed_price`, `preco_minimo` → `floor_price`
- `cidade_vendedor` → `seller_city`, `endereco_retirada` → `pickup_address`
- `chave_pix` → `pix_key`, `preco_atual_proposto` → `current_price`
- `documentos_identidade` → `identity_documents`, `buscas_salvas` → `saved_searches`
- `listing_flows.dados` → `listing_flows.data`, `listing_flows.fotos` → `listing_flows.photos`

## Migration
`artifacts/notha/db/migrations/006_english_standardization.sql` — aplicar no Supabase antes de ir a prod.
Até a migration ser aplicada, os jobs de background geram erros de coluna (esperado, não crítico).

**Why:** Codebase multilíngue (PT/EN misturado) criava bugs silenciosos e dificultava manutenção.
**How to apply:** Qualquer novo campo/status/método deve seguir inglês. Textos visíveis ao usuário: português.
