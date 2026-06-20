"""
Fluxo completo de upload de documento de identidade.

1. Baixa a imagem do WhatsApp via Graph API
2. Faz upload para o bucket Supabase Storage (documentos-identidade)
3. Registra no banco (documentos_identidade) e atualiza status do usuário
4. Retorna a URL assinada para acesso interno (admin)
"""
import logging
import os
from datetime import datetime

import httpx

from storage.client import upload_bytes, signed_url, guess_content_type

logger = logging.getLogger("notha.storage.identity")

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


async def baixar_midia_whatsapp(media_id: str) -> tuple[bytes, str]:
    """Baixa mídia pelo media_id do WhatsApp Cloud API.

    Retorna (bytes_da_imagem, content_type).
    """
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Obtém a URL real da mídia
        info_resp = await client.get(
            f"{GRAPH_API_URL}/{media_id}",
            headers=headers,
        )
        info_resp.raise_for_status()
        info = info_resp.json()

        media_url = info.get("url", "")
        mime_type = info.get("mime_type", "image/jpeg")

        if not media_url:
            raise ValueError(f"URL de mídia não encontrada para media_id={media_id}")

        # 2. Baixa os bytes da imagem
        img_resp = await client.get(media_url, headers=headers)
        img_resp.raise_for_status()

    return img_resp.content, mime_type


def _extensao(mime_type: str) -> str:
    _MAP = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/heic": "heic",
        "application/pdf": "pdf",
    }
    return _MAP.get(mime_type, "bin")


async def processar_documento_identidade(
    user_id: int,
    media_id: str,
    tipo: str = "desconhecido",
    user_repo=None,
) -> dict:
    """Baixa, armazena e registra um documento de identidade.

    Retorna dict com: object_path, signed_url, doc_id.
    """
    # Baixa a imagem do WhatsApp
    try:
        imagem_bytes, mime_type = await baixar_midia_whatsapp(media_id)
    except Exception as e:
        logger.error("Falha ao baixar mídia %s: %s", media_id, e)
        raise

    # Constrói o nome do arquivo com timestamp para unicidade
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    ext = _extensao(mime_type)
    filename = f"{tipo}_{ts}.{ext}"

    # Upload para Supabase Storage
    try:
        object_path = await upload_bytes(
            user_id=user_id,
            filename=filename,
            data=imagem_bytes,
            content_type=mime_type,
        )
    except Exception as e:
        logger.error("Falha no upload para Storage (user_id=%s): %s", user_id, e)
        raise

    # URL de acesso assinada (1 hora)
    try:
        url_assinada = await signed_url(object_path, expires_in=3600)
    except Exception:
        url_assinada = ""

    # Registra no banco e marca status do usuário como em_analise
    doc = None
    if user_repo:
        try:
            doc = await user_repo.registrar_documento_identidade(
                user_id=user_id,
                url_imagem=object_path,      # Caminho interno no bucket
                tipo=tipo,
                whatsapp_media_id=media_id,
            )
            logger.info(
                "Documento registrado: doc_id=%s user_id=%s tipo=%s",
                doc["id"] if doc else "?",
                user_id,
                tipo,
            )
        except Exception as e:
            logger.error("Falha ao registrar documento no banco (user_id=%s): %s", user_id, e)

    return {
        "object_path": object_path,
        "signed_url": url_assinada,
        "mime_type": mime_type,
        "doc_id": doc["id"] if doc else None,
    }
