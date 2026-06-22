"""
Complete identity document upload flow.

1. Downloads the image from WhatsApp via Graph API
2. Uploads to Supabase Storage bucket (identity-documents)
3. Registers in the database and updates the user's identity_status
4. Returns the signed URL for internal (admin) access
"""
import logging
import os
from datetime import datetime

import httpx

from storage.client import upload_bytes, signed_url, guess_content_type

logger = logging.getLogger("notha.storage.identity")

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


async def download_whatsapp_media(media_id: str) -> tuple[bytes, str]:
    """Downloads media by media_id from the WhatsApp Cloud API.

    Returns (image_bytes, content_type).
    """
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Fetch the real media URL
        info_resp = await client.get(
            f"{GRAPH_API_URL}/{media_id}",
            headers=headers,
        )
        info_resp.raise_for_status()
        info = info_resp.json()

        media_url = info.get("url", "")
        mime_type = info.get("mime_type", "image/jpeg")

        if not media_url:
            raise ValueError(f"Media URL not found for media_id={media_id}")

        # 2. Download the image bytes
        img_resp = await client.get(media_url, headers=headers)
        img_resp.raise_for_status()

    return img_resp.content, mime_type


def _extension(mime_type: str) -> str:
    _MAP = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/heic": "heic",
        "application/pdf": "pdf",
    }
    return _MAP.get(mime_type, "bin")


async def process_identity_document(
    user_id: int,
    media_id: str,
    doc_type: str = "unknown",
    user_repo=None,
) -> dict:
    """Downloads, stores, and registers an identity document.

    Returns dict with: object_path, signed_url, doc_id.
    """
    try:
        image_bytes, mime_type = await download_whatsapp_media(media_id)
    except Exception as e:
        logger.error("Failed to download media %s: %s", media_id, e)
        raise

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    ext = _extension(mime_type)
    filename = f"{doc_type}_{ts}.{ext}"

    try:
        object_path = await upload_bytes(
            user_id=user_id,
            filename=filename,
            data=image_bytes,
            content_type=mime_type,
        )
    except Exception as e:
        logger.error("Storage upload failed (user_id=%s): %s", user_id, e)
        raise

    try:
        signed_url_result = await signed_url(object_path, expires_in=3600)
    except Exception:
        signed_url_result = ""

    doc = None
    if user_repo:
        try:
            doc = await user_repo.register_identity_document(
                user_id=user_id,
                image_url=object_path,
                doc_type=doc_type,
                whatsapp_media_id=media_id,
            )
            logger.info(
                "Document registered: doc_id=%s user_id=%s type=%s",
                doc["id"] if doc else "?",
                user_id,
                doc_type,
            )
        except Exception as e:
            logger.error("Failed to register document in DB (user_id=%s): %s", user_id, e)

    return {
        "object_path": object_path,
        "signed_url": signed_url_result,
        "mime_type": mime_type,
        "doc_id": doc["id"] if doc else None,
    }
