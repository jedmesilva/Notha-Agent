import json
import os
from openai import AsyncOpenAI
from providers.base import LLMProvider, LLMResponse, ToolCall


class OpenAIProvider(LLMProvider):
    """OpenAI provider — supports direct API (OPENAI_API_KEY) or Replit AI Integrations."""

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
                "OpenAI not configured. Set OPENAI_API_KEY or the "
                "AI_INTEGRATIONS_OPENAI_BASE_URL and AI_INTEGRATIONS_OPENAI_API_KEY variables."
            )

        self._default_model = os.environ.get("OPENAI_MODEL", "gpt-4o")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

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
            return LLMResponse(text=message.content, tool_calls=tool_calls)

        return LLMResponse(text=(message.content or "").strip())
