import logging
import os
import httpx

logger = logging.getLogger("notha.whatsapp")

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


async def get_media_url(media_id: str) -> str | None:
    """Fetches the temporary download URL for a WhatsApp media item."""
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
        logger.warning(f"Failed to get media URL for {media_id}: {e}")
    return None


async def download_media_as_base64(media_id: str, mime_type: str = "image/jpeg") -> str | None:
    """
    Downloads WhatsApp media binary and returns it as a base64 data URI.

    WhatsApp media URLs require an Authorization header — they are not directly
    accessible by GPT-4o Vision. The media must be downloaded here and forwarded
    as data:image/...;base64,<data>.

    Returns: "data:<mime>;base64,<b64>" or None on failure.
    """
    import base64

    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token or not media_id:
        return None

    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Step 1: resolve the download URL
            meta_resp = await client.get(
                f"{GRAPH_API_URL}/{media_id}",
                headers=headers,
            )
            if meta_resp.status_code != 200:
                logger.warning(f"Failed to get media metadata for {media_id}: {meta_resp.status_code}")
                return None

            download_url = meta_resp.json().get("url")
            if not download_url:
                return None

            # Step 2: download the binary with the same token
            dl_resp = await client.get(download_url, headers=headers)
            if dl_resp.status_code != 200:
                logger.warning(f"Failed to download media {media_id}: {dl_resp.status_code}")
                return None

            # Use the actual MIME type if the server returns one
            content_type = dl_resp.headers.get("content-type", mime_type).split(";")[0].strip()
            b64 = base64.b64encode(dl_resp.content).decode("utf-8")
            return f"data:{content_type};base64,{b64}"

    except Exception as e:
        logger.warning(f"Failed to download media {media_id} as base64: {e}")
        return None


async def download_media_bytes(media_id: str) -> tuple[bytes, str] | tuple[None, None]:
    """Downloads WhatsApp media binary.

    Returns (bytes, mime_type) or (None, None) on failure.
    Used for audio files sent to Whisper for transcription.
    """
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token or not media_id:
        return None, None

    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            meta_resp = await client.get(
                f"{GRAPH_API_URL}/{media_id}",
                headers=headers,
            )
            if meta_resp.status_code != 200:
                logger.warning(f"Failed to get media metadata for {media_id}: {meta_resp.status_code}")
                return None, None

            data = meta_resp.json()
            download_url = data.get("url")
            mime_type = data.get("mime_type", "audio/ogg")
            if not download_url:
                return None, None

            dl_resp = await client.get(download_url, headers=headers)
            if dl_resp.status_code != 200:
                logger.warning(f"Failed to download media {media_id}: {dl_resp.status_code}")
                return None, None

            content_type = dl_resp.headers.get("content-type", mime_type).split(";")[0].strip()
            return dl_resp.content, content_type

    except Exception as e:
        logger.warning(f"Failed to download media bytes for {media_id}: {e}")
        return None, None


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
    """Extracts text, audio, image and document messages from the webhook payload.

    Fields in each returned message:
      from            — sender's phone number
      id              — message_id (for deduplication)
      type            — "text" | "audio" | "image" | "document"
      text            — text body or caption (str, may be empty)
      media_id        — WhatsApp media ID (None for text)
      media_mime_type — MIME type reported by WhatsApp (None for text)
      caption         — caption sent with image/document (None for text/audio)
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

                    elif msg_type == "audio":
                        audio = msg.get("audio", {})
                        messages.append({
                            "from": sender,
                            "id": msg_id,
                            "type": "audio",
                            "text": "",
                            "media_id": audio.get("id"),
                            "media_mime_type": audio.get("mime_type", "audio/ogg"),
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
