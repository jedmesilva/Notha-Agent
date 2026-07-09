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


async def _migrate_debt_id_on_investments() -> None:
    """
    Adiciona debt_id diretamente em investments — FK de 1 salto para a dívida coberta.

    Sem esse campo, rastrear "quais investimentos cobrem esta dívida?" exige 2 JOINs
    (investments → investment_opportunities → debts). Com debt_id direto, é 1 JOIN
    e o vínculo persiste mesmo se a oportunidade for cancelada/excluída.

    Backfill automático: preenche debt_id nos registros existentes onde
    investment_opportunities.debt_id não é NULL.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE investments "
            "ADD COLUMN IF NOT EXISTS debt_id BIGINT REFERENCES debts(id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_investments_debt_id "
            "ON investments(debt_id) WHERE debt_id IS NOT NULL"
        )
        # Backfill — preenche debt_id nos investimentos existentes ligados a oportunidades
        # com debt_id. Idempotente: só atualiza onde debt_id ainda é NULL.
        result = await conn.execute(
            """
            UPDATE investments i
               SET debt_id = o.debt_id
              FROM investment_opportunities o
             WHERE o.id       = i.opportunity_id
               AND o.debt_id  IS NOT NULL
               AND i.debt_id  IS NULL
            """
        )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info(
                "_migrate_debt_id_on_investments: backfill de %d registro(s).", count
            )
    logger.info("investments.debt_id ready.")


async def _migrate_investor_profile_tables() -> None:
    """
    Cria as tabelas de perfil de investidor e ofertas de investimento.

    investor_profiles — preferências e métricas históricas por usuário.
    investment_offers — ofertas pendentes enviadas a investidores específicos.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_profiles (
                id                      SERIAL PRIMARY KEY,
                user_id                 INT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                level_id                INT REFERENCES levels(id) ON DELETE SET NULL,
                risk_tolerance          VARCHAR(20)    NOT NULL DEFAULT 'moderate',
                min_investment_amount   NUMERIC(15,2)  NOT NULL DEFAULT 50,
                max_investment_amount   NUMERIC(15,2),
                min_term_days           INT            NOT NULL DEFAULT 1,
                max_term_days           INT            NOT NULL DEFAULT 365,
                auto_invest             BOOLEAN        NOT NULL DEFAULT FALSE,
                is_active               BOOLEAN        NOT NULL DEFAULT TRUE,
                avg_investment_amount   NUMERIC(15,2),
                avg_term_days           INT,
                total_invested_lifetime NUMERIC(15,2)  NOT NULL DEFAULT 0,
                active_investment_count INT            NOT NULL DEFAULT 0,
                last_metrics_at         TIMESTAMPTZ,
                created_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investment_offers (
                id               SERIAL PRIMARY KEY,
                opportunity_id   INT           NOT NULL REFERENCES investment_opportunities(id),
                user_id          INT           NOT NULL REFERENCES users(id),
                level_id         INT           NOT NULL,
                suggested_amount NUMERIC(15,2) NOT NULL,
                maturity_at      TIMESTAMPTZ   NOT NULL,
                status           VARCHAR(20)   NOT NULL DEFAULT 'pending',
                message_sent_at  TIMESTAMPTZ,
                expires_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
                responded_at     TIMESTAMPTZ,
                final_amount     NUMERIC(15,2),
                investment_id    INT REFERENCES investments(id),
                created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                UNIQUE(opportunity_id, user_id)
            )
            """
        )
        # Additive migrations — add columns that may be missing on existing tables.
        # Each ALTER is wrapped so the server starts even when the column already exists.
        import asyncpg as _asyncpg

        _alter_ddls = [
            # investor_profiles: add level_id column added during group→level migration
            ("ALTER TABLE investor_profiles "
             "ADD COLUMN IF NOT EXISTS level_id INT REFERENCES levels(id) ON DELETE SET NULL"),
            # investment_offers: level_id was NOT NULL in DDL but may be absent on old tables;
            # allow NULL here — old rows simply have no level.
            ("ALTER TABLE investment_offers "
             "ADD COLUMN IF NOT EXISTS level_id INT"),
        ]
        for _ddl in _alter_ddls:
            try:
                await conn.execute(_ddl)
            except Exception:
                pass  # column already exists or table not yet created — CREATE TABLE covers it

        # Índices e restrições — cada DDL isolado para resiliência a falhas parciais.
        # Cada bloco captura UniqueViolationError que ocorre quando dois processos
        # (hot-reload do uvicorn) tentam criar o mesmo objeto simultaneamente —
        # o IF NOT EXISTS não é atômico entre processos no pg_class.

        _idx_ddls = [
            ("CREATE INDEX IF NOT EXISTS idx_investor_profiles_active "
             "ON investor_profiles(is_active, level_id)"),

            ("CREATE INDEX IF NOT EXISTS idx_investment_offers_lookup "
             "ON investment_offers(user_id, status, expires_at)"),
        ]
        for _ddl in _idx_ddls:
            try:
                await conn.execute(_ddl)
            except (_asyncpg.exceptions.UniqueViolationError,
                    _asyncpg.exceptions.DuplicateTableError,
                    _asyncpg.exceptions.DuplicateObjectError):
                pass

        # Troca a constraint UNIQUE global pela restrição parcial (apenas 'pending').
        # Isso permite ao mesmo investidor receber nova oferta após decline/expire.
        try:
            await conn.execute(
                "ALTER TABLE investment_offers "
                "DROP CONSTRAINT IF EXISTS investment_offers_opportunity_id_user_id_key"
            )
        except Exception:
            pass

        try:
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_offers_pending_unique "
                "ON investment_offers(opportunity_id, user_id) WHERE status = 'pending'"
            )
        except (_asyncpg.exceptions.UniqueViolationError,
                _asyncpg.exceptions.DuplicateTableError,
                _asyncpg.exceptions.DuplicateObjectError):
            pass  # índice já existe — criado por processo concorrente

    logger.info("Investor profile tables ready.")


async def _migrate_financial_schema_extensions() -> None:
    """
    Adiciona colunas novas à estrutura financeira existente.

    Migrações seguras (IF NOT EXISTS / idempotentes):

    level_policies:
      - term_rate_formula      VARCHAR(20)  DEFAULT 'bands'
        Formula de ajuste por prazo: 'bands' (tabela), 'linear', 'log', 'sqrt'.
      - term_rate_base_bps     NUMERIC(10,2) DEFAULT 0
        Coeficiente A da fórmula (y-intercept em basis points).
      - term_rate_scale        NUMERIC(10,6) DEFAULT 0
        Coeficiente B da fórmula (inclinação).
      - default_individual_limit  NUMERIC(15,2)
        Limite individual padrão para usuários sem configuração explícita.

    investments:
      - maturity_at  TIMESTAMPTZ
        Vencimento do investimento com precisão de minutos/horas.

    investment_payouts:
      - scheduled_at  TIMESTAMPTZ
        Momento exato de execução do payout (prioridade sobre scheduled_date).
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        # level_policies — suporte a fórmulas de prazo + limite default
        for col, definition in [
            ("term_rate_formula",       "VARCHAR(20) NOT NULL DEFAULT 'bands'"),
            ("term_rate_base_bps",      "NUMERIC(10,2) NOT NULL DEFAULT 0"),
            ("term_rate_scale",         "NUMERIC(10,6) NOT NULL DEFAULT 0"),
            ("default_individual_limit","NUMERIC(15,2)"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE level_policies "
                    f"ADD COLUMN IF NOT EXISTS {col} {definition}"
                )
            except Exception:
                pass  # tabela pode não existir em envs antigos

        # investments — vencimento de alta precisão
        await conn.execute(
            "ALTER TABLE investments "
            "ADD COLUMN IF NOT EXISTS maturity_at TIMESTAMPTZ"
        )

        # investment_payouts — agendamento de alta precisão
        await conn.execute(
            "ALTER TABLE investment_payouts "
            "ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ"
        )

    logger.info("Financial schema extensions applied.")


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


async def _migrate_level_upgrade_suggestions() -> None:
    """
    Cria a tabela de sugestões de upgrade de nível, se não existir.
    Uma sugestão por usuário (status='suggested') — constraint parcial.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS level_upgrade_suggestions (
                id                  SERIAL PRIMARY KEY,
                user_id             INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                from_level_id       SMALLINT NOT NULL REFERENCES levels(id),
                to_level_id         SMALLINT NOT NULL REFERENCES levels(id),
                trigger_score       NUMERIC(6,2),
                reason              TEXT,
                status              VARCHAR(20) NOT NULL DEFAULT 'suggested',
                    -- suggested | accepted | rejected
                suggested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at         TIMESTAMPTZ,
                resolved_to_level_id SMALLINT REFERENCES levels(id)
            )
            """
        )
        import asyncpg as _asyncpg
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_level_upgrade_suggestions_status "
                "ON level_upgrade_suggestions(status, suggested_at DESC)"
            )
        except (_asyncpg.exceptions.UniqueViolationError,
                _asyncpg.exceptions.DuplicateObjectError):
            pass
        try:
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_level_upgrade_suggestions_pending "
                "ON level_upgrade_suggestions(user_id) WHERE status = 'suggested'"
            )
        except (_asyncpg.exceptions.UniqueViolationError,
                _asyncpg.exceptions.DuplicateObjectError):
            pass
    logger.info("Level upgrade suggestions table ready.")


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
    await _migrate_financial_schema_extensions()
    await _migrate_investor_profile_tables()
    await _migrate_level_upgrade_suggestions()
    await _migrate_debt_id_on_investments()

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

