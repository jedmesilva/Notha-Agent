import os
import anthropic
from providers.base import LLMProvider, LLMResponse, ToolCall


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Converte mensagens do formato canônico (OpenAI) para o formato Anthropic."""
    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg["role"] in ("user", "assistant") and "tool_calls" not in msg:
            result.append({"role": msg["role"], "content": msg.get("content") or ""})

        elif msg["role"] == "assistant" and msg.get("tool_calls"):
            content = []
            for tc in msg["tool_calls"]:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": tc["function"].get("arguments", {}),
                })
            result.append({"role": "assistant", "content": content})

        elif msg["role"] == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }],
            })

        i += 1
    return result


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Converte schemas OpenAI para o formato de tools do Anthropic."""
    result = []
    for t in tools:
        fn = t["function"]
        result.append({
            "name": fn["name"],
            "description": fn["description"],
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


class AnthropicProvider(LLMProvider):
    """Provedor Anthropic Claude — requer ANTHROPIC_API_KEY."""

    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Anthropic não configurado. Defina a variável ANTHROPIC_API_KEY."
            )
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
        self._system_prompt: str = ""

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        system = self._system_prompt
        anthropic_messages = _to_anthropic_messages(messages)

        kwargs = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": anthropic_messages,
        }
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

        response = await self._client.messages.create(**kwargs)

        tool_calls = []
        text_parts = []

        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=block.input))
            elif block.type == "text":
                text_parts.append(block.text)

        if tool_calls:
            return LLMResponse(text=None, tool_calls=tool_calls)

        return LLMResponse(text=" ".join(text_parts).strip())
