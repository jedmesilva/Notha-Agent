---
name: NOTHA OpenAI lazy initialization
description: AsyncOpenAI deve ser inicializado de forma lazy para não crashar na startup sem OPENAI_API_KEY
---

**Rule:** Nunca instanciar `AsyncOpenAI` no `__init__` de uma classe. Usar pattern `_get_client()` que instancia na primeira chamada real.

**Why:** O servidor faz startup antes das env vars serem lidas corretamente em alguns ambientes (Railway, Replit). Instanciar no `__init__` lança `OpenAIError: api_key must be set` e impede o servidor de subir, mesmo quando o banco de dados conecta normalmente.

**How to apply:**
```python
class MinhaClasse:
    def __init__(self):
        self._client = None  # não instanciar aqui

    def _get_client(self):
        if self._client is None:
            self._client = AsyncOpenAI(api_key=...)
        return self._client
```
Aplicado em: `agents/conversation.py`, `agents/pricing.py`, `agents/proxy.py` (SellerProxy, BuyerProxy, DeliveryProxy).
