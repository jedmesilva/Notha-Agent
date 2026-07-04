"""
Pluggy Connection Flow — Open Finance via WhatsApp.

Fluxo completo:
1. Bot envia link único para o usuário no WhatsApp
2. Usuário abre a página web intermediária
3. Página lista bancos disponíveis e inicia conexão via Pluggy Connect Widget
4. Após autorização no banco, Pluggy notifica via webhook
5. Bot confirma a conexão no WhatsApp e persiste os dados

Tabela: pluggy_connections
  id, user_id, phone, token (UUID), pluggy_item_id, pluggy_connect_token,
  status, connectors (JSONB), created_at, expires_at, completed_at
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any

from db.connection import get_db
from pluggy import get_pluggy_client
from whatsapp import send_message

logger = logging.getLogger("notha.pluggy_flow")

# Token válido por 30 minutos — tempo suficiente para o usuário completar o fluxo
CONNECTION_TOKEN_TTL_MINUTES = 30


def _base_url() -> str:
    """Retorna a URL base da plataforma (Railway/Replit/custom domain)."""
    return os.environ.get("BASE_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# Operações de banco de dados
# ---------------------------------------------------------------------------

async def init_pluggy_tables() -> None:
    """Cria as tabelas necessárias se ainda não existirem. Seguro para rodar na startup."""
    db = get_db()
    if not db:
        return

    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pluggy_connections (
                id                   SERIAL PRIMARY KEY,
                user_id              INT,
                phone                VARCHAR(20) NOT NULL,
                token                TEXT NOT NULL UNIQUE,
                pluggy_item_id       TEXT,
                pluggy_connect_token TEXT,
                status               VARCHAR(30) NOT NULL DEFAULT 'pending',
                connectors           JSONB,
                error_message        TEXT,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at           TIMESTAMPTZ NOT NULL,
                completed_at         TIMESTAMPTZ
            )
        """)
    except Exception as e:
        if "already exists" in str(e):
            logger.debug("pluggy_connections table already exists — skipping.")
        else:
            raise

    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_pluggy_connections_phone ON pluggy_connections (phone)",
        """CREATE INDEX IF NOT EXISTS idx_pluggy_connections_item_id
           ON pluggy_connections (pluggy_item_id) WHERE pluggy_item_id IS NOT NULL""",
    ]:
        try:
            await db.execute(stmt)
        except Exception as e:
            if "already exists" in str(e):
                pass
            else:
                raise

    logger.info("Pluggy connections table ready.")


