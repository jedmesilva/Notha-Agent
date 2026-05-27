import os
from openai import AsyncOpenAI

SYSTEM_PROMPT = """Você é o Notha, um assistente inteligente e prestativo disponível pelo WhatsApp.
Responda de forma clara, concisa e amigável. Suas respostas devem ser adequadas para o formato de mensagem do WhatsApp — evite formatações complexas como markdown, use texto simples."""


def get_client() -> AsyncOpenAI:
    base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
    api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")

    if not base_url or not api_key:
        raise RuntimeError(
            "Variáveis de ambiente AI_INTEGRATIONS_OPENAI_BASE_URL e "
            "AI_INTEGRATIONS_OPENAI_API_KEY não configuradas."
        )

    return AsyncOpenAI(base_url=base_url, api_key=api_key)


async def chat(history: list[dict]) -> str:
    client = get_client()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    response = await client.chat.completions.create(
        model="gpt-5.4",
        messages=messages,
        max_completion_tokens=1024,
    )
    return response.choices[0].message.content.strip()
