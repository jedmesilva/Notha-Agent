import logging
import os
import httpx

logger = logging.getLogger("notha.whatsapp")

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


async def get_media_url(media_id: str) -> str | None:
    """Obtém a URL temporária de download de uma mídia do WhatsApp."""
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token or not media_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{GRAPH_API_URL}/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return resp.json().get("url")
    except Exception as e:
        logger.warning(f"Falha ao obter URL de mídia {media_id}: {e}")
    return None


async def download_media_as_base64(media_id: str, mime_type: str = "image/jpeg") -> str | None:
    """
    Baixa o binário de uma mídia do WhatsApp e retorna como data URI base64.

    As URLs de mídia do WhatsApp exigem Authorization header — não são acessíveis
    diretamente pelo GPT-4o Vision. Por isso é necessário baixar aqui e repassar
    como data:image/...;base64,<dados>.

    Retorna: "data:<mime>;base64,<b64>" ou None em caso de falha.
    """
    import base64

    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token or not media_id:
        return None

    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Passo 1: resolve a URL de download
            meta_resp = await client.get(
                f"{GRAPH_API_URL}/{media_id}",
                headers=headers,
            )
            if meta_resp.status_code != 200:
                logger.warning(f"Falha ao obter metadata de mídia {media_id}: {meta_resp.status_code}")
                return None

            download_url = meta_resp.json().get("url")
            if not download_url:
                return None

            # Passo 2: baixa o binário com o mesmo token
            dl_resp = await client.get(download_url, headers=headers)
            if dl_resp.status_code != 200:
                logger.warning(f"Falha ao baixar mídia {media_id}: {dl_resp.status_code}")
                return None

            # Usa o mime_type real se o servidor retornar
            content_type = dl_resp.headers.get("content-type", mime_type).split(";")[0].strip()
            b64 = base64.b64encode(dl_resp.content).decode("utf-8")
            return f"data:{content_type};base64,{b64}"

    except Exception as e:
        logger.warning(f"Falha ao baixar mídia {media_id} como base64: {e}")
        return None


async def send_message(to: str, text: str) -> dict:
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

    url = f"{GRAPH_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


def extract_messages(body: dict) -> list[dict]:
    """Extrai mensagens de texto, imagem e documento do payload do webhook.

    Campos de cada mensagem retornada:
      from            — telefone do remetente
      id              — message_id (para deduplicação)
      type            — "text" | "image" | "document"
      text            — corpo de texto ou legenda (str, pode ser vazio)
      media_id        — ID da mídia no WhatsApp (None para texto)
      media_mime_type — tipo MIME informado pelo WhatsApp (None para texto)
      caption         — legenda enviada com a imagem/documento (None para texto)
    """
    messages = []
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    msg_type = msg.get("type", "")
                    sender = msg["from"]
                    msg_id = msg.get("id", "")

                    if msg_type == "text":
                        messages.append({
                            "from": sender,
                            "id": msg_id,
                            "type": "text",
                            "text": msg["text"]["body"],
                            "media_id": None,
                            "media_mime_type": None,
                            "caption": None,
                        })

                    elif msg_type == "image":
                        img = msg.get("image", {})
                        messages.append({
                            "from": sender,
                            "id": msg_id,
                            "type": "image",
                            "text": img.get("caption", ""),
                            "media_id": img.get("id"),
                            "media_mime_type": img.get("mime_type", "image/jpeg"),
                            "caption": img.get("caption", ""),
                        })

                    elif msg_type == "document":
                        doc = msg.get("document", {})
                        messages.append({
                            "from": sender,
                            "id": msg_id,
                            "type": "document",
                            "text": doc.get("caption", ""),
                            "media_id": doc.get("id"),
                            "media_mime_type": doc.get("mime_type", "application/pdf"),
                            "caption": doc.get("caption", ""),
                        })

    except Exception:
        pass
    return messages
