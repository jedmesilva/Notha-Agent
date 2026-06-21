import os
import anthropic
from providers.base import LLMProvider, LLMResponse, ToolCall


def _extract_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Separa o system prompt das mensagens de conversa.

    Anthropic exige system como parâmetro separado, não dentro de messages.
    """
    if messages and messages[0]["role"] == "system":
        return messages[0]["content"], messages[1:]
    return "", messages


def _translate_content(content) -> list[dict]:
    """Converte content de mensagem do formato OpenAI para Anthropic.

    Trata strings simples e listas de blocos (texto + imagens).
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    result = []
    for block in content:
        if block.get("type") == "text":
            result.append({"type": "text", "text": block["text"]})

        elif block.get("type") == "image_url":
            url: str = block["image_url"]["url"]
            if url.startswith("data:"):
                media_type, data = url[5:].split(";base64,", 1)
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                })
            else:
                result.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })

    return result


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Converte mensagens do formato canônico (OpenAI) para o formato Anthropic."""
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content")

        if role == "user":
            result.append({"role": "user", "content": _translate_content(content)})

        elif role == "assistant" and not msg.get("tool_calls"):
            result.append({"role": "assistant", "content": content or ""})

        elif role == "assistant" and msg.get("tool_calls"):
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in msg["tool_calls"]:
                import json as _json
                args = tc["function"].get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = _json.loads(args)
                    except Exception:
                        args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": args,
                })
            result.append({"role": "assistant", "content": blocks})

        elif role == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg.get("content", ""),
                }],
            })

    return result


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Converte schemas OpenAI para o formato de tools do Anthropic."""
    result = []
    for t in tools:
        fn = t.get("function", t)
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
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
        self._default_model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse:
        system, conversation = _extract_system(messages)

        if json_mode:
            system = (system + "\n\nResponda SEMPRE com um JSON válido, sem texto adicional.").strip()

        kwargs: dict = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": _to_anthropic_messages(conversation),
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
            return LLMResponse(text=" ".join(text_parts) or None, tool_calls=tool_calls)

        return LLMResponse(text=" ".join(text_parts).strip())
