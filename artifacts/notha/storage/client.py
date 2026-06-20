"""
Cliente Supabase Storage — upload e geração de URLs assinadas.

Bucket: documentos-identidade (privado)
Acesso: apenas via service role key (servidor) ou URLs assinadas com TTL.
"""
import logging
import mimetypes
import os
from pathlib import PurePosixPath

import httpx

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger("notha.storage")

BUCKET = "documentos-identidade"
_STORAGE_BASE = f"{SUPABASE_URL}/storage/v1"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }


def _object_path(user_id: int, filename: str) -> str:
    """Caminho dentro do bucket: {user_id}/{filename}"""
    return f"{user_id}/{filename}"


async def upload_bytes(
    user_id: int,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """Faz upload de bytes para o bucket e retorna o caminho interno (object path).

    Lança httpx.HTTPStatusError se o upload falhar.
    """
    path = _object_path(user_id, filename)
    url = f"{_STORAGE_BASE}/object/{BUCKET}/{path}"

    async with httpx.AsyncClient(timeout=30) as client:
        headers = {**_headers(), "Content-Type": content_type}
        resp = await client.post(url, headers=headers, content=data)
        resp.raise_for_status()

    logger.info("Upload concluído: bucket=%s path=%s (%d bytes)", BUCKET, path, len(data))
    return path


async def signed_url(object_path: str, expires_in: int = 3600) -> str:
    """Gera URL assinada de acesso temporário para um objeto privado.

    expires_in: duração em segundos (padrão 1h).
    """
    url = f"{_STORAGE_BASE}/object/sign/{BUCKET}/{object_path}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            json={"expiresIn": expires_in},
        )
        resp.raise_for_status()
        signed = resp.json().get("signedURL", "")

    # Garante URL absoluta
    if signed.startswith("/"):
        signed = f"{SUPABASE_URL}{signed}"

    return signed


def public_storage_url(object_path: str) -> str:
    """URL pública permanente — só usar se o bucket for público (não é o caso aqui).

    Mantido para referência futura caso o bucket seja tornado público.
    """
    return f"{_STORAGE_BASE}/object/public/{BUCKET}/{object_path}"


def guess_content_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"
