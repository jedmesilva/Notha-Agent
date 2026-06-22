---
name: NOTHA arquitetura multi-agente
description: Pipeline de 4 fases (Understand→Plan→Execute→Synthesize), guardrails financeiros, e decisões arquiteturais centrais
---

## Pipeline de 4 fases (implementado em 2026-06-22)

O loop agentic ReAct implícito foi substituído por um pipeline explícito de 4 fases em `engine/orchestrator.py` e `agents/conversation.py`:

### Fase 0 — Understand (`conversation.understand()`)
- Chamada LLM rápida, sem tools (~300ms)
- Retorna JSON: `{objective, intent, flow, needs_tools, confidence, notes}`
- O `objective` é o "contrato" que passa por todo o pipeline e chega ao guardrail

### Fase 1 — Plan (`conversation.plan()`)
- Recebe `tool_catalog` (nome + params required/optional + descrição) — NÃO só nomes
- Sem catalog completo o planner gera `args: {}` e o tool quebra com TypeError
- Retorna steps: `[{step, tool, args, reason, user_message}]`
- `user_message` é gerado pelo LLM no idioma do usuário — contextual, não fixo
- `user_message=null` para steps internos (check_restriction, update_*, get_datetime)
- check_restriction SEMPRE vem antes de search_product ou list_product

### Fase 2 — Execute (`orchestrator.handle_message` loop)
- Para cada step: envia user_message → executa tool → chama assess_result()
- `assess_result()` decide: continue | done | replan | abort
- replan injeta novos steps na fila
- abort vai para síntese com synthesis_instruction de falha
- Se tool retorna `complex_reply` (search/listing), pula direto para final sem síntese

### Fase 3 — Synthesize (`conversation.synthesize()`)
- Recebe: objective + outcome + todos os tool_results + history + context
- Passa objective para o guardrail → validação muito mais precisa

## Guardrail melhorado
- `validate_reply()` agora aceita `objective: str` (opcional)
- Prompts de avaliação e correção incluem o objetivo → guardrail sabe o que estava sendo tentado
- Reduz falsos positivos de incoerência quando a conversa muda de assunto por causa dos tools

## Regra financeira central (imutável)
- Guard rails de preço: sempre código determinístico, nunca LLM
- Proxies propõem valores dentro de limites → código valida e executa
- LLMs nunca executam ações financeiras diretamente

## Tool catalog para o planner
- O orchestrator monta `_TOOL_CATALOG` passando nome + params + required flags
- Tools NOTHA_TOOLS (OpenAI schema) e builtin tools (Tool.parameters) são normalizados juntos
- `plan()` aceita `tool_catalog` (preferido) ou `tool_names` (legado)

**Why:** Sem o catalog completo, o planner gerava `args: {}` para tools com params required (ex: check_restriction exige `product_description`) causando TypeError em runtime.