# ---------------------------------------------------------------------------
# Investimentos — fluxo de captação
# ---------------------------------------------------------------------------

@app.get("/oportunidades")
async def listar_oportunidades_endpoint(level_id: int | None = None, limit: int = 20) -> dict:
    """
    Lista oportunidades de investimento abertas.
    Filtro opcional: ?level_id=1
    """
    from db.connection import get_db
    from db.repositories.opportunities import OpportunityRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    opps = await OpportunityRepository(db).list_open(level_id=level_id, limit=limit)
    return {
        "oportunidades": [dict(o) for o in opps],
        "total": len(opps),
    }


@app.get("/oportunidades/{opp_id}")
async def detalhe_oportunidade(opp_id: int) -> dict:
    """Detalhes de uma oportunidade específica."""
    from db.connection import get_db
    from db.repositories.opportunities import OpportunityRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    opp = await OpportunityRepository(db).get_by_id(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="Oportunidade não encontrada")
    return dict(opp)


class InvestirBody(BaseModel):
    investor_user_id: int
    opportunity_id: int
    amount: float
    maturity_at: str  # ISO-8601 datetime — ex: "2025-12-31T23:59:00Z" ou "2025-01-01"


@app.post("/investir")
async def aceitar_investimento(body: InvestirBody) -> dict:
    """
    Registra um investimento de um usuário em uma oportunidade.

    Body JSON:
      investor_user_id — ID do usuário investidor
      opportunity_id   — ID da oportunidade aberta
      amount           — Valor investido (R$)
      maturity_at      — Vencimento do investimento (ISO-8601).
                         Pode ser curto prazo (minutos/horas) ou longo (meses).
                         Exemplo: "2025-03-01T10:00:00Z", "2025-06-30", "2025-01-01T00:30:00Z"
    """
    from db.connection import get_db
    from engine.investment_engine import accept_investment
    from decimal import Decimal
    from datetime import datetime, timezone

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    # Parse maturity_at — aceita date ou datetime ISO-8601
    try:
        maturity_str = body.maturity_at.strip().replace(" ", "T")
        if "T" in maturity_str:
            maturity_dt = datetime.fromisoformat(maturity_str)
            if maturity_dt.tzinfo is None:
                maturity_dt = maturity_dt.replace(tzinfo=timezone.utc)
        else:
            from datetime import date as _date, time as _time
            d = _date.fromisoformat(maturity_str)
            maturity_dt = datetime.combine(d, _time.max, tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"maturity_at inválido — use formato ISO-8601: {exc}",
        )

    result = await accept_investment(
        db=db,
        opportunity_id=body.opportunity_id,
        investor_user_id=body.investor_user_id,
        amount=Decimal(str(body.amount)),
        maturity_date=None,   # deprecated — usar maturity_at
        maturity_at=maturity_dt,
    )

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return result


