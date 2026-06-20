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
