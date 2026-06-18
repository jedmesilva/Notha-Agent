import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from conversation import add_message, get_history, clear_history
from whatsapp import send_message, extract_messages
from llm import chat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("notha")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Notha iniciado e pronto para receber mensagens do WhatsApp.")
    yield


app = FastAPI(title="Notha", version="1.0.0", lifespan=lifespan)


async def process_message(phone: str, text: str) -> None:
    if text.lower() in ("/reset", "/limpar", "/clear"):
        clear_history(phone)
        try:
            await send_message(phone, "Conversa reiniciada! Como posso te ajudar?")
        except Exception as e:
            logger.error(f"Erro ao enviar reset para {phone}: {e}")
        return

    add_message(phone, "user", text)
    try:
        reply = "✅ Notha recebeu sua mensagem! O agente está funcionando."
        await send_message(phone, reply)
        logger.info(f"Resposta enviada para {phone}.")
    except Exception as e:
        logger.error(f"Erro ao processar mensagem de {phone}: {e}")
        try:
            await send_message(phone, "Desculpe, ocorreu um erro. Tente novamente.")
        except Exception:
            pass


@app.get("/webhook")
async def verify_webhook(request: Request) -> PlainTextResponse:
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == os.environ.get("WHATSAPP_VERIFY_TOKEN", ""):
        logger.info("Webhook verificado com sucesso.")
        return PlainTextResponse(content=challenge)

    logger.warning("Falha na verificação do webhook.")
    raise HTTPException(status_code=403, detail="Token de verificação inválido")


@app.post("/webhook")
async def receive_message(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    messages = extract_messages(body)
    for msg in messages:
        logger.info(f"Mensagem recebida de {msg['from']}: {msg['text'][:50]}...")
        asyncio.create_task(process_message(msg["from"], msg["text"].strip()))

    return Response(status_code=200)


class TestMessage(BaseModel):
    message: str
    phone: str = "test_user"


@app.post("/test")
async def test_chat(body: TestMessage) -> dict:
    """Testa o LLM diretamente sem enviar mensagem pelo WhatsApp."""
    if body.message.lower() in ("/reset", "/limpar", "/clear"):
        clear_history(body.phone)
        return {"reply": "Conversa reiniciada!"}

    add_message(body.phone, "user", body.message)
    reply = await chat(get_history(body.phone))
    add_message(body.phone, "assistant", reply)
    return {"reply": reply, "phone": body.phone}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "Notha"}
