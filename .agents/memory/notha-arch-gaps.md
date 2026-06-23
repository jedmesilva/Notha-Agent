---
name: NOTHA gaps arquitetura implementados
description: Lacunas entre o doc de arquitetura e o código, implementadas em junho/2026. Decisões e convenções que futuras sessões devem respeitar.
---

## §5 — System prompt como arquivo versionado separado

**Regra**: `SYSTEM_PROMPT` vive em `agents/prompts/system.txt` e é carregado com `pathlib.Path(__file__).parent / "prompts" / "system.txt"`. Não inline no código.

**Why**: Identidade, escopo e tom de NOTHA são texto versionado, não código. Facilita revisão sem tocar em Python.

**How to apply**: Qualquer alteração de persona vai em `agents/prompts/system.txt`, nunca diretamente em `conversation.py`.

---

## §12 — ScopeReviewerAgent (agents/reviewer.py)

**Regra**: Roda DEPOIS de qualquer síntese, ANTES de enviar ao WhatsApp. Dois critérios: (1) dentro do escopo NOTHA? (2) coerente com dados reais do contexto/DB?

**Why**: Evita que o LLM responda perguntas fora de escopo (receitas, piadas, traduções) ou invente dados do usuário.

**How to apply**: Fail-open — se a chamada LLM falhar, retorna a resposta original. A heurística `_should_review()` evita chamar o LLM para a maioria das respostas limpas (len < 25 e sem sinais off-topic).

Ponto de integração em `orchestrator.py`: linha logo após `final_reply` ser montado por `synthesize()`, antes de `_maybe_set_turn_state()`.

---

## §12 — Circuit breaker (attempt_count em turn_state)

**Regra**: `turn_state.attempt_count` incrementa atomicamente a cada `get()` via `UPDATE … RETURNING`. Limite: `MAX_ATTEMPTS = 3` em `engine/turn_state.py`. Quando `is_exhausted()` retorna True: o orchestrator limpa o pending, salva `_exhausted_field`, e não re-seta aquele campo.

**Why**: Evita loops infinitos onde o sistema pergunta a mesma coisa repetidamente sem progressão.

**How to apply**: `_maybe_set_turn_state()` recebe `exhausted_field` — se `pending_field == exhausted_field`, não seta. Migration: `ALTER TABLE turn_state ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0` roda em cada startup.

---

## §12 — Schema validator (tools/schema_validator.py)

**Regra**: `validate_understand()` e `validate_assess()` são chamados logo após `json.loads()` de cada agente. `repair=True` (default): substitui valores inválidos por safe defaults e loga warning. `repair=False`: lança `SchemaValidationError`.

**Why**: Saída malformada de LLM nunca silenciosa — sempre logada e corrigida de forma explícita.

**How to apply**: `validate_proxy()` existe para proxies. Sempre passar o dict bruto do LLM antes de usar qualquer campo.

---

## §4 — Resolução ambígua de pending (3-state)

**Regra**: `understand()` retorna `pending_resolution: "yes" | "no" | "ambiguous"` (não mais `pending_resolved: bool`).

- `"yes"` → injeta synthetic tool step para auto-salvar `pending_value`
- `"no"` → ignora o pending para tool calls (continua conversa normal)
- `"ambiguous"` → NÃO salva; injeta `synthesis_instruction` com `confirmation_question` para o LLM gerar a pergunta de confirmação; pending permanece ativo

**Why**: Previne que respostas de baixa confiança sejam auto-salvas (ex: "Maria" sendo capturado como nome quando o usuário estava respondendo outra coisa).

**How to apply**: `confirmation_question` vem do próprio LLM no JSON do `understand()`. O orchestrator usa esse campo como instrução para `synthesize()`.