@app.get("/investimentos/{user_id}")
async def investimentos_do_usuario(user_id: int, level_id: int | None = None) -> dict:
    """
    Posição consolidada de um investidor.
    ?level_id=1 filtra por nível específico.
    """
    from db.connection import get_db
    from db.repositories.investments import InvestmentRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    inv_repo = InvestmentRepository(db)
    if level_id:
        position = await inv_repo.get_investor_position(user_id, level_id)
        active   = await inv_repo.list_by_investor(user_id, status="active")
        return {
            "user_id":     user_id,
            "level_id":    level_id,
            "position":    position,
            "investments": [dict(i) for i in active],
        }

    # Sem filtro de nível: lista todos os investimentos
    all_inv = await inv_repo.list_by_investor(user_id)
    return {
        "user_id":     user_id,
        "investments": [dict(i) for i in all_inv],
        "total":       len(all_inv),
    }


# ---------------------------------------------------------------------------
# Eventos de risco geográfico — alimentação do motor de scoring (§8.2)
# ---------------------------------------------------------------------------

class RiskEventInput(BaseModel):
    geohash: str
    event_type: str          # 'climate' | 'economic' | 'social' | 'other'
    severity: int            # 1 (mínimo) a 5 (crítico)
    description: str
    occurred_at: str         # ISO-8601, ex: "2025-07-06T12:00:00Z"
    source: str              # ex: "inmet", "ibge", "reuters", "motor_noticias"
    source_url: str | None = None  # link opcional da matéria/alerta original


