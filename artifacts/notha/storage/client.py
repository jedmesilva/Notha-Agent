"""
Supabase Storage client — file upload and signed URL generation.

Bucket: identity-documents (private)
Access: server-side only via service role key, or via signed URLs with TTL.
"""
import logging
import mimetypes
import os
from pathlib import PurePosixPath

import httpx

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger("notha.storage")

BUCKET = "identity-documents"
_STORAGE_BASE = f"{SUPABASE_URL}/storage/v1"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }


def _object_path(user_id: int, filename: str) -> str:
    """Path inside the bucket: {user_id}/{filename}"""
    return f"{user_id}/{filename}"


async def upload_bytes(
    user_id: int,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """Uploads bytes to the bucket and returns the internal object path.

    Raises httpx.HTTPStatusError if the upload fails.
    """
    path = _object_path(user_id, filename)
    url = f"{_STORAGE_BASE}/object/{BUCKET}/{path}"

    async with httpx.AsyncClient(timeout=30) as client:
        headers = {**_headers(), "Content-Type": content_type}
        resp = await client.post(url, headers=headers, content=data)
        resp.raise_for_status()

    logger.info("Upload complete: bucket=%s path=%s (%d bytes)", BUCKET, path, len(data))
    return path


async def signed_url(object_path: str, expires_in: int = 3600) -> str:
    """Generates a temporary signed access URL for a private object.

    expires_in: duration in seconds (default 1h).
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

    if signed.startswith("/"):
        signed = f"{SUPABASE_URL}{signed}"

    return signed


def public_storage_url(object_path: str) -> str:
    """Permanent public URL — only use if the bucket is public (not the case here).

    Kept for future reference if the bucket is made public.
    """
    return f"{_STORAGE_BASE}/object/public/{BUCKET}/{object_path}"


def guess_content_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"
