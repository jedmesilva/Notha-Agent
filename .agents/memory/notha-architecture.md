---
name: NOTHA multi-agent architecture
description: Princípio central de guard rails e separação de responsabilidades entre LLM e código determinístico
---

**Rule:** Toda decisão sobre valor monetário é validada por código determinístico antes de qualquer execução. LLMs propõem dentro de limites declarados pelos humanos — nunca executam.

**Why:** Decisão monetária precisa ser auditável e repetível. Mesma entrada deve sempre produzir mesma saída. LLM tem variabilidade por natureza.

**How to apply:**
- `_validate_proxy_response()` em `agents/proxy.py` — chamado após toda saída de proxy antes de usar o valor
- `NegotiationEngine` em `engine/negotiation.py` — lógica determinística pura para modo direto
- `AsaasClient` em `asaas.py` — única camada que toca dinheiro real, chamada apenas pelo `TransactionRepository` ou após confirmação mútua de entrega
- Contextual Evaluator (LLM dentro do NegotiationEngine) tem teto fixo `AJUSTE_MAXIMO_PERMITIDO` aplicado em código antes de qualquer uso