def _require_admin_key(request: Request) -> None:
    """
    Verifica a chave de administração para endpoints que alteram dados de scoring.
    Requer header `Authorization: Bearer <ADMIN_API_KEY>`.
    Quando ADMIN_API_KEY não está configurada (ambiente dev), permite acesso livre
    mas registra warning para lembrar de configurar antes do deploy.
    """
    from config import ADMIN_API_KEY
    if not ADMIN_API_KEY:
        logger.warning(
            "ADMIN_API_KEY não configurada — endpoint /admin/risk-events aberto. "
            "Configure ADMIN_API_KEY antes de expor em produção."
        )
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != ADMIN_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Não autorizado. Forneça 'Authorization: Bearer <ADMIN_API_KEY>'.",
        )


@app.post("/admin/risk-events", status_code=201)
async def ingest_risk_event(request: Request, body: RiskEventInput) -> dict:
    """
    Ingere um evento de risco geográfico em `location_risk_events`.

    Projetado para receber chamadas do motor de notícias que classificará
    matérias em risco financeiro e impacto potencial sobre tomadores de
    crédito na região indicada pelo geohash.

    Após a inserção, dispara em background o recálculo das métricas de
    mercado local (location_market_metrics) para o geohash afetado — o
    próximo cálculo de score de usuários na região já refletirá o evento.

    Requer header `Authorization: Bearer <ADMIN_API_KEY>` quando ADMIN_API_KEY
    estiver configurada no ambiente.

    Campos:
      geohash      — Geohash Nível 4–6 da região afetada (ex: "6gyf").
      event_type   — 'climate' | 'economic' | 'social' | 'other'.
      severity     — Escala 1 (baixo) a 5 (crítico).
      description  — Resumo do evento para auditoria/exibição.
      occurred_at  — Quando o evento ocorreu (ISO-8601).
      source       — Identificador da fonte (ex: "inmet", "motor_noticias").
      source_url   — URL da matéria/alerta (opcional).

    Retorna: {ok, event_id, geohash, severity}
    """
    _require_admin_key(request)

    from db.connection import get_db
    from datetime import datetime, timezone

    if body.severity < 1 or body.severity > 5:
        raise HTTPException(
            status_code=422,
            detail="severity deve estar entre 1 (mínimo) e 5 (crítico).",
        )

    valid_types = ("climate", "economic", "social", "other")
    if body.event_type not in valid_types:
        raise HTTPException(
            status_code=422,
            detail=f"event_type inválido. Use: {valid_types}",
        )

    try:
        occurred_dt = datetime.fromisoformat(
            body.occurred_at.strip().replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"occurred_at inválido — use ISO-8601: {exc}",
        )

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    event_id = await db.fetch_val(
        """
        INSERT INTO location_risk_events
            (geohash, event_type, severity, description, occurred_at, source, source_url)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        body.geohash,
        body.event_type,
        body.severity,
        body.description,
        occurred_dt,
        body.source,
        body.source_url,
    )

    logger.info(
        "risk_event inserido: id=%d geohash=%s type=%s severity=%d source=%s",
        event_id, body.geohash, body.event_type, body.severity, body.source,
    )

    # Recalcula métricas de mercado local em background para o geohash afetado.
    # O próximo cálculo de score de usuários na região já inclui este evento.
    async def _refresh_location():
        try:
            from engine.scoring_engine import recalculate_location_market_metrics
            await recalculate_location_market_metrics(db, body.geohash)
            logger.info(
                "location_market_metrics recalculado após risk_event id=%d geohash=%s",
                event_id, body.geohash,
            )
        except Exception as exc:
            logger.error(
                "Erro ao recalcular location_metrics após risk_event id=%d: %s",
                event_id, exc,
            )

    asyncio.create_task(_refresh_location())

    return {
        "ok":         True,
        "event_id":   event_id,
        "geohash":    body.geohash,
        "severity":   body.severity,
        "event_type": body.event_type,
        "source":     body.source,
    }


@app.get("/admin/risk-events")
async def list_risk_events(
    geohash: str | None = None,
    days: int = 30,
    limit: int = 100,
) -> dict:
    """
    Lista eventos de risco registrados.

    ?geohash=6gyf  — filtra por região (prefixo de geohash)
    ?days=30       — janela de tempo em dias (padrão: 30)
    ?limit=100     — máximo de registros retornados
    """
    from db.connection import get_db
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    if geohash:
        rows = await db.fetch_all(
            """
            SELECT * FROM location_risk_events
            WHERE geohash LIKE $1
              AND occurred_at >= NOW() - ($2 || ' days')::interval
            ORDER BY occurred_at DESC
            LIMIT $3
            """,
            geohash + "%", str(days), limit,
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT * FROM location_risk_events
            WHERE occurred_at >= NOW() - ($1 || ' days')::interval
            ORDER BY occurred_at DESC
            LIMIT $2
            """,
            str(days), limit,
        )

    return {
        "events": [dict(r) for r in rows],
        "total":  len(rows),
        "filter": {"geohash": geohash, "days": days},
    }


