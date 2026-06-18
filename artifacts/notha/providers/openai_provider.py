import json
import os
from openai import AsyncOpenAI
from providers.base import LLMProvider, LLMResponse, ToolCall


class OpenAIProvider(LLMProvider):
    """Provedor OpenAI — suporta API direta (OPENAI_API_KEY) ou Replit AI Integrations."""

    def __init__(self):
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

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        kwargs = {
            "model": self._model,
            "messages": messages,
            "max_completion_tokens": 1024,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        if message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=json.loads(tc.function.arguments),
                )
                for tc in message.tool_calls
            ]
            return LLMResponse(text=None, tool_calls=tool_calls)

        return LLMResponse(text=message.content.strip())
