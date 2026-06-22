---
name: NOTHA refactor arquitetura agentes
description: Alterações estruturais da refatoração de arquitetura multi-agente — Turn State, routing determinístico, ContentSafety seletivo, CourierRegistry separado, scoped tools.
---

## Regra central
O Orchestrator é um roteador puro — nenhuma chamada LLM na fase Plan(). Toda decisão de ferramenta é determinística com base no `intent` da fase Understand().

## Turn State (tabela `turn_state`)
- Tabela: `phone PK, pending_field, operation, context_data JSONB, asked_at, expires_at`
- Migration em `main.py` → `_migrate_turn_state_table()`
- Repository: `db/repositories/turn_state.py` → `TurnStateRepository`
- Service: `engine/turn_state.py` → `TurnStateService`
- Exportado em `db/repositories/__init__.py`

**Why:** Evita que "Oi" seja capturado como nome. Antes de qualquer interpretação, o Orchestrator verifica se havia uma pergunta pendente do turno anterior.

**How to apply:** Em `handle_message()`, após `_build_context()`: chamar `TurnStateService.get_pending(phone)`, injetar nota no contexto, passar `pending_turn` ao `understand()`. Após tool saves, chamar `resolve_if_tool_matches()`. Após `final_reply`, chamar `_maybe_set_turn_state()`.

## Routing determinístico (`_deterministic_route()`)
- Substitui a chamada LLM `plan()` no Orchestrator
- `intent="buy"` ou `flow="product_search"` → `check_restriction` + `search_product`
- `intent="sell"` ou `flow="listing"` → `check_restriction` + `list_product`
- `intent="info"` + `needs_tools=True` → `web_search`
- Chitchat/greeting/out_of_scope → `[]`
- `data_update`/`other` → `[]` (heuristic merger cuida disso)

**Why:** Reduz latência (1 chamada LLM a menos por turno) e aumenta previsibilidade.

## `understand()` com `pending_turn`
- Assinatura: `understand(user_message, history, context, pending_turn=None)`
- Retorno inclui agora: `pending_resolved: bool`, `pending_value: str`
- Prompt `_UNDERSTAND_PROMPT` tem seção `{pending_turn_section}` injetada dinamicamente
- Quando `pending_turn` está presente, LLM avalia primeiro se a mensagem responde à pergunta

## ContentSafetyAgent (seletivo)
- `agents/content_safety.py`
- `should_check(description, category)` → heurístico, sem LLM, retorna `(bool, signals)`
- `evaluate(description, category, signals)` → LLM, só chamado quando `should_check=True`
- Integrado em `listing_flow.py` → `_step_product()` (antes da classificação de tipo)
- Listings suspeitos → `data["status"] = "em_revisao_manual"`, nunca bloqueio automático

## CourierRegistry separado
- `engine/courier_registry.py` → sem LLM, queries SQL puras
- Separado do `LogisticsAgent`/`DeliveryProxyAgent`

## Scoped tools por agente
- `tools/scoped.py` → `AGENT_TOOLS_MAP` e `tools_for(agent_name)`
- Cada agente recebe apenas ferramentas do seu domínio