@app.post("/admin/oportunidades")
async def criar_oportunidade_manual(payload: dict) -> dict:
    """
    Cria uma oportunidade de captação manualmente (sem empréstimo vinculado).
    Útil para captação geral de liquidez do fundo.

    Body: {
      "level_id": 1,
      "amount_needed": 10000.00,
      "ttl_days": 30
    }
    """
    from db.connection import get_db
    from engine.investment_engine import create_opportunity
    from decimal import Decimal
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    required = ["level_id", "amount_needed"]
    missing = [f for f in required if f not in payload]
    if missing:
        raise HTTPException(status_code=422, detail=f"Campos obrigatórios ausentes: {missing}")

    result = await create_opportunity(
        db=db,
        level_id=int(payload["level_id"]),
        amount_needed=Decimal(str(payload["amount_needed"])),
        ttl_days=int(payload.get("ttl_days", 30)),
    )
    return {"ok": True, **result}


@app.get("/admin/niveis/{level_id}/oportunidades")
async def oportunidades_do_nivel(level_id: int, limit: int = 50) -> dict:
    """Lista todas as oportunidades de um nível (todos os status)."""
    from db.connection import get_db
    from db.repositories.opportunities import OpportunityRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    opps = await OpportunityRepository(db).list_by_level(level_id, limit=limit)
    return {"level_id": level_id, "oportunidades": [dict(o) for o in opps], "total": len(opps)}


@app.delete("/admin/oportunidades/{opp_id}")
async def cancelar_oportunidade(opp_id: int) -> dict:
    """Cancela uma oportunidade de investimento aberta."""
    from db.connection import get_db
    from db.repositories.opportunities import OpportunityRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    opp = await OpportunityRepository(db).get_by_id(opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="Oportunidade não encontrada")
    if opp["status"] not in ("open", "partially_funded"):
        raise HTTPException(status_code=400, detail=f"Oportunidade não pode ser cancelada (status={opp['status']})")

    await OpportunityRepository(db).cancel(opp_id)
    return {"ok": True, "opp_id": opp_id}


@app.post("/admin/payouts/distribuir")
async def distribuir_payouts_manual() -> dict:
    """Job manual: processa todos os rendimentos de investimento vencidos."""
    from db.connection import get_db
    from engine.investment_engine import distribute_payouts
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    result = await distribute_payouts(db)
    return result


