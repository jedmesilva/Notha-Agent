---
name: NOTHA financial gaps — session julho/2026
description: Decisões de implementação dos 7 gaps do motor financeiro identificados contra o documento de estrutura de dados.
---

# NOTHA Financial Gaps — Decisões

## Regras críticas implementadas

### accept_investment — validação antes de mutações
`maturity_at` (e qualquer outro parâmetro obrigatório) DEVE ser validado e normalizado
antes de qualquer INSERT/UPDATE no banco. Se a validação estiver depois das wallet
transactions, uma ValueError deixa o banco em estado inconsistente.

**Why:** bug introduzido nesta sessão, detectado pelo code review do arquiteto.

**How to apply:** qualquer novo parâmetro obrigatório em funções financeiras deve ser
validado na primeira linha, retornando `{"ok": False, "error": "..."}` antes de
tocar o banco.

### distribute_payouts — atomicidade obrigatória
Os 4 wallet.add_transaction + mark_payout_paid + update_status devem estar dentro de
`async with db.atomic() as tx`. Sem isso, falha parcial → double-payment no próximo run.

**Why:** payout tem 4 movimentações (juros debit/credit + principal debit/credit).
Qualquer falha intermediária deixa txs postadas com payout ainda 'scheduled'.

**How to apply:** usar `db.atomic()` (adicionado em db/connection.py) sempre que
houver ≥2 movimentações wallet + atualização de status que precisam ser todas-ou-nada.

### Modelo de payout — único no vencimento
Substituiu ciclo mensal hardcoded (30 dias). Cada investimento tem `maturity_at` próprio.
Juros simples: `I = P × r × t` onde `t = term_seconds / 31557600`.
Payout = interesse + devolução de principal, em transações separadas no mesmo atomic block.

**Why:** investimentos podem ter prazo de minutos a meses; ciclo fixo era incorreto.

### Term rate formula — fórmulas contínuas vs bandas fixas
`get_term_adjustment` agora suporta 'linear', 'log', 'sqrt' além de 'bands'.
Params em group_rate_policies: `term_rate_formula`, `term_rate_base_bps`, `term_rate_scale`.
No modo 'bands', lança ValueError se nenhuma banda cobrir o prazo — nunca silencia.

**Why:** bandas fixas não cobrem prazos arbitrários (ex: 13 dias entre duas bandas).

### Credit limit — sem bypass silencioso
Quando não há configuração individual, a cadeia de fallback é:
1. score_band (% de max_per_user_limit)
2. max_per_user_limit direto (quando sem bands)
3. group_rate_policies.default_individual_limit
4. Decimal("0") — rejeita qualquer valor (nunca None)

Retornar None fazia validate_limits pular o check individual silenciosamente.

### Ingestion de risk events — endpoint REST
POST /admin/risk-events insere em location_risk_events e dispara
recalculate_location_market_metrics em background (asyncio.create_task).
Requer header Authorization: Bearer <ADMIN_API_KEY> quando configurado.
GET /admin/risk-events lista eventos (com filtro por geohash prefix e janela de dias).

### Fire-and-forget — create_opportunity e snapshot_liquidity_for_group
Ambos são asyncio.create_task após approve_loan e accept_investment.
Falha nunca reverte a operação principal.

### InvestirTool — breaking change de contrato
Tool agora requer `maturity_at` (ISO-8601) e retorna `interest_at_maturity`
(era `monthly_return`). LLM deve perguntar o prazo antes de chamar a tool.
