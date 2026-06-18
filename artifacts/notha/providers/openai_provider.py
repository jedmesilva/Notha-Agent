import os
from openai import AsyncOpenAI
from .base import LLMProvider


class OpenAIProvider(LLMProvider):
    """Provedor OpenAI — suporta API direta (OPENAI_API_KEY) ou Replit AI Integrations."""

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt

        replit_base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
        replit_api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
        direct_api_key = os.environ.get("OPENAI_API_KEY")

        if replit_base_url and replit_api_key:
            self._client = AsyncOpenAI(base_url=replit_base_url, api_key=replit_api_key)
        elif direct_api_key:
            self._client = AsyncOpenAI(api_key=direct_api_key)
        else:
            raise RuntimeError(
                "OpenAI não configurado. Defina OPENAI_API_KEY ou as variáveis "
                "AI_INTEGRATIONS_OPENAI_BASE_URL e AI_INTEGRATIONS_OPENAI_API_KEY."
            )

        self._model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    async def chat(self, history: list[dict]) -> str:
        messages = [{"role": "system", "content": self.system_prompt}] + history
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_completion_tokens=1024,
        )
        return response.choices[0].message.content.strip()