@app.get("/admin/niveis/{level_id}/posicao")
async def posicao_do_nivel(level_id: int) -> dict:
    """
    Visão financeira completa do nível:
    saldo real, exposição, total investido, oportunidades abertas.
    """
    from db.connection import get_db
    from db.repositories.wallets import WalletRepository
    from db.repositories.opportunities import OpportunityRepository
    from db.repositories.investments import InvestmentRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    wallet_repo = WalletRepository(db)
    opp_repo    = OpportunityRepository(db)
    inv_repo    = InvestmentRepository(db)

    wallet  = await wallet_repo.get_by_owner("level", level_id)
    balance = await wallet_repo.true_balance(wallet["id"]) if wallet else 0

    open_opps      = await opp_repo.list_open(level_id=level_id)
    total_invested = await inv_repo.total_active_by_level(level_id)

    policy = await db.fetch_one(
        """
        SELECT * FROM level_policies
        WHERE level_id = $1 AND effective_from <= NOW()
        ORDER BY effective_from DESC LIMIT 1
        """,
        level_id,
    )

    return {
        "level_id":              level_id,
        "wallet_balance":        float(balance),
        "total_active_invested": float(total_invested),
        "current_exposure":      float(policy["current_exposure_cache"]) if policy else 0,
        "max_exposure":          float(policy["max_aggregate_exposure"]) if policy else None,
        "max_per_user":          float(policy["max_per_user_limit"]) if policy and policy["max_per_user_limit"] else None,
        "open_opportunities":    len(open_opps),
        "open_opp_total_needed": float(sum(
            float(o["amount_needed"]) - float(o["amount_committed"]) for o in open_opps
        )),
    }


# ---------------------------------------------------------------------------
# Níveis — CRUD completo (substituem grupos)
# ---------------------------------------------------------------------------

@app.get("/admin/niveis")
async def listar_niveis(status: str | None = None) -> dict:
    """Lista todos os níveis. Filtro opcional: ?status=active|inactive"""
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    niveis = await LevelRepository(db).list_all(status=status)
    return {"niveis": [dict(n) for n in niveis], "total": len(niveis)}


