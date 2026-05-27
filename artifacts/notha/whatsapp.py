import os
import httpx

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


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
    messages = []
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") == "text":
                        messages.append(
                            {
                                "from": msg["from"],
                                "text": msg["text"]["body"],
                                "id": msg["id"],
                            }
                        )
    except Exception:
        pass
    return messages
