import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from db.connection import init_pool, close_pool, get_pool
from engine.orchestrator import Orchestrator, localize
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


async def _migrate_user_profile_columns() -> None:
    """Adiciona colunas de perfil estendido à tabela users se ainda não existirem.

    Seguro para executar a cada startup — usa IF NOT EXISTS para cada ALTER TABLE.
    Colunas: gender, date_of_birth, preferred_language, street, street_number,
             state, country, zip_code.
    """
    pool = get_pool()
    if not pool:
        return
    columns = [
        ("gender",             "VARCHAR(20)"),
        ("date_of_birth",      "DATE"),
        ("preferred_language", "VARCHAR(10)"),
        ("street",             "VARCHAR(200)"),
        ("street_number",      "VARCHAR(30)"),
        ("state",              "VARCHAR(100)"),
        ("country",            "VARCHAR(100)"),
        ("zip_code",           "VARCHAR(20)"),
    ]
    async with pool.acquire() as conn:
        for col, col_type in columns:
            await conn.execute(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )
    logger.info("User profile columns ready in users table.")


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


async def _migrate_sessions_tables() -> None:
    """Creates sessions and pending_verifications tables if they don't exist yet."""
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id               SERIAL PRIMARY KEY,
                user_id          INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                phone            VARCHAR(20) NOT NULL,
                status           VARCHAR(20) NOT NULL DEFAULT 'active',
                reauth_tier      VARCHAR(20),
                reauth_attempts  INT NOT NULL DEFAULT 0,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reauthed_at      TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_phone_live
                ON sessions(phone)
                WHERE status IN ('active', 'pending_reauth')
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_phone ON sessions(phone)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions(user_id)")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_verifications (
                id         SERIAL PRIMARY KEY,
                session_id INT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                user_id    INT NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
                phone      VARCHAR(20) NOT NULL,
                token      TEXT NOT NULL,
                status     VARCHAR(20) NOT NULL DEFAULT 'pending',
                result     JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pv_token ON pending_verifications(token)")
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pv_phone_pending
                ON pending_verifications(phone)
                WHERE status = 'pending'
        """)
    logger.info("Sessions tables ready.")


async def _migrate_pending_confirmations_table() -> None:
    """Creates the pending_confirmations table if it doesn't exist yet.

    Persistent replacement for the in-memory PENDING_CONFIRMATIONS dict.
    Stores one row per phone — the business confirmation NOTHA is awaiting
    (e.g. the seller confirming a suggested listing price). Expires after
    2 hours if never resolved.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_confirmations (
                phone      VARCHAR(20) PRIMARY KEY,
                type       VARCHAR(100) NOT NULL,
                data       JSONB        NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '2 hours'
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_confirmations_expires
            ON pending_confirmations (expires_at)
        """)
    logger.info("Pending confirmations table ready.")


async def _migrate_turn_state_table() -> None:
    """Creates the turn_state table if it doesn't exist yet.

    Stores one row per phone — the pending field/question NOTHA asked in
    the previous turn, waiting for the user's reply. Expires after 30 min
    by default. This is the fix for the 'Oi captured as name' class of bugs.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_state (
                phone         VARCHAR(20) PRIMARY KEY,
                pending_field VARCHAR(100) NOT NULL,
                operation     VARCHAR(100) NOT NULL DEFAULT '',
                context_data  JSONB        NOT NULL DEFAULT '{}',
                asked_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                expires_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '30 minutes',
                attempt_count INTEGER      NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_state_expires
            ON turn_state (expires_at)
        """)
        await conn.execute(
            "ALTER TABLE turn_state ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0"
        )
    logger.info("Turn state table ready.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator

    await init_pool()
    await _migrate_user_profile_columns()
    await _migrate_phone_info_columns()
    await _init_webhook_dedup_table()
    await _migrate_sessions_tables()
    await _migrate_pending_confirmations_table()
    await _migrate_turn_state_table()

    # Pluggy — Open Finance
    from pluggy_flow import init_pluggy_tables
    await init_pluggy_tables()

    orchestrator = Orchestrator()

    await start_all_jobs()
    asyncio.create_task(_webhook_dedup_cleanup_loop())

    logger.info("NOTHA started and ready to receive WhatsApp messages.")
    yield

    await close_pool()
    logger.info("NOTHA shut down.")


app = FastAPI(title="NOTHA", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


async def process_message(phone: str, text: str) -> None:
    async with _phone_lock(phone):
        if text.lower() in ("/reset", "/limpar", "/clear"):
            await orchestrator.reset(phone)
            try:
                msg = await localize("Conversation reset! How can I help you?", phone)
                await send_message(phone, msg)
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
                msg = await localize("Sorry, an error occurred. Please try again in a moment.", phone)
                await send_message(phone, msg)
            except Exception:
                pass


async def process_audio_message(phone: str, media_id: str, mime_type: str) -> None:
    """Downloads audio from WhatsApp, transcribes via Whisper and processes as text."""
    async with _phone_lock(phone):
        try:
            msg = await localize("🎙️ Audio received, transcribing...", phone)
            await send_message(phone, msg)
        except Exception:
            pass

        try:
            audio_bytes, detected_mime = await download_media_bytes(media_id)
            if not audio_bytes:
                logger.error(f"Failed to download audio from {phone} (media_id={media_id})")
                msg = await localize("I couldn't process your audio. Could you try sending it as text?", phone)
                await send_message(phone, msg)
                return

            effective_mime = detected_mime or mime_type
            transcribed = await transcribe_audio(audio_bytes, effective_mime)

            if not transcribed:
                logger.warning(f"Empty transcription for audio from {phone}")
                msg = await localize("I couldn't understand the audio. Could you try sending it as text?", phone)
                await send_message(phone, msg)
                return

            logger.info(f"Audio transcribed from {phone}: {transcribed[:80]}...")

            reply = await orchestrator.handle_message(phone, transcribed, send_fn=send_message)
            await send_message(phone, reply)
            logger.info(f"Audio reply sent to {phone}.")

        except Exception as e:
            logger.error(f"Error processing audio from {phone}: {e}")
            try:
                msg = await localize("An error occurred processing your audio. Please try sending it as text.", phone)
                await send_message(phone, msg)
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
                msg = await localize(
                    "I received your image but had a technical issue processing it. Please try sending it again in a moment.",
                    phone,
                )
                await send_message(phone, msg)
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
    raise HTTPException(status_code=403, detail="Invalid verification token")


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


@app.get("/verificar/{token}")
async def verification_page(token: str) -> HTMLResponse:
    """Serves the facial verification page for link-based re-auth."""
    html_path = Path(__file__).parent / "static" / "verify.html"
    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="Página não encontrada")
    content = html_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/verificar/{token}/check")
async def verification_check(token: str) -> Response:
    """Returns 200 if the token is still valid, 410 if expired/invalid."""
    from db.connection import get_db
    from db.repositories.sessions import SessionRepository
    db = get_db()
    if not db:
        return Response(status_code=503)
    session_repo = SessionRepository(db)
    pv = await session_repo.get_pending_verification(token)
    if not pv:
        return Response(status_code=410)
    return Response(status_code=200)


@app.post("/verificar/{token}/submit")
async def verification_submit(token: str, request: Request) -> dict:
    """Receives selfie image from the browser, runs server-side face comparison."""
    from db.connection import get_db
    from db.repositories.sessions import SessionRepository
    from agents.auth_user import AuthUserAgent
    import base64

    db = get_db()
    if not db:
        return {"ok": False, "message": "Erro interno — tente novamente."}

    session_repo = SessionRepository(db)

    pv = await session_repo.get_pending_verification(token)
    if not pv:
        return {"ok": False, "message": "Link inválido ou já utilizado. Solicite um novo link no WhatsApp."}

    try:
        body      = await request.json()
        image_b64 = body.get("image", "")
        mime      = body.get("mime", "image/jpeg")
        if not image_b64:
            return {"ok": False, "message": "Imagem não recebida. Tente novamente."}
        media_bytes = base64.b64decode(image_b64)
    except Exception:
        return {"ok": False, "message": "Erro ao processar imagem. Tente novamente."}

    # Load user for face comparison
    from db.repositories.users import UserRepository
    user_repo = UserRepository(db)
    user = await user_repo.find_by_id(pv["user_id"])
    if not user:
        return {"ok": False, "message": "Usuário não encontrado."}

    auth  = AuthUserAgent()
    # Reuse selfie tier logic — passes media_bytes to GPT-4o vision for comparison
    session = await db.fetch_one("SELECT * FROM sessions WHERE id = $1", pv["session_id"])
    session = dict(session) if session else {"id": pv["session_id"], "reauth_attempts": 0}

    ok, reply = await auth._handle_selfie_tier(
        user=dict(user), phone=pv["phone"], session=session,
        session_repo=session_repo, user_repo=user_repo,
        media_bytes=media_bytes, media_mime=mime,
        attempts=session.get("reauth_attempts", 0),
    )

    await session_repo.complete_verification(token, ok, {"source": "link"})

    if ok:
        # send WhatsApp confirmation
        try:
            name = (dict(user).get("nickname") or (dict(user).get("full_name") or "")).split()[0] or "você"
            await send_message(
                pv["phone"],
                f"✅ Verificação facial concluída! Bem-vindo de volta, {name}! Como posso ajudar?",
            )
        except Exception as exc:
            logger.warning("Could not send re-auth confirmation: %s", exc)
        return {"ok": True, "message": "✅ Identidade verificada! Volte ao WhatsApp para continuar."}

    return {"ok": False, "message": reply or "❌ Verificação não concluída. Tente novamente."}


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
    db = get_db()
    if not db:
        return

    charge_id = payment.get("id")
    if not charge_id:
        return

    # TODO: Refactor for financial domain debts/wallet_transactions
    logger.info(f"Payment confirmed for charge_id={charge_id} - Logic needs update for new domain.")


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
    db_ok = get_pool() is not None

    retained = None
    if db_ok:
        try:
            from db.connection import get_db
            db = get_db()
            if db:
                row = await db.fetch_one(
                    "SELECT COALESCE(SUM(balance_cache), 0) AS total "
                    "FROM wallets WHERE owner_type = 'platform'"
                )
                retained = float(row["total"]) if row else None
        except Exception:
            pass

    return {
        "status":            "ok",
        "version":           "2.0.0",
        "database":          "conectado" if db_ok else "desconectado",
        "saldo_retido_total": retained,
    }


@app.get("/admin/conciliacao")
async def conciliacao() -> dict:
    """Admin endpoint: platform wallet balance for financial reconciliation."""
    from db.connection import get_db
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    row = await db.fetch_one(
        "SELECT COALESCE(SUM(balance_cache), 0) AS total "
        "FROM wallets WHERE owner_type = 'platform'"
    )
    total = float(row["total"]) if row else 0.0
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


# ---------------------------------------------------------------------------
# Pluggy — Open Finance: fluxo de conexão bancária
# ---------------------------------------------------------------------------

@app.get("/pluggy/connect/{token}")
async def pluggy_connect_page(token: str):
    """
    Redireciona o usuário direto para o widget oficial da Pluggy.

    Valida o token interno e, se válido, faz redirect para
    https://connect.pluggy.ai?connect_token=<pluggy_token>.
    """
    from pluggy_flow import get_connection_by_token
    from fastapi.responses import RedirectResponse
    from datetime import datetime, timezone

    record = await get_connection_by_token(token)
    if not record:
        return HTMLResponse(
            content="<h2>Link inválido ou expirado. Volte ao WhatsApp e solicite um novo link.</h2>",
            status_code=404,
        )

    if record["expires_at"] < datetime.now(timezone.utc):
        return HTMLResponse(
            content="<h2>Este link expirou. Volte ao WhatsApp e solicite um novo link.</h2>",
            status_code=410,
        )

    if record["status"] not in ("pending",):
        return HTMLResponse(
            content="<h2>Este link já foi utilizado. Volte ao WhatsApp para continuar.</h2>",
            status_code=410,
        )

    pluggy_token = record["pluggy_connect_token"] or ""
    pluggy_url = f"https://connect.pluggy.ai?connect_token={pluggy_token}"
    return RedirectResponse(url=pluggy_url, status_code=302)


@app.get("/pluggy/connect/{token}/check")
async def pluggy_connect_check(token: str) -> Response:
    """
    Verifica se um token de conexão ainda é válido.
    Usado pelo frontend JavaScript para detectar links expirados antes de iniciar o fluxo.

    Returns 200 se válido, 410 se expirado/inválido.
    """
    from pluggy_flow import get_connection_by_token
    from datetime import datetime, timezone

    record = await get_connection_by_token(token)
    if not record:
        return Response(status_code=410)
    if record["expires_at"] < datetime.now(timezone.utc):
        return Response(status_code=410)
    if record["status"] != "pending":
        return Response(status_code=410)
    return Response(status_code=200)


@app.post("/pluggy/connect/{token}/callback")
async def pluggy_connect_callback(token: str, request: Request) -> dict:
    """
    Recebe o resultado do fluxo de conexão diretamente do frontend (Pluggy Connect Widget).

    Chamado pelo JavaScript da página após onSuccess ou onError do widget.
    Atualiza o status no banco e notifica o usuário via WhatsApp.

    Body JSON:
      status  — "connected" | "error"
      itemId  — ID do item Pluggy (quando status=connected)
      error   — Mensagem de erro (quando status=error)
    """
    from pluggy_flow import (
        get_connection_by_token,
        update_connection_status,
        _on_connection_success,
        _on_connection_error,
    )
    from pluggy import get_pluggy_client
    from datetime import datetime, timezone

    record = await get_connection_by_token(token)
    if not record:
        return {"ok": False, "message": "Token inválido."}

    if record["expires_at"] < datetime.now(timezone.utc):
        return {"ok": False, "message": "Token expirado."}

    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Corpo inválido."}

    status  = body.get("status", "")
    item_id = body.get("itemId")
    error   = body.get("error")

    if status == "connected" and item_id:
        pluggy = get_pluggy_client()
        try:
            item = await pluggy.get_item(item_id)
        except Exception as e:
            logger.warning("Não foi possível consultar item %s: %s", item_id, e)
            item = {"id": item_id, "connector": {}, "executionStatus": "SUCCESS"}

        await update_connection_status(token=token, status="connected", item_id=item_id)
        asyncio.create_task(_on_connection_success(record, item))
        return {"ok": True}

    elif status == "error":
        await update_connection_status(token=token, status="error", error_message=error or "")
        asyncio.create_task(_on_connection_error(record, {}, error or "ERROR"))
        return {"ok": True}

    return {"ok": False, "message": "Status desconhecido."}


@app.post("/webhook/pluggy")
async def pluggy_webhook(request: Request) -> Response:
    """
    Recebe notificações de eventos da Pluggy (item criado, erro, aguardando ação do usuário, etc.).

    Registre esta URL no painel Pluggy como webhook endpoint.
    URL: https://<SEU_DOMINIO>/webhook/pluggy
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    event = body.get("event", "")
    logger.info("Pluggy webhook recebido: event=%s", event)

    from pluggy_flow import handle_pluggy_webhook
    asyncio.create_task(handle_pluggy_webhook(body))

    return Response(status_code=200)


@app.post("/pluggy/iniciar")
async def pluggy_iniciar_conexao(request: Request) -> dict:
    """
    Endpoint de teste/admin para iniciar o fluxo de conexão bancária via WhatsApp.

    Body JSON:
      phone    — Número do usuário (ex: "5511999999999")
      user_id  — ID do usuário no banco (opcional)
      message  — Mensagem customizada (opcional)

    Returns o token interno da conexão para rastreamento.
    """
    from pluggy_flow import initiate_bank_connection

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corpo JSON inválido.")

    phone = body.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Campo 'phone' é obrigatório.")

    user_id = body.get("user_id")
    message = body.get("message")

    token = await initiate_bank_connection(
        phone=phone,
        user_id=user_id,
        message_override=message,
    )
    return {"ok": True, "token": token}


@app.get("/pluggy/demo")
async def pluggy_demo() -> Response:
    """
    Endpoint de demo/teste — gera um token Pluggy e redireciona direto para a
    página de conexão, sem precisar de WhatsApp.

    Acesse no navegador: /pluggy/demo
    """
    from pluggy_flow import initiate_bank_connection, get_connection_by_token, create_connection_record
    from pluggy import get_pluggy_client
    import os

    phone = "demo_test"
    pluggy = get_pluggy_client()
    connect_token = await pluggy.create_connect_token(client_user_id=phone)
    token = await create_connection_record(phone=phone, user_id=None, pluggy_connect_token=connect_token)

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    redirect_url = f"{base_url}/pluggy/connect/{token}" if base_url else f"/pluggy/connect/{token}"

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=redirect_url, status_code=302)


@app.get("/admin/pluggy/conexoes")
async def pluggy_listar_conexoes(phone: str | None = None, limit: int = 50) -> dict:
    """Admin endpoint: lista conexões Pluggy registradas."""
    from db.connection import get_db
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    if phone:
        rows = await db.fetch_all(
            "SELECT * FROM pluggy_connections WHERE phone = $1 ORDER BY created_at DESC LIMIT $2",
            phone, limit,
        )
    else:
        rows = await db.fetch_all(
            "SELECT * FROM pluggy_connections ORDER BY created_at DESC LIMIT $1",
            limit,
        )

    return {"conexoes": [dict(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Grupos — CRUD completo
# ---------------------------------------------------------------------------

@app.post("/admin/grupos")
async def criar_grupo(payload: dict) -> dict:
    """
    Cria um grupo completo em uma única chamada.

    Body JSON:
    {
      "name": "Grupo Seed SP",
      "description": "Tomadores iniciantes — São Paulo",
      "max_aggregate_exposure": 50000.00,
      "max_per_user_limit": 2000.00,
      "base_borrowing_rate": 0.04,
      "base_investment_rate": 0.025,
      "min_spread": 0.01,
      "spread_violation_strategy": "reject_investment",
      "term_curve": [
        {"min_term_days": 1,  "max_term_days": 30,  "adjustment_bps": 0},
        {"min_term_days": 31, "max_term_days": 90,  "adjustment_bps": 50},
        {"min_term_days": 91, "max_term_days": 180, "adjustment_bps": 100}
      ],
      "score_bands": [
        {"min_score": 0,   "max_score": 300,  "limit_percentage": 0.20, "label": "Iniciante"},
        {"min_score": 300, "max_score": 500,  "limit_percentage": 0.35, "label": "Bronze"},
        {"min_score": 500, "max_score": 700,  "limit_percentage": 0.55, "label": "Prata"},
        {"min_score": 700, "max_score": 850,  "limit_percentage": 0.75, "label": "Ouro"},
        {"min_score": 850, "max_score": 1001, "limit_percentage": 1.00, "label": "Elite"}
      ]
    }
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    required = ["name", "max_aggregate_exposure",
                "base_borrowing_rate", "base_investment_rate", "min_spread"]
    missing = [f for f in required if f not in payload]
    if missing:
        raise HTTPException(status_code=422, detail=f"Campos obrigatórios ausentes: {missing}")

    try:
        result = await GroupRepository(db).create_full(payload)
        return {"ok": True, "grupo": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/grupos")
async def listar_grupos(status: str | None = None) -> dict:
    """Lista todos os grupos. Filtro opcional: ?status=active|inactive"""
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    grupos = await GroupRepository(db).list_all(status=status)
    return {"grupos": [dict(g) for g in grupos], "total": len(grupos)}


@app.get("/admin/grupos/{group_id}")
async def detalhe_grupo(group_id: int) -> dict:
    """
    Retorna perfil completo do grupo:
    dados base + pool_limit + rate_policy + term_curve + score_bands + membros + saldo da wallet.
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    profile = await GroupRepository(db).get_full_profile(group_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Grupo não encontrado")
    return profile


@app.patch("/admin/grupos/{group_id}/status")
async def atualizar_status_grupo(group_id: int, payload: dict) -> dict:
    """Ativa ou desativa um grupo. Body: {"status": "active" | "inactive"}"""
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    status = payload.get("status")
    if status not in ("active", "inactive"):
        raise HTTPException(status_code=422, detail="status deve ser 'active' ou 'inactive'")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    await repo.update_status(group_id, status)
    return {"ok": True, "group_id": group_id, "status": status}


@app.put("/admin/grupos/{group_id}/taxas")
async def atualizar_taxas_grupo(group_id: int, payload: dict) -> dict:
    """
    Atualiza política de taxas do grupo (insere novo registro com effective_from=agora).

    Body: {
      "base_borrowing_rate": 0.04,
      "base_investment_rate": 0.025,
      "min_spread": 0.01,
      "spread_violation_strategy": "reject_investment"
    }
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    required = ["base_borrowing_rate", "base_investment_rate", "min_spread"]
    missing = [f for f in required if f not in payload]
    if missing:
        raise HTTPException(status_code=422, detail=f"Campos obrigatórios ausentes: {missing}")

    policy_id = await repo.set_rate_policy(
        group_id=group_id,
        base_borrowing_rate=payload["base_borrowing_rate"],
        base_investment_rate=payload["base_investment_rate"],
        min_spread=payload["min_spread"],
        spread_violation_strategy=payload.get("spread_violation_strategy", "reject_investment"),
    )
    return {"ok": True, "group_id": group_id, "rate_policy_id": policy_id}


@app.put("/admin/grupos/{group_id}/limites")
async def atualizar_limites_grupo(group_id: int, payload: dict) -> dict:
    """
    Adiciona novo pool limit para o grupo.

    Body: {
      "max_aggregate_exposure": 100000.00,
      "max_per_user_limit": 5000.00
    }
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    if "max_aggregate_exposure" not in payload:
        raise HTTPException(status_code=422, detail="max_aggregate_exposure é obrigatório")

    pool_id = await repo.set_pool_limit(
        group_id=group_id,
        max_aggregate_exposure=payload["max_aggregate_exposure"],
        max_per_user_limit=payload.get("max_per_user_limit"),
    )
    return {"ok": True, "group_id": group_id, "pool_limit_id": pool_id}


@app.put("/admin/grupos/{group_id}/curva-prazo")
async def atualizar_curva_prazo(group_id: int, payload: dict) -> dict:
    """
    Recria a curva de ajuste de taxa por prazo.

    Body: {
      "bands": [
        {"min_term_days": 1,  "max_term_days": 30,  "adjustment_bps": 0},
        {"min_term_days": 31, "max_term_days": 90,  "adjustment_bps": 50},
        {"min_term_days": 91, "max_term_days": 180, "adjustment_bps": 100}
      ]
    }
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    bands = payload.get("bands", [])
    await repo.set_term_curve(group_id, bands)
    return {"ok": True, "group_id": group_id, "bands_configuradas": len(bands)}


@app.put("/admin/grupos/{group_id}/faixas-score")
async def atualizar_faixas_score(group_id: int, payload: dict) -> dict:
    """
    Recria as faixas de score → percentual do teto de crédito.

    Body: {
      "bands": [
        {"min_score": 0,   "max_score": 300,  "limit_percentage": 0.20, "label": "Iniciante"},
        {"min_score": 300, "max_score": 500,  "limit_percentage": 0.35, "label": "Bronze"},
        {"min_score": 500, "max_score": 700,  "limit_percentage": 0.55, "label": "Prata"},
        {"min_score": 700, "max_score": 850,  "limit_percentage": 0.75, "label": "Ouro"},
        {"min_score": 850, "max_score": 1001, "limit_percentage": 1.00, "label": "Elite"}
      ]
    }
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    bands = payload.get("bands", [])
    await repo.set_score_bands(group_id, bands)
    return {"ok": True, "group_id": group_id, "faixas_configuradas": len(bands)}


@app.get("/admin/grupos/{group_id}/membros")
async def listar_membros(group_id: int, todos: bool = False) -> dict:
    """Lista membros do grupo. ?todos=true inclui histórico de saídas."""
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    membros = await repo.list_members(group_id, active_only=not todos)
    return {"group_id": group_id, "membros": [dict(m) for m in membros], "total": len(membros)}


@app.post("/admin/grupos/{group_id}/membros")
async def adicionar_membro(group_id: int, payload: dict) -> dict:
    """
    Adiciona um usuário ao grupo.
    Body: {"user_id": 42, "allocation_reason": "Onboarding manual"}
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id é obrigatório")

    repo = GroupRepository(db)
    if not await repo.get_by_id(group_id):
        raise HTTPException(status_code=404, detail="Grupo não encontrado")

    ug_id = await repo.add_member(
        group_id=group_id,
        user_id=user_id,
        allocation_reason=payload.get("allocation_reason"),
    )
    return {"ok": True, "user_group_id": ug_id, "group_id": group_id, "user_id": user_id}


@app.delete("/admin/grupos/{group_id}/membros/{user_id}")
async def remover_membro(group_id: int, user_id: int) -> dict:
    """Remove (marca left_at) um usuário do grupo."""
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    removed = await GroupRepository(db).remove_member(group_id, user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Membro ativo não encontrado neste grupo")
    return {"ok": True, "group_id": group_id, "user_id": user_id}


@app.get("/admin/upgrades")
async def listar_upgrades(status: str = "suggested") -> dict:
    """
    Lista candidatos a upgrade de grupo.
    ?status=suggested|accepted|rejected
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    eventos = await GroupRepository(db).list_upgrade_candidates(status=status)
    return {"upgrades": [dict(e) for e in eventos], "total": len(eventos)}


@app.post("/admin/upgrades/{event_id}/resolver")
async def resolver_upgrade(event_id: int, payload: dict) -> dict:
    """
    Aceita ou rejeita uma sugestão de upgrade de grupo.
    Se aceito, move o usuário para o novo grupo automaticamente.

    Body: {
      "resolution": "accepted",
      "to_group_id": 2,
      "allocation_reason": "Upgrade por score Elite"
    }
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    resolution = payload.get("resolution")
    if resolution not in ("accepted", "rejected"):
        raise HTTPException(status_code=422, detail="resolution deve ser 'accepted' ou 'rejected'")

    result = await GroupRepository(db).resolve_upgrade(
        event_id=event_id,
        resolution=resolution,
        to_group_id=payload.get("to_group_id"),
        allocation_reason=payload.get("allocation_reason"),
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/admin/usuarios/{user_id}/grupos")
async def grupos_do_usuario(user_id: int, todos: bool = False) -> dict:
    """
    Retorna os grupos de um usuário.
    ?todos=true inclui grupos que o usuário já saiu.
    """
    from db.connection import get_db
    from db.repositories.groups import GroupRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    grupos = await GroupRepository(db).get_user_groups(user_id, active_only=not todos)
    return {"user_id": user_id, "grupos": [dict(g) for g in grupos], "total": len(grupos)}


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
