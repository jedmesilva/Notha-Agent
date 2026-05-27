import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse

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


@app.get("/webhook")
async def verify_webhook(request: Request) -> PlainTextResponse:
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

    if mode == "subscribe" and token == verify_token:
        logger.info("Webhook verificado com sucesso.")
        return PlainTextResponse(content=challenge)

    logger.warning("Falha na verificação do webhook.")
    raise HTTPException(status_code=403, detail="Token de verificação inválido")


@app.post("/webhook")
async def receive_message(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corpo da requisição inválido")

    messages = extract_messages(body)

    for msg in messages:
        phone = msg["from"]
        text = msg["text"].strip()

        logger.info(f"Mensagem recebida de {phone}: {text[:50]}...")

        if text.lower() in ("/reset", "/limpar", "/clear"):
            clear_history(phone)
            await send_message(phone, "Conversa reiniciada! Como posso te ajudar?")
            continue

        add_message(phone, "user", text)
        history = get_history(phone)

        try:
            reply = await chat(history)
            add_message(phone, "assistant", reply)
            await send_message(phone, reply)
            logger.info(f"Resposta enviada para {phone}.")
        except Exception as e:
            logger.error(f"Erro ao processar mensagem de {phone}: {e}")
            await send_message(
                phone,
                "Desculpe, ocorreu um erro ao processar sua mensagem. Tente novamente.",
            )

    return Response(status_code=200)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "Notha"}
