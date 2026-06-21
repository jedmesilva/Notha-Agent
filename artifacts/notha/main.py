import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from db.connection import init_pool, close_pool
from engine.orchestrator import Orchestrator
from engine.jobs import start_all_jobs
from whatsapp import send_message, extract_messages, download_media_bytes
from transcribe import transcribe_audio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("notha")

orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator

    await init_pool()

    orchestrator = Orchestrator()

    await start_all_jobs()

    logger.info("NOTHA started and ready to receive WhatsApp messages.")
    yield

    await close_pool()
    logger.info("NOTHA shut down.")


app = FastAPI(title="NOTHA", version="2.0.0", lifespan=lifespan)


async def process_message(phone: str, text: str) -> None:
    if text.lower() in ("/reset", "/limpar", "/clear"):
        await orchestrator.reset(phone)
        try:
            await send_message(phone, "Conversa reiniciada! Como posso te ajudar?")
        except Exception as e:
            logger.error(f"Error sending reset to {phone}: {e}")
        return

    try:
        reply = await orchestrator.handle_message(phone, text, send_fn=send_message)
        await send_message(phone, reply)
        logger.info(f"Reply sent to {phone}.")
    except Exception as e:
        logger.error(f"Error processing message from {phone}: {e}")
        try:
            await send_message(phone, "Desculpe, ocorreu um erro. Tente novamente em instantes.")
        except Exception:
            pass


async def process_audio_message(phone: str, media_id: str, mime_type: str) -> None:
    """Downloads audio from WhatsApp, transcribes via Whisper and processes as text."""
    try:
        await send_message(phone, "🎙️ Recebi seu áudio, transcrevendo...")
    except Exception:
        pass

    try:
        audio_bytes, detected_mime = await download_media_bytes(media_id)
        if not audio_bytes:
            logger.error(f"Failed to download audio from {phone} (media_id={media_id})")
            await send_message(phone, "Não consegui processar seu áudio. Pode tentar enviar como texto?")
            return

        effective_mime = detected_mime or mime_type
        transcribed = await transcribe_audio(audio_bytes, effective_mime)

        if not transcribed:
            logger.warning(f"Empty transcription for audio from {phone}")
            await send_message(phone, "Não consegui entender o áudio. Pode tentar enviar como texto?")
            return

        logger.info(f"Audio transcribed from {phone}: {transcribed[:80]}...")

        reply = await orchestrator.handle_message(phone, transcribed, send_fn=send_message)
        await send_message(phone, reply)
        logger.info(f"Audio reply sent to {phone}.")

    except Exception as e:
        logger.error(f"Error processing audio from {phone}: {e}")
        try:
            await send_message(phone, "Ocorreu um erro ao processar seu áudio. Tente enviar como texto.")
        except Exception:
            pass


async def process_media(phone: str, media_id: str, mime_type: str, caption: str) -> None:
    """
    Routes received image/document.

    Priority:
    1. If there is an active product listing flow at the 'fotos' step → routes to listing flow
    2. Otherwise → treats as identity document
    """
    try:
        reply = await orchestrator.handle_media(
            phone=phone,
            media_id=media_id,
            mime_type=mime_type,
            caption=caption,
        )
        if reply:
            await send_message(phone, reply)
        logger.info(f"Media processed for {phone}.")
    except Exception as e:
        logger.error(f"Error processing media from {phone}: {e}")
        try:
            await send_message(
                phone,
                "Recebi sua imagem, mas tive um problema técnico ao processá-la. Tenta enviar de novo em instantes.",
            )
        except Exception:
            pass


@app.get("/webhook")
async def verify_webhook(request: Request) -> PlainTextResponse:
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == os.environ.get("WHATSAPP_VERIFY_TOKEN", ""):
        logger.info("Webhook verified successfully.")
        return PlainTextResponse(content=challenge)

    logger.warning("Webhook verification failed.")
    raise HTTPException(status_code=403, detail="Token de verificação inválido")