async def create_connection_record(
    phone: str,
    user_id: int | None,
    pluggy_connect_token: str,
) -> str:
    """
    Persiste um novo registro de conexão Pluggy e retorna o token interno único.

    Args:
        phone: Número de telefone do usuário.
        user_id: ID do usuário no banco (pode ser None se ainda não cadastrado).
        pluggy_connect_token: Token de sessão gerado pela Pluggy.

    Returns:
        Token interno (UUID hex) para compor o link enviado ao usuário.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Banco de dados não disponível.")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=CONNECTION_TOKEN_TTL_MINUTES)

    await db.execute(
        """
        INSERT INTO pluggy_connections
            (phone, user_id, token, pluggy_connect_token, status, expires_at)
        VALUES ($1, $2, $3, $4, 'pending', $5)
        """,
        phone, user_id, token, pluggy_connect_token, expires_at,
    )
    return token


async def get_connection_by_token(token: str) -> dict | None:
    """Retorna o registro de conexão pelo token interno."""
    db = get_db()
    if not db:
        return None
    row = await db.fetch_one(
        "SELECT * FROM pluggy_connections WHERE token = $1", token
    )
    return dict(row) if row else None


async def get_connection_by_item_id(item_id: str) -> dict | None:
    """Retorna o registro de conexão pelo pluggy_item_id (usado no webhook)."""
    db = get_db()
    if not db:
        return None
    row = await db.fetch_one(
        "SELECT * FROM pluggy_connections WHERE pluggy_item_id = $1", item_id
    )
    return dict(row) if row else None


async def update_connection_status(
    token: str,
    status: str,
    item_id: str | None = None,
    connectors: Any = None,
    error_message: str | None = None,
) -> None:
    """Atualiza o status de uma conexão."""
    db = get_db()
    if not db:
        return

    completed_at = (
        datetime.now(timezone.utc)
        if status in ("connected", "error")
        else None
    )

    await db.execute(
        """
        UPDATE pluggy_connections
        SET status         = $1,
            pluggy_item_id = COALESCE($2, pluggy_item_id),
            connectors     = COALESCE($3::jsonb, connectors),
            error_message  = $4,
            completed_at   = COALESCE($5, completed_at)
        WHERE token = $6
        """,
        status,
        item_id,
        __import__("json").dumps(connectors) if connectors else None,
        error_message,
        completed_at,
        token,
    )


async def get_active_connections_for_phone(phone: str) -> list[dict]:
    """Retorna todas as conexões Pluggy ativas para um número de telefone."""
    db = get_db()
    if not db:
        return []
    rows = await db.fetch_all(
        """
        SELECT * FROM pluggy_connections
        WHERE phone = $1 AND status = 'connected'
        ORDER BY completed_at DESC
        """,
        phone,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Lógica de negócio do fluxo
# ---------------------------------------------------------------------------

async def initiate_bank_connection(
    phone: str,
    user_id: int | None = None,
    message_override: str | None = None,
) -> str:
    """
    Inicia o fluxo de conexão bancária para um usuário via WhatsApp.

    1. Gera connect token na Pluggy
    2. Persiste no banco
    3. Envia link para o usuário no WhatsApp

    Args:
        phone: Número de telefone do usuário.
        user_id: ID do usuário no banco.
        message_override: Mensagem personalizada (substitui o padrão).

    Returns:
        Token interno da conexão (para rastreamento).
    """
    pluggy = get_pluggy_client()

    # Gera connect token na Pluggy vinculado ao número do usuário
    connect_token = await pluggy.create_connect_token(client_user_id=phone)

    # Persiste o registro
    token = await create_connection_record(
        phone=phone,
        user_id=user_id,
        pluggy_connect_token=connect_token,
    )

    # Monta o link
    base = _base_url()
    link = f"{base}/pluggy/connect/{token}"

    # Mensagem padrão
    msg = message_override or (
        "🏦 *Conexão bancária via Open Finance*\n\n"
        "Para continuar, preciso acessar seus dados bancários de forma segura "
        "através do Open Finance — o sistema regulamentado pelo Banco Central.\n\n"
        f"👉 Clique no link abaixo para conectar sua conta:\n{link}\n\n"
        f"_O link expira em {CONNECTION_TOKEN_TTL_MINUTES} minutos._"
    )

    await send_message(phone, msg)
    logger.info("Pluggy connect link sent to %s (token=%s)", phone, token[:12])
    return token


async def handle_pluggy_webhook(payload: dict) -> None:
    """
    Processa notificações de webhook da Pluggy.

    Eventos relevantes:
    - item/created     — Item criado com sucesso
    - item/updated     — Item atualizado
    - item/error       — Erro na conexão
    - item/waiting_user_action — Requer ação adicional do usuário (MFA etc.)

    Args:
        payload: Corpo do webhook enviado pela Pluggy.
    """
    event = payload.get("event", "")
    item  = payload.get("item", {})
    item_id = item.get("id", "")

    logger.info("Pluggy webhook: event=%s item_id=%s", event, item_id)

    if not item_id:
        return

    connection = await get_connection_by_item_id(item_id)

    if event in ("item/created", "item/updated"):
        execution_status = item.get("executionStatus", "")

        if execution_status == "SUCCESS":
            if connection:
                await _on_connection_success(connection, item)
            else:
                logger.warning("Webhook SUCCESS mas nenhuma conexão local para item_id=%s", item_id)

        elif execution_status in ("LOGIN_ERROR", "INVALID_CREDENTIALS", "ERROR"):
            if connection:
                await _on_connection_error(connection, item, execution_status)

        else:
            logger.info("Item %s status intermediário: %s", item_id, execution_status)

    elif event == "item/error":
        if connection:
            error = item.get("error", {})
            await _on_connection_error(connection, item, error.get("code", "ERROR"))

    elif event == "item/waiting_user_action":
        if connection:
            await _on_waiting_user_action(connection, item)


async def _on_connection_success(connection: dict, item: dict) -> None:
    """Trata conexão bem-sucedida: persiste dados e notifica via WhatsApp."""
    phone = connection["phone"]
    token = connection["token"]
    item_id = item["id"]

    # Coleta contas via API
    pluggy = get_pluggy_client()
    try:
        accounts = await pluggy.list_accounts(item_id)
    except Exception as e:
        logger.warning("Não foi possível listar contas para item %s: %s", item_id, e)
        accounts = []

    connector_info = item.get("connector", {})
    connector_name = connector_info.get("name", "Instituição")

    connectors_data = {
        "connector_id":   connector_info.get("id"),
        "connector_name": connector_name,
        "accounts":       accounts,
    }

    await update_connection_status(
        token=token,
        status="connected",
        item_id=item_id,
        connectors=connectors_data,
    )

    # Monta resumo de contas para a mensagem
    account_lines = []
    for acc in accounts[:5]:  # Limita a 5 contas na mensagem
        acc_type  = acc.get("type", "")
        acc_name  = acc.get("name", acc.get("number", ""))
        balance   = acc.get("balance", 0)
        currency  = acc.get("currencyCode", "BRL")
        account_lines.append(f"• {acc_type} — {acc_name}: {currency} {balance:,.2f}")

    accounts_text = "\n".join(account_lines) if account_lines else "Nenhuma conta encontrada."

    msg = (
        f"✅ *{connector_name} conectado com sucesso!*\n\n"
        f"Contas encontradas:\n{accounts_text}\n\n"
        "Seus dados bancários estão protegidos e só serão usados "
        "conforme autorizado por você.\n\n"
        "Como posso ajudar agora?"
    )

    try:
        await send_message(phone, msg)
    except Exception as e:
        logger.error("Erro ao notificar usuário %s sobre conexão: %s", phone, e)


async def _on_connection_error(connection: dict, item: dict, error_code: str) -> None:
    """Trata erro na conexão bancária."""
    phone = connection["phone"]
    token = connection["token"]

    error_messages = {
        "LOGIN_ERROR":          "Credenciais incorretas ou sessão expirada no banco.",
        "INVALID_CREDENTIALS":  "Usuário/senha inválidos.",
        "ACCOUNT_LOCKED":       "Conta bloqueada. Desbloqueie no app do banco e tente novamente.",
        "CONNECTION_ERROR":     "Erro de comunicação com o banco. Tente novamente em alguns minutos.",
    }
    detail = error_messages.get(error_code, f"Erro técnico ({error_code}).")

    await update_connection_status(
        token=token,
        status="error",
        item_id=item.get("id"),
        error_message=detail,
    )

    msg = (
        f"❌ *Não foi possível conectar sua conta bancária*\n\n"
        f"Motivo: {detail}\n\n"
        "Para tentar novamente, basta me dizer \"quero conectar meu banco\"."
    )

    try:
        await send_message(phone, msg)
    except Exception as e:
        logger.error("Erro ao notificar erro de conexão para %s: %s", phone, e)


async def _on_waiting_user_action(connection: dict, item: dict) -> None:
    """Trata casos onde o banco exige ação adicional (MFA, token, etc.)."""
    phone = connection["phone"]

    parameters = item.get("parameters", [])
    action_label = next(
        (p.get("label", "") for p in parameters if p.get("type") == "MFA"),
        "verificação adicional",
    )

    msg = (
        f"⚠️ *Seu banco solicitou uma verificação adicional*\n\n"
        f"Por favor, verifique: *{action_label}*\n\n"
        "Você pode precisar abrir o aplicativo do seu banco para confirmar o acesso. "
        "Depois disso, tente conectar novamente."
    )

    try:
        await send_message(phone, msg)
    except Exception as e:
        logger.error("Erro ao notificar MFA para %s: %s", phone, e)
