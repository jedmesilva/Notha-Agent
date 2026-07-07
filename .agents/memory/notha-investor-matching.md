---
name: NOTHA Investor Matching
description: Arquitetura do sistema de perfil de investidor, matching e distribuição de oportunidades — decisões críticas de concorrência e atomicidade
---

## Arquitetura geral

**Tabelas novas:** `investor_profiles` (preferências + métricas), `investment_offers` (ofertas pendentes por investidor por oportunidade).

**Fluxo:** `approve_loan` → `create_opportunity(loan_term_days=term_days)` → `match_and_notify()` (fire-and-forget) → investidores selecionados recebem WhatsApp ou investem automaticamente.

## Decisões críticas de atomicidade

### accept_investment() — SELECT FOR UPDATE + db.atomic()
Todo o caminho de aceitação (validação remaining, debit wallet, create investment, add_commitment, schedule_payout) acontece dentro de `async with db.atomic() as tx:` com `SELECT ... FOR UPDATE` na opportunity.
- **Por quê:** sem lock, dois aceites concorrentes passavam na validação do remaining e criavam double-commit; wallet debit + opp commitment em calls separadas violava consistência financeira.
- **Como aplicar:** repositories dentro do bloco recebem `tx` (não `db`). Efeitos colaterais (snapshot_liquidity) ficam fora, após o commit.

### process_offer_response() — CAS antes de investir
Usa `UPDATE investment_offers SET status='processing' WHERE id=? AND status='pending' RETURNING id` antes de chamar `accept_investment()`.
- Se o UPDATE retorna 0 rows → outro processo já reclamou a oferta → retorna erro sem chamar accept_investment.
- Se accept_investment falha → reverte status 'processing' → 'pending' para permitir nova tentativa.
- **Por quê:** sem CAS, dois requests do mesmo usuário (double-tap) podiam ambos ler status='pending' e criar investimentos duplicados.

### Partial unique index em investment_offers
`CREATE UNIQUE INDEX idx_investment_offers_pending_unique ON investment_offers(opportunity_id, user_id) WHERE status = 'pending'`
- Remove a constraint UNIQUE global (que impedia re-oferta permanentemente).
- Permite ao mesmo investidor receber nova oferta após decline/expire.
- ON CONFLICT no INSERT usa a sintaxe: `ON CONFLICT (opportunity_id, user_id) WHERE status = 'pending' DO NOTHING`

### Resiliência a hot-reload concorrente na migração
`CREATE INDEX IF NOT EXISTS` não é atômico entre processos no pg_catalog. Uvicorn com `--reload` pode disparar dois workers simultaneamente causando `UniqueViolationError` no pg_class.
- **Fix:** cada DDL de índice envolvido em `try/except` capturando `UniqueViolationError | DuplicateTableError | DuplicateObjectError`.

## maturity_at dos investimentos
`create_opportunity` recebe `loan_term_days` (passado por `approve_loan` via `term_days`). `investment_maturity_at = now + timedelta(days=loan_term_days)`. Fallback para `expires_at` somente em criação manual (sem empréstimo subjacente).
- **Por quê:** usar `expires_at` (TTL da oportunidade) como maturity criava vencimentos errados para investidores que aceitavam tarde — o prazo do empréstimo é o referencial econômico correto.

## Jobs novos
- `recalculate_investor_metrics` (1h): avg_investment_amount, avg_term_days, total_invested_lifetime, active_investment_count calculados via SQL sobre tabela investments.
- `expire_investment_offers` (5min): UPDATE investment_offers SET status='expired' WHERE status='pending' AND expires_at < NOW().

## Scoring e distribuição
- Score = 40% capacidade + 30% histórico + 10% bonus auto_invest.
- Distribuição: first-fit decreasing por score — `suggested = min(capacity, remaining)`.
- `auto_invest=True`: chama accept_investment() direto; notifica no WhatsApp após commit.
- `auto_invest=False`: cria investment_offer + envia mensagem WhatsApp com oferta de 24h.
