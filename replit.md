# Notha

Agente de IA disponível via WhatsApp — integra a Meta WhatsApp Cloud API com GPT-5.4 via Replit AI Integrations.

## Run & Operate

- `cd artifacts/notha && uvicorn main:app --host 0.0.0.0 --port 8000 --reload` — iniciar o servidor Notha
- Workflow: **Notha (WhatsApp Agent)** — processo gerenciado pelo Replit

## Stack

- Python 3.11 + FastAPI + Uvicorn
- LLM: OpenAI GPT-5.4 via Replit AI Integrations (sem chave própria)
- WhatsApp: Meta WhatsApp Cloud API v21 (webhook)

## Where things live

- `artifacts/notha/main.py` — entrypoint FastAPI, rotas /webhook e /health
- `artifacts/notha/whatsapp.py` — cliente HTTP para envio de mensagens e parsing de payloads
- `artifacts/notha/llm.py` — integração com OpenAI via Replit AI Integrations
- `artifacts/notha/conversation.py` — histórico de conversa em memória por número de telefone

## Architecture decisions

- Histórico de conversa em memória (defaultdict por número de telefone), limitado a 20 mensagens por usuário.
- Webhook unico `/webhook` com GET para verificação e POST para recebimento de mensagens.
- LLM chamado de forma assíncrona (AsyncOpenAI) para não bloquear o event loop.
- Comando `/reset` (ou `/limpar`) reinicia o histórico do usuário.

## Product

O Notha é um agente de IA acessível pelo WhatsApp. O usuário envia uma mensagem, o Notha responde usando GPT-5.4. Suporta contexto de conversa por sessão e comando de reset.

## Secrets necessários

- `WHATSAPP_ACCESS_TOKEN` — token de acesso Meta (WhatsApp > API Setup no Meta for Developers)
- `WHATSAPP_PHONE_NUMBER_ID` — ID do número de telefone (mesma página)
- `WHATSAPP_VERIFY_TOKEN` — texto livre escolhido por você; usado na verificação do webhook

## Como configurar o webhook na Meta

1. Acesse [developers.facebook.com](https://developers.facebook.com) → seu app → WhatsApp → Configuration
2. Em **Webhook**, clique em "Edit"
3. **Callback URL**: `https://<SEU_DOMINIO>/webhook`
4. **Verify token**: o mesmo valor que você definiu em `WHATSAPP_VERIFY_TOKEN`
5. Inscreva-se no campo `messages`

## Gotchas

- O domínio público do Replit é `https://<REPL_SLUG>.<USERNAME>.repl.co` ou o domínio custom configurado.
- Para produção, o token de acesso permanente precisa ser gerado via Meta Business Manager.
- Histórico é perdido se o servidor reiniciar — para persistência, adicionar banco de dados.
