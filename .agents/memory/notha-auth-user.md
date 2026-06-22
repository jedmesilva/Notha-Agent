---
name: NOTHA AuthUser e sessões
description: Arquitetura do agente AuthUser, gestão de sessão e re-autenticação multi-tier
---

## Regra
AuthUserAgent intercepta TODA mensagem (texto e mídia) antes de qualquer domain agent.
Integrado em `orchestrator.handle_message` logo após `find_or_create_by_phone` e em
`orchestrator.handle_media` antes do routing para listing flow ou identity document.

## Tiers de re-autenticação (por inatividade)
- < 7 dias   → sessão ativa, sem re-auth
- 7–30 dias  → CPF tier: usuário digita CPF, código compara com `users.tax_id`
- 30–90 dias → selfie tier: foto via WhatsApp, GPT-4o vision compara com documento cadastrado
- > 90 dias  → link tier: URL com token (15 min), face-api.js no browser + POST ao backend

## Tabelas criadas por migração em startup (main.py `_migrate_sessions_tables`)
- `sessions`: user_id, phone, status (active|pending_reauth|revoked), reauth_tier, reauth_attempts, last_activity_at
- `pending_verifications`: session_id, user_id, phone, token (unique), status, expires_at

## Armadilha PostgreSQL
`ON CONFLICT (phone)` NÃO funciona com índice parcial (`WHERE status = 'pending'`).
Solução: `UPDATE SET status='expired' WHERE phone=$1 AND status='pending'` + `INSERT` separado.

## Página de verificação
`/verificar/{token}` serve `static/verify.html` (face-api.js + câmera).
`/verificar/{token}/check` retorna 200 se token válido, 410 se expirado.
`/verificar/{token}/submit` recebe imagem base64, chama `_handle_selfie_tier` internamente.

**Why:** WhatsApp não tem mecanismo de login nativo. Sessões com re-auth progressiva equilibram
segurança e fricção para uma plataforma financeira transacional.
