# NOTHA

Agente de negociação de produtos físicos via WhatsApp.
Integra Meta WhatsApp Cloud API + GPT via OpenAI + Supabase (PostgreSQL) + Asaas (pagamentos).
Hospedado no Railway.

## Run & Operate

```
cd artifacts/notha && uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Workflow Replit: **artifacts/api-server: Notha**

## Stack

- Python 3.11 + FastAPI + Uvicorn
- LLM: OpenAI (lazy init, compatível com Replit AI Integrations)
- WhatsApp: Meta WhatsApp Cloud API v21 (webhook)
- Banco: PostgreSQL via asyncpg (Supabase)
- Pagamentos: Asaas (Pix — sem subconta/KYC de terceiros)

## Estrutura de arquivos

```
artifacts/notha/
├── main.py                    # Entrypoint FastAPI: /webhook, /test, /health, /admin/*
├── config.py                  # Todas as variáveis de ambiente/configuração
├── whatsapp.py                # Cliente Meta WhatsApp Cloud API
├── asaas.py                   # Cliente Asaas (cobranças, transferências Pix, estornos)
│
├── db/
│   ├── connection.py          # Pool asyncpg + wrapper DB
│   ├── schema.sql             # Schema completo — APLICAR NO SUPABASE
│   └── repositories/
│       ├── users.py           # users, user_phone_numbers, seller/buyer/courier_profile
│       ├── listings.py        # listings, interest_queue
│       ├── negotiations.py    # negotiations, negotiation_offers, proxy_negotiation_rounds
│       ├── transactions.py    # transactions
│       └── delivery.py        # delivery_confirmations
│
├── agents/
│   ├── conversation.py        # Conversation Agent (LLM — interface com humanos)
│   ├── pricing.py             # Pricing/Appraisal Agent (LLM + web search + vision)
│   ├── proxy.py               # Buyer/Seller/Delivery Proxy Agents (LLM + guard rails)
│   └── logistics.py           # Logistics Agent (coordena entrega/retirada)
│
└── engine/
    ├── negotiation.py         # Negotiation Engine (lógica determinística + proxies)
    ├── orchestrator.py        # Orquestrador (roteamento central de mensagens)
    └── jobs.py                # Jobs periódicos (timeouts, retiradas, estornos)
```

## Segredos necessários

| Variável | Descrição |
|---|---|
| `DATABASE_URL` | Connection string PostgreSQL do Supabase (com `?sslmode=require`) |
| `WHATSAPP_ACCESS_TOKEN` | Token de acesso Meta (WhatsApp > API Setup) |
| `WHATSAPP_PHONE_NUMBER_ID` | ID do número de telefone Meta |
| `WHATSAPP_VERIFY_TOKEN` | Texto livre para verificação do webhook |
| `OPENAI_API_KEY` | Chave OpenAI (ou usar OPENAI_BASE_URL para Replit AI) |
| `ASAAS_API_KEY` | Chave da conta Asaas da MAISOR CAPITAL |
| `ASAAS_BASE_URL` | `https://sandbox.asaas.com/api/v3` (sandbox) ou `https://api.asaas.com/api/v3` (prod) |

## Configurar o banco (Supabase)

1. No Supabase, vá em **SQL Editor**
2. Cole o conteúdo de `artifacts/notha/db/schema.sql`
3. Execute o script
4. Pegue a connection string em **Project Settings > Database > Connection string > URI**
5. Defina `DATABASE_URL` no Railway (com `?sslmode=require` ao final)

## Configurar o webhook na Meta

1. [developers.facebook.com](https://developers.facebook.com) → seu app → WhatsApp → Configuration
2. **Callback URL**: `https://<SEU_DOMINIO_RAILWAY>/webhook`
3. **Verify token**: valor de `WHATSAPP_VERIFY_TOKEN`
4. Inscreva-se no campo `messages`

## Webhook do Asaas

Registre `https://<SEU_DOMINIO_RAILWAY>/webhook/asaas` no painel Asaas para receber confirmações de pagamento (`PAYMENT_RECEIVED`, `PAYMENT_CONFIRMED`).

## Endpoints

| Método | Path | Descrição |
|---|---|---|
| GET | `/webhook` | Verificação do webhook Meta |
| POST | `/webhook` | Recebimento de mensagens WhatsApp |
| POST | `/webhook/asaas` | Confirmação de pagamentos Asaas |
| POST | `/test` | Teste direto do orquestrador |
| GET | `/health` | Status + saldo retido total |
| GET | `/admin/listings` | Lista produtos disponíveis |
| GET | `/admin/conciliacao` | Saldo retido para conciliação |

## Arquitetura de agentes

| Componente | Tipo | Decide dinheiro? |
|---|---|---|
| Conversation Agent | LLM | Não |
| Pricing/Appraisal Agent | LLM + web search + vision | Não (sugere, vendedor confirma) |
| Negotiation Engine | Código determinístico | Sim |
| Buyer/Seller Proxy Agent | LLM com guard rails | Propõe, código valida |
| Logistics/Delivery Agent | LLM + Delivery Proxy | Propõe entrega, código valida |
| Transaction Agent (asaas.py) | Código determinístico | Executa via Asaas |
| Orquestrador | Código de roteamento | Não |

## Princípio arquitetural central

> Conversação é responsabilidade de LLM. Decisão sobre dinheiro é responsabilidade de código determinístico.

LLMs propõem valores dentro de guard rails — nunca executam ações financeiras diretamente.

## Empresa operadora

MAISOR CAPITAL LTDA — apenas esta empresa tem KYC no Asaas.
Vendedores, compradores e entregadores recebem via chave Pix de qualquer banco, sem onboarding bancário.

## User preferences

- Português brasileiro em todo o código e documentação
