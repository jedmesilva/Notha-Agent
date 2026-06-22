"""
Fluxo completo de upload de documento de identidade.

1. Faz download da imagem do WhatsApp via Graph API
2. Faz upload para o Supabase Storage (bucket identity-documents)
3. Executa OCR via LLM com visão para extrair dados do documento
4. Registra no banco e atualiza identity_status do usuário
5. Retorna URL assinada + dados extraídos para auto-preenchimento do perfil
"""
import json
import logging
import os
import re
from datetime import datetime

import httpx

from storage.client import upload_bytes, signed_url, guess_content_type

logger = logging.getLogger("notha.storage.identity")

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


async def download_whatsapp_media(media_id: str) -> tuple[bytes, str]:
    """Faz download da mídia pelo media_id da WhatsApp Cloud API.

    Retorna (bytes_da_imagem, content_type).
    """
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Busca a URL real da mídia
        info_resp = await client.get(
            f"{GRAPH_API_URL}/{media_id}",
            headers=headers,
        )
        info_resp.raise_for_status()
        info = info_resp.json()

        media_url = info.get("url", "")
        mime_type = info.get("mime_type", "image/jpeg")

        if not media_url:
            raise ValueError(f"URL da mídia não encontrada para media_id={media_id}")

        # 2. Faz download dos bytes da imagem
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


async def extract_document_data(image_url: str, doc_type: str) -> dict:
    """Usa LLM com visão para extrair dados pessoais de um documento de identidade.

    Retorna dict com: full_name, tax_id, date_of_birth, gender, detected_doc_type.
    Campos que não puderam ser lidos retornam None.
    """
    try:
        from llm import get_provider

        doc_label = {
            "national_id":     "RG (Registro Geral)",
            "drivers_license": "CNH (Carteira Nacional de Habilitação)",
            "passport":        "Passaporte",
            "unknown":         "documento de identidade",
        }.get(doc_type, "documento de identidade")

        prompt = (
            f"Esta é uma imagem de um {doc_label} brasileiro.\n"
            "Extraia as seguintes informações e retorne SOMENTE um JSON válido:\n"
            '{\n'
            '  "full_name": "nome completo da pessoa (null se não visível)",\n'
            '  "tax_id": "CPF sem pontuação — apenas dígitos (null se não visível)",\n'
            '  "date_of_birth": "data de nascimento no formato YYYY-MM-DD (null se não visível)",\n'
            '  "gender": "M para masculino, F para feminino (null se não visível)",\n'
            '  "detected_doc_type": "national_id | drivers_license | passport | unknown"\n'
            '}\n\n'
            "Importante:\n"
            "- Retorne APENAS o JSON, sem explicações, sem markdown\n"
            "- Se um campo não estiver visível ou legível, use null\n"
            "- O CPF pode aparecer como XXX.XXX.XXX-XX — remova a pontuação\n"
            "- A data pode aparecer em DD/MM/YYYY — converta para YYYY-MM-DD"
        )

        resp = await get_provider().complete(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                ],
            }],
            temperature=0.0,
            max_tokens=300,
            model="gpt-4o",
        )

        text = (resp.text or "").strip()
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            logger.info("OCR extraído do documento: %s", {k: v for k, v in data.items() if k != "full_name"})
            return data

    except Exception as e:
        logger.warning("Falha na extração OCR do documento: %s", e)

    return {}


async def process_identity_document(
    user_id: int,
    media_id: str,
    doc_type: str = "unknown",
    user_repo=None,
    run_ocr: bool = True,
) -> dict:
    """Faz download, armazena, executa OCR e registra um documento de identidade.

    Retorna dict com: object_path, signed_url, doc_id, extracted_data.
    """
    try:
        image_bytes, mime_type = await download_whatsapp_media(media_id)
    except Exception as e:
        logger.error("Falha ao fazer download da mídia %s: %s", media_id, e)
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
        logger.error("Falha no upload para storage (user_id=%s): %s", user_id, e)
        raise

    signed_url_result = ""
    try:
        signed_url_result = await signed_url(object_path, expires_in=3600)
    except Exception:
        pass

    # OCR — extrai dados do documento para auto-preenchimento do perfil
    extracted_data: dict = {}
    if run_ocr and signed_url_result:
        extracted_data = await extract_document_data(signed_url_result, doc_type)

        # Se o OCR detectou o tipo de documento, refina doc_type
        if extracted_data.get("detected_doc_type") and doc_type == "unknown":
            doc_type = extracted_data["detected_doc_type"]

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
                "Documento registrado: doc_id=%s user_id=%s type=%s",
                doc["id"] if doc else "?",
                user_id,
                doc_type,
            )
        except Exception as e:
            logger.error("Falha ao registrar documento no banco (user_id=%s): %s", user_id, e)

    return {
        "object_path":    object_path,
        "signed_url":     signed_url_result,
        "mime_type":      mime_type,
        "doc_id":         doc["id"] if doc else None,
        "extracted_data": extracted_data,
    }