@app.post("/webhook")
async def receive_message(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    messages = extract_messages(body)
    for msg in messages:
        msg_id = msg.get("id", "")
        from engine.orchestrator import PROCESSED_MESSAGE_IDS, MAX_PROCESSED_IDS
        if msg_id and msg_id in PROCESSED_MESSAGE_IDS:
            logger.info(f"Duplicate message ignored: {msg_id}")
            continue
        if msg_id:
            PROCESSED_MESSAGE_IDS.add(msg_id)
            if len(PROCESSED_MESSAGE_IDS) > MAX_PROCESSED_IDS:
                try:
                    PROCESSED_MESSAGE_IDS.pop()
                except KeyError:
                    pass

        phone = msg["from"]
        msg_type = msg.get("type", "text")

        if msg_type == "text":
            text = msg["text"].strip()
            logger.info(f"Message from {phone}: {text[:60]}...")
            asyncio.create_task(process_message(phone, text))

        elif msg_type == "audio":
            media_id = msg.get("media_id")
            if media_id:
                mime = msg.get("media_mime_type", "audio/ogg")
                logger.info(f"Audio received from {phone}: media_id={media_id} mime={mime}")
                asyncio.create_task(
                    process_audio_message(
                        phone=phone,
                        media_id=media_id,
                        mime_type=mime,
                    )
                )

        elif msg_type in ("image", "document"):
            media_id = msg.get("media_id")
            if media_id:
                logger.info(f"Media received from {phone}: type={msg_type} media_id={media_id}")
                asyncio.create_task(
                    process_media(
                        phone=phone,
                        media_id=media_id,
                        mime_type=msg.get("media_mime_type", "image/jpeg"),
                        caption=msg.get("caption", ""),
                    )
                )

    return Response(status_code=200)


@app.post("/webhook/asaas")
async def asaas_webhook(request: Request) -> Response:
    """Asaas webhook for payment and transfer confirmations."""
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    event = body.get("event", "")
    payment = body.get("payment", {})
    logger.info(f"Asaas event received: {event} — charge_id={payment.get('id')}")

    if event in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"):
        asyncio.create_task(_handle_payment_confirmed(payment))

    return Response(status_code=200)


async def _handle_payment_confirmed(payment: dict) -> None:
    """Marks transaction as paid and notifies parties."""
    from db.connection import get_db
    from db.repositories import TransactionRepository, NegotiationRepository
    db = get_db()
    if not db:
        return

    charge_id = payment.get("id")
    if not charge_id:
        return

    tx_repo = TransactionRepository(db)
    neg_repo = NegotiationRepository(db)

    row = await db.fetch_one(
        "SELECT * FROM transactions WHERE asaas_charge_id = $1", charge_id
    )
    if not row:
        logger.warning(f"Charge {charge_id} not found in database.")
        return

    await tx_repo.set_paid(row["id"])
    await neg_repo.set_status(row["negotiation_id"], "paga")
    logger.info(f"Payment confirmed: transaction_id={row['id']}, negotiation_id={row['negotiation_id']}")


class TestMessage(BaseModel):
    message: str
    phone: str = "test_user"


@app.post("/test")
async def test_chat(body: TestMessage) -> dict:
    """Tests the orchestrator directly without sending a WhatsApp message."""
    if body.message.lower() in ("/reset", "/limpar", "/clear"):
        await orchestrator.reset(body.phone)
        return {"reply": "Conversa reiniciada!"}

    reply = await orchestrator.handle_message(body.phone, body.message)
    return {"reply": reply, "phone": body.phone}


@app.get("/health")
async def health() -> dict:
    from db.connection import get_pool
    from db.repositories import TransactionRepository
    db_ok = get_pool() is not None

    saldo_retido = None
    if db_ok:
        try:
            from db.connection import get_db
            db = get_db()
            if db:
                tx_repo = TransactionRepository(db)
                saldo_retido = await tx_repo.get_total_retained()
        except Exception:
            pass

    return {
        "status": "ok",
        "version": "2.0.0",
        "database": "conectado" if db_ok else "desconectado",
        "saldo_retido_total": saldo_retido,
    }


@app.get("/admin/listings")
async def list_listings(status: str = "disponivel", limit: int = 20) -> dict:
    """Admin endpoint: lists products by status."""
    from db.connection import get_db
    from db.repositories import ListingRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    repo = ListingRepository(db)
    rows = await repo.find_available(limit=limit) if status == "disponivel" else []
    return {"listings": [dict(r) for r in rows], "total": len(rows)}


@app.get("/admin/conciliacao")
async def conciliacao() -> dict:
    """Admin endpoint: total retained balance for financial reconciliation."""
    from db.connection import get_db
    from db.repositories import TransactionRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    tx_repo = TransactionRepository(db)
    total = await tx_repo.get_total_retained()
    return {
        "saldo_retido_total_notha": total,
        "nota": "Compare este valor com o saldo real da conta Asaas da MAISOR CAPITAL. Divergência é bug crítico.",
    }


@app.get("/admin/identidade/pendentes")
async def identidade_pendentes(limit: int = 50) -> dict:
    """Admin endpoint: lists identity documents pending review."""
    from db.connection import get_db
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    rows = await db.fetch_all(
        """
        SELECT d.id, d.user_id, d.tipo, d.url_imagem, d.status,
               d.criado_em, d.whatsapp_media_id,
               u.nome, u.apelido, u.cpf
        FROM documentos_identidade d
        JOIN users u ON u.id = d.user_id
        WHERE d.status = 'em_analise'
        ORDER BY d.criado_em ASC
        LIMIT $1
        """,
        limit,
    )
    return {"pendentes": [dict(r) for r in rows], "total": len(rows)}


@app.post("/admin/identidade/{doc_id}/aprovar")
async def aprovar_documento(doc_id: int) -> dict:
    """Admin endpoint: approves a document and marks the user as verified."""
    from db.connection import get_db
    from db.repositories import UserRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    doc = await db.fetch_one("SELECT * FROM documentos_identidade WHERE id = $1", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    from datetime import datetime
    await db.execute(
        "UPDATE documentos_identidade SET status='aprovado', analisado_em=$1, analisado_por='admin' WHERE id=$2",
        datetime.utcnow(), doc_id,
    )
    user_repo = UserRepository(db)
    await user_repo.update_identidade_status(doc["user_id"], "verificado")

    return {"ok": True, "doc_id": doc_id, "user_id": doc["user_id"]}


@app.post("/admin/identidade/{doc_id}/rejeitar")
async def rejeitar_documento(doc_id: int, motivo: str = "") -> dict:
    """Admin endpoint: rejects a document and marks the user as rejected."""
    from db.connection import get_db
    from db.repositories import UserRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    doc = await db.fetch_one("SELECT * FROM documentos_identidade WHERE id = $1", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    from datetime import datetime
    await db.execute(
        """
        UPDATE documentos_identidade
        SET status='rejeitado', motivo_rejeicao=$1, analisado_em=$2, analisado_por='admin'
        WHERE id=$3
        """,
        motivo, datetime.utcnow(), doc_id,
    )
    user_repo = UserRepository(db)
    await user_repo.update_identidade_status(doc["user_id"], "rejeitado")

    return {"ok": True, "doc_id": doc_id, "user_id": doc["user_id"], "motivo": motivo}
