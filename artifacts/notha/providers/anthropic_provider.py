import os
import anthropic
from .base import LLMProvider


class AnthropicProvider(LLMProvider):
    """Provedor Anthropic Claude — requer ANTHROPIC_API_KEY."""

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Anthropic não configurado. Defina a variável ANTHROPIC_API_KEY."
            )

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")

    async def chat(self, history: list[dict]) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=self.system_prompt,
            messages=history,
        )
        return response.content[0].text.strip()
