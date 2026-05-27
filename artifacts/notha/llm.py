import os
from openai import AsyncOpenAI

SYSTEM_PROMPT = """Você é o Notha, um assistente inteligente e prestativo disponível pelo WhatsApp.
Responda de forma clara, concisa e amigável. Suas respostas devem ser adequadas para o formato de mensagem do WhatsApp — evite formatações complexas como markdown, use texto simples."""

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


async def chat(history: list[dict]) -> str:
    client = get_client()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=1024,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()