@app.get("/admin/niveis/{level_id}")
async def detalhe_nivel(level_id: int) -> dict:
    """
    Retorna perfil completo do nível:
    dados base + level_policies + level_term_curve + saldo da wallet.
    """
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    profile = await LevelRepository(db).get_full_profile(level_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Nível não encontrado")
    return profile


@app.patch("/admin/niveis/{level_id}/status")
async def atualizar_status_nivel(level_id: int, payload: dict) -> dict:
    """Ativa ou desativa um nível. Body: {"status": "active" | "inactive"}"""
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    status = payload.get("status")
    if status not in ("active", "inactive"):
        raise HTTPException(status_code=422, detail="status deve ser 'active' ou 'inactive'")

    repo = LevelRepository(db)
    if not await repo.get_by_id(level_id):
        raise HTTPException(status_code=404, detail="Nível não encontrado")

    await repo.update_status(level_id, status)
    return {"ok": True, "level_id": level_id, "status": status}


@app.put("/admin/niveis/{level_id}/politica")
async def atualizar_politica_nivel(level_id: int, payload: dict) -> dict:
    """
    Atualiza a política financeira do nível (insere novo registro com effective_from=agora).

    Body: {
      "base_borrowing_rate": 0.04,
      "base_investment_rate": 0.025,
      "min_spread": 0.01,
      "spread_violation_strategy": "raise_borrowing_rate",
      "max_aggregate_exposure": 100000.00,
      "max_per_user_limit": 5000.00
    }
    """
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = LevelRepository(db)
    if not await repo.get_by_id(level_id):
        raise HTTPException(status_code=404, detail="Nível não encontrado")

    required = ["base_borrowing_rate", "base_investment_rate", "min_spread"]
    missing = [f for f in required if f not in payload]
    if missing:
        raise HTTPException(status_code=422, detail=f"Campos obrigatórios ausentes: {missing}")

    policy_id = await repo.set_policy(
        level_id=level_id,
        base_borrowing_rate=payload["base_borrowing_rate"],
        base_investment_rate=payload["base_investment_rate"],
        min_spread=payload["min_spread"],
        spread_violation_strategy=payload.get("spread_violation_strategy", "raise_borrowing_rate"),
        max_aggregate_exposure=payload.get("max_aggregate_exposure"),
        max_per_user_limit=payload.get("max_per_user_limit"),
    )
    return {"ok": True, "level_id": level_id, "policy_id": policy_id}


@app.put("/admin/niveis/{level_id}/curva-prazo")
async def atualizar_curva_prazo_nivel(level_id: int, payload: dict) -> dict:
    """
    Recria a curva de ajuste de taxa por prazo para o nível.

    Body: {
      "bands": [
        {"min_term_days": 1,  "max_term_days": 30,  "adjustment_bps": 0},
        {"min_term_days": 31, "max_term_days": 90,  "adjustment_bps": 50},
        {"min_term_days": 91, "max_term_days": 180, "adjustment_bps": 100}
      ]
    }
    """
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = LevelRepository(db)
    if not await repo.get_by_id(level_id):
        raise HTTPException(status_code=404, detail="Nível não encontrado")

    bands = payload.get("bands", [])
    await repo.set_term_curve(level_id, bands)
    return {"ok": True, "level_id": level_id, "bands_configuradas": len(bands)}


@app.get("/admin/upgrades")
async def listar_upgrades(status: str = "suggested") -> dict:
    """
    Lista candidatos a upgrade de nível.
    ?status=suggested|accepted|rejected
    """
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    eventos = await LevelRepository(db).list_upgrade_candidates(status=status)
    return {"upgrades": [dict(e) for e in eventos], "total": len(eventos)}


@app.post("/admin/upgrades/{event_id}/resolver")
async def resolver_upgrade(event_id: int, payload: dict) -> dict:
    """
    Aceita ou rejeita uma sugestão de upgrade de nível.
    Se aceito, executa a transição de nível do usuário automaticamente.

    Body: {
      "resolution": "accepted",
      "to_level_id": 3,
      "transition_reason": "Upgrade por score Elite"
    }
    """
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    resolution = payload.get("resolution")
    if resolution not in ("accepted", "rejected"):
        raise HTTPException(status_code=422, detail="resolution deve ser 'accepted' ou 'rejected'")

    result = await LevelRepository(db).resolve_upgrade(
        event_id=event_id,
        resolution=resolution,
        to_level_id=payload.get("to_level_id"),
        transition_reason=payload.get("transition_reason"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Erro ao resolver upgrade"))
    return result


@app.get("/admin/usuarios/{user_id}/nivel")
async def nivel_do_usuario(user_id: int, historico: bool = False) -> dict:
    """
    Retorna o nível atual do usuário.
    ?historico=true inclui histórico de transições.
    """
    from db.connection import get_db
    from db.repositories.levels import LevelRepository
    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    repo = LevelRepository(db)
    nivel = await repo.get_user_level(user_id)
    result: dict = {"user_id": user_id, "nivel": dict(nivel) if nivel else None}

    if historico:
        hist = await repo.get_user_level_history(user_id)
        result["historico"] = [dict(h) for h in hist]

    return result


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Perfis de investidor e ofertas
# ══════════════════════════════════════════════════════════════════════════════

class InvestorProfileInput(BaseModel):
    user_id:                int
    level_id:               int | None = None
    risk_tolerance:         str = "moderate"        # conservative | moderate | aggressive
    min_investment_amount:  float = 50.0
    max_investment_amount:  float | None = None
    min_term_days:          int = 1
    max_term_days:          int = 365
    auto_invest:            bool = False


@app.post("/admin/investor-profiles", status_code=201)
async def admin_create_investor_profile(request: Request, body: InvestorProfileInput) -> dict:
    """
    Cria ou atualiza o perfil de investidor de um usuário.
    Upsert por user_id — pode ser chamado para criar ou editar.
    """
    _require_admin_key(request)
    from db.connection import get_db
    from db.repositories.investor_profiles import InvestorProfileRepository
    from decimal import Decimal

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    if body.risk_tolerance not in ("conservative", "moderate", "aggressive"):
        raise HTTPException(status_code=422, detail="risk_tolerance inválido")
    if body.min_term_days > body.max_term_days:
        raise HTTPException(status_code=422, detail="min_term_days > max_term_days")

    repo = InvestorProfileRepository(db)
    profile_id = await repo.upsert(
        user_id=body.user_id,
        level_id=body.level_id,
        risk_tolerance=body.risk_tolerance,
        min_investment_amount=Decimal(str(body.min_investment_amount)),
        max_investment_amount=Decimal(str(body.max_investment_amount)) if body.max_investment_amount else None,
        min_term_days=body.min_term_days,
        max_term_days=body.max_term_days,
        auto_invest=body.auto_invest,
    )
    return {"ok": True, "profile_id": profile_id, "user_id": body.user_id}


@app.get("/admin/investor-profiles")
async def admin_list_investor_profiles(request: Request, level_id: int | None = None) -> dict:
    """
    Lista perfis de investidor ativos.
    ?level_id=N filtra por nível preferido.
    """
    _require_admin_key(request)
    from db.connection import get_db
    from db.repositories.investor_profiles import InvestorProfileRepository

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    profiles = await InvestorProfileRepository(db).list_active(level_id=level_id)
    return {
        "profiles": [dict(p) for p in profiles],
        "total": len(profiles),
        "level_id": level_id,
    }


@app.get("/admin/investor-profiles/{user_id}")
async def admin_get_investor_profile(request: Request, user_id: int) -> dict:
    """Retorna o perfil de investidor de um usuário específico."""
    _require_admin_key(request)
    from db.connection import get_db
    from db.repositories.investor_profiles import InvestorProfileRepository

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    profile = await InvestorProfileRepository(db).get_by_user(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil não encontrado")
    return dict(profile)


@app.delete("/admin/investor-profiles/{user_id}", status_code=200)
async def admin_deactivate_investor_profile(request: Request, user_id: int) -> dict:
    """Desativa o perfil de investidor de um usuário (não exclui)."""
    _require_admin_key(request)
    from db.connection import get_db
    from db.repositories.investor_profiles import InvestorProfileRepository

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")
    await InvestorProfileRepository(db).deactivate(user_id)
    return {"ok": True, "user_id": user_id, "status": "deactivated"}


@app.get("/admin/investment-offers")
async def admin_list_investment_offers(
    request: Request,
    status: str | None = None,
    level_id: int | None = None,
    opportunity_id: int | None = None,
    limit: int = 50,
) -> dict:
    """
    Lista ofertas de investimento com filtros opcionais.
    ?status=pending|accepted|declined|expired
    ?level_id=N ?opportunity_id=N
    """
    _require_admin_key(request)
    from db.connection import get_db

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    clauses = []
    params: list = []
    idx = 1

    if status:
        clauses.append(f"io.status = ${idx}"); params.append(status); idx += 1
    if level_id:
        clauses.append(f"io.level_id = ${idx}"); params.append(level_id); idx += 1
    if opportunity_id:
        clauses.append(f"io.opportunity_id = ${idx}"); params.append(opportunity_id); idx += 1

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit); limit_idx = idx

    offers = await db.fetch_all(
        f"""
        SELECT io.*, u.name AS user_name, u.phone
        FROM investment_offers io
        JOIN users u ON u.id = io.user_id
        {where}
        ORDER BY io.created_at DESC
        LIMIT ${limit_idx}
        """,
        *params,
    )
    return {"offers": [dict(o) for o in offers], "total": len(offers)}


@app.post("/admin/investor-matching/{opportunity_id}")
async def admin_trigger_matching(request: Request, opportunity_id: int) -> dict:
    """
    Dispara manualmente o matching de investidores para uma oportunidade aberta.
    Útil para re-tentar captação de oportunidades com cobertura incompleta.
    """
    _require_admin_key(request)
    from db.connection import get_db
    from engine.investor_matching import match_and_notify
    from decimal import Decimal

    db = get_db()
    if not db:
        raise HTTPException(status_code=503, detail="Banco de dados indisponível")

    opp = await db.fetch_one(
        "SELECT * FROM investment_opportunities WHERE id = $1", opportunity_id
    )
    if not opp:
        raise HTTPException(status_code=404, detail="Oportunidade não encontrada")
    if opp["status"] not in ("open", "partially_funded"):
        raise HTTPException(
            status_code=400,
            detail=f"Oportunidade não elegível para matching (status={opp['status']})"
        )

    remaining = Decimal(str(opp["amount_needed"])) - Decimal(str(opp["amount_committed"] or 0))
    if remaining <= 0:
        return {"ok": True, "message": "Oportunidade já totalmente financiada.", "coverage_pct": 100.0}

    result = await match_and_notify(
        db=db,
        opportunity_id=opportunity_id,
        level_id=opp["level_id"],
        amount_needed=remaining,
        expected_rate=Decimal(str(opp["expected_rate"])),
        maturity_at=opp["expires_at"],
    )
    return {"ok": True, "opportunity_id": opportunity_id, **result}


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
