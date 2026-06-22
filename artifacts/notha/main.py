import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from db.connection import init_pool, close_pool, get_pool
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

# Per-phone locks — prevent concurrent processing of messages from the same number.
# If a previous message is still being processed, new ones queue behind the lock.
_PHONE_LOCKS: dict[str, asyncio.Lock] = {}


def _phone_lock(phone: str) -> asyncio.Lock:
    if phone not in _PHONE_LOCKS:
        _PHONE_LOCKS[phone] = asyncio.Lock()
    return _PHONE_LOCKS[phone]


async def _migrate_phone_info_columns() -> None:
    """Adds phone-info columns to user_phone_numbers if they don't exist yet.

    Safe to run on every startup — each statement uses IF NOT EXISTS / DO NOTHING.
    Required because the table already exists in production; ALTER TABLE is used
    instead of CREATE TABLE so existing rows are preserved.
    """
    pool = get_pool()
    if not pool:
        return
    columns = [
        ("country_code", "SMALLINT"),
        ("country_iso",  "VARCHAR(2)"),
        ("country_name", "VARCHAR(100)"),
        ("region",       "VARCHAR(150)"),
        ("carrier",      "VARCHAR(100)"),
        ("timezone",     "VARCHAR(60)"),
        ("number_type",  "VARCHAR(30)"),
        ("is_valid",     "BOOLEAN"),
        ("parsed_at",    "TIMESTAMPTZ"),
    ]
    async with pool.acquire() as conn:
        for col, col_type in columns:
            await conn.execute(
                f"ALTER TABLE user_phone_numbers ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )
    logger.info("Phone info columns ready in user_phone_numbers.")


async def _init_webhook_dedup_table() -> None:
    """Creates the webhook dedup table if it doesn't exist.

    Stores processed WhatsApp message IDs for 2 hours so that Meta webhook
    retries (which happen when the server restarts mid-delivery) are safely
    ignored without re-processing the same message twice.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhook_msgs (
                msg_id      TEXT        PRIMARY KEY,
                processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pwm_processed_at
            ON processed_webhook_msgs (processed_at)
        """)
    logger.info("Webhook dedup table ready.")


async def _is_duplicate_webhook(msg_id: str) -> bool:
    """Returns True if this msg_id was already processed (persisted in DB).

    Also inserts the ID so future calls return True for the same ID.
    Uses ON CONFLICT DO NOTHING so the check+insert is atomic.
    """
    pool = get_pool()
    if not pool or not msg_id:
        return False
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO processed_webhook_msgs (msg_id) VALUES ($1) ON CONFLICT DO NOTHING",
            msg_id,
        )
        # If 0 rows inserted → already existed → duplicate
        inserted = result.split()[-1] if result else "0"
        return inserted == "0"


async def _cleanup_old_webhook_ids() -> None:
    """Removes webhook IDs older than 2 hours (run periodically by jobs)."""
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        deleted = await conn.execute(
            "DELETE FROM processed_webhook_msgs WHERE processed_at < NOW() - INTERVAL '2 hours'"
        )
        count = deleted.split()[-1] if deleted else "0"
        if count != "0":
            logger.info("Cleaned up %s old webhook dedup entries.", count)


async def _webhook_dedup_cleanup_loop() -> None:
    """Runs _cleanup_old_webhook_ids every hour."""
    while True:
        await asyncio.sleep(3600)
        try:
            await _cleanup_old_webhook_ids()
        except Exception as e:
            logger.error("Webhook dedup cleanup failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator

    await init_pool()
    await _migrate_phone_info_columns()
    await _init_webhook_dedup_table()

    orchestrator = Orchestrator()

    await start_all_jobs()
    asyncio.create_task(_webhook_dedup_cleanup_loop())

    logger.info("NOTHA started and ready to receive WhatsApp messages.")
    yield

    await close_pool()
    logger.info("NOTHA shut down.")


app = FastAPI(title="NOTHA", version="2.0.0", lifespan=lifespan)


async def process_message(phone: str, text: str) -> None:
    async with _phone_lock(phone):
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
    async with _phone_lock(phone):
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
    """Routes received image/document.

    Priority:
    1. If there is an active product listing flow at the 'photos_upload' step → routes to listing flow
    2. Otherwise → treats as identity document
    """
    async with _phone_lock(phone):
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

        # DB-based deduplication — survives server restarts (unlike in-memory set).
        # Meta retries webhook delivery when it doesn't get a 200 quickly enough
        # (e.g. during server reload). The DB check+insert is atomic so concurrent
        # requests for the same msg_id are also handled safely.
        if msg_id and await _is_duplicate_webhook(msg_id):
            logger.info(f"Duplicate webhook ignored (msg_id={msg_id})")
            continue

        phone    = msg["from"]
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

    event   = body.get("event", "")
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

    tx_repo  = TransactionRepository(db)
    neg_repo = NegotiationRepository(db)

    row = await db.fetch_one(
        "SELECT * FROM transactions WHERE asaas_charge_id = $1", charge_id
    )
    if not row:
        logger.warning(f"Charge {charge_id} not found in database.")
        return

    await tx_repo.set_paid(row["id"])
    await neg_repo.update_status(row["negotiation_id"], "paid")
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

    retained = None
    if db_ok:
        try:
            from db.connection import get_db
            db = get_db()
            if db:
                tx_repo  = TransactionRepository(db)
                retained = await tx_repo.get_total_retained()
        except Exception:
            pass

    return {
        "status":            "ok",
        "version":           "2.0.0",
        "database":          "conectado" if db_ok else "desconectado",
        "saldo_retido_total": retained,
    }


@app.get("/admin/listings")
async def list_listings(status: str = "available", limit: int = 20) -> dict:
    """Admin endpoint: lists products by status."""
    from db.connection import get_db
    from db.repositories import ListingRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    repo = ListingRepository(db)
    rows = await repo.find_available(limit=limit) if status == "available" else []
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
    total   = await tx_repo.get_total_retained()
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
        SELECT d.id, d.user_id, d.doc_type, d.image_url, d.status,
               d.created_at, d.whatsapp_media_id,
               u.full_name, u.nickname, u.tax_id
        FROM identity_documents d
        JOIN users u ON u.id = d.user_id
        WHERE d.status = 'under_review'
        ORDER BY d.created_at ASC
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

    doc = await db.fetch_one("SELECT * FROM identity_documents WHERE id = $1", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    from datetime import datetime
    await db.execute(
        "UPDATE identity_documents SET status='approved', reviewed_at=$1, reviewed_by='admin' WHERE id=$2",
        datetime.utcnow(), doc_id,
    )
    user_repo = UserRepository(db)
    await user_repo.update_identity_status(doc["user_id"], "verified")

    return {"ok": True, "doc_id": doc_id, "user_id": doc["user_id"]}


@app.post("/admin/identidade/{doc_id}/rejeitar")
async def rejeitar_documento(doc_id: int, motivo: str = "") -> dict:
    """Admin endpoint: rejects a document and marks the user as rejected."""
    from db.connection import get_db
    from db.repositories import UserRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    doc = await db.fetch_one("SELECT * FROM identity_documents WHERE id = $1", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    from datetime import datetime
    await db.execute(
        """
        UPDATE identity_documents
        SET status='rejected', rejection_reason=$1, reviewed_at=$2, reviewed_by='admin'
        WHERE id=$3
        """,
        motivo, datetime.utcnow(), doc_id,
    )
    user_repo = UserRepository(db)
    await user_repo.update_identity_status(doc["user_id"], "rejected")

    return {"ok": True, "doc_id": doc_id, "user_id": doc["user_id"], "motivo": motivo}
