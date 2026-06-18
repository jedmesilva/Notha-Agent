import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import os

from agent.agent import Agent
from tools.registry import registry
from tools.builtin.datetime_tool import DateTimeTool
from tools.builtin.web_search import WebSearchTool
from tools.builtin.math_tool import MathTool
from whatsapp import send_message, extract_messages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("notha")

agent: Agent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent

    registry.register(DateTimeTool())
    registry.register(WebSearchTool())
    registry.register(MathTool())

    agent = Agent(registry=registry)
    logger.info("Notha iniciado e pronto para receber mensagens do WhatsApp.")
    yield


app = FastAPI(title="Notha", version="2.0.0", lifespan=lifespan)


async def process_message(phone: str, text: str) -> None:
    if text.lower() in ("/reset", "/limpar", "/clear"):
        await agent.reset(phone)
        try:
            await send_message(phone, "Conversa reiniciada! Como posso te ajudar?")
        except Exception as e:
            logger.error(f"Erro ao enviar reset para {phone}: {e}")
        return

    try:
        reply = await agent.run(phone, text)
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
    """Testa o agente diretamente sem enviar mensagem pelo WhatsApp."""
    if body.message.lower() in ("/reset", "/limpar", "/clear"):
        await agent.reset(body.phone)
        return {"reply": "Conversa reiniciada!"}

    reply = await agent.run(body.phone, body.message)
    return {"reply": reply, "phone": body.phone}


@app.get("/health")
async def health() -> dict:
    tools = list(registry._tools.keys())
    return {"status": "ok", "agent": "Notha", "tools": tools}
