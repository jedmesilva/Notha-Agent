"""
Output guardrail — validates the agent's response before sending to the user.

Flow:
  1. Normal response generation by the agent
  2. Guardrail analyses the response with context + history
  3. If approved → sends normally
  4. If rejected → attempts correction once with a specific instruction
  5. If still rejected → returns a generic safe fallback

Checks:
  - Coherence with the user's message and recent history
  - Sensitive data leakage (seller's minimum price, buyer's maximum limit)
  - Forbidden terms (GPT, AI, algorithm, OpenAI, Anthropic, Claude, etc.)
  - Monetary values invented without a tool call
  - Other non-negotiable NOTHA rules
"""
import json
import logging

logger = logging.getLogger("notha.guardrail")

_SAFE_FALLBACK = (
    "Desculpe, tive um problema ao formular minha resposta. "
    "Pode repetir o que você precisa?"
)

_GUARDRAIL_PROMPT = """Você é um auditor de qualidade do NOTHA, sistema de compra e venda pelo WhatsApp.

Analise a RESPOSTA GERADA e verifique se ela é adequada para enviar ao usuário.

━━━ CONTEXTO DO USUÁRIO ━━━
{context}

━━━ HISTÓRICO RECENTE (últimas mensagens) ━━━
{history}

━━━ ÚLTIMA MENSAGEM DO USUÁRIO ━━━
{last_message}

━━━ RESPOSTA GERADA PELO AGENTE ━━━
{reply}

━━━ CRITÉRIOS DE AVALIAÇÃO ━━━
Reprove (aprovado: false) se qualquer condição abaixo for verdadeira:

1. INCOERÊNCIA: A resposta não tem relação com a última mensagem do usuário, ou ignora completamente o assunto tratado na conversa.
2. VAZAMENTO DE DADOS: A resposta revela ao comprador o preço mínimo do vendedor, ou revela ao vendedor o limite máximo do comprador.
3. TERMOS PROIBIDOS: A resposta menciona "GPT", "OpenAI", "Anthropic", "Claude", "LLM", "inteligência artificial", "algoritmo" ou qualquer referência ao modelo de IA subjacente.
4. VALOR INVENTADO: A resposta cita um valor monetário específico (ex: "R$ 350", "USD 100") que não estava no histórico da conversa nem foi confirmado pelo sistema.
5. PROMESSA NÃO CONFIRMADA: A resposta promete prazo, condição ou funcionalidade que o sistema não confirmou (ex: "vou te avisar amanhã", "você receberá em 2 dias").
6. SEM SENTIDO: A resposta está truncada, incompleta, é só caracteres aleatórios, ou não faz sentido algum em português ou no idioma do usuário.
7. INFORMAÇÃO REPETIDA DESNECESSARIAMENTE: A resposta pede ao usuário uma informação que ele já forneceu claramente nessa mesma conversa (ex: perguntar o nome de alguém que já se identificou).

Aprove (aprovado: true) se:
- A resposta é coerente com a conversa e a mensagem do usuário
- Não viola nenhum dos critérios acima
- Está em linguagem natural adequada para WhatsApp

━━━ FORMATO DE RETORNO ━━━
Retorne SOMENTE JSON válido, sem explicações extras:

Se aprovada:
{{"aprovado": true}}

Se reprovada:
{{"aprovado": false, "categoria": "<categoria>", "motivo": "<motivo conciso>"}}

Categorias possíveis: incoerencia | vazamento_dados | termo_proibido | valor_inventado | promessa_nao_confirmada | sem_sentido | repeticao_desnecessaria
"""

_CORRECTION_PROMPT = """A resposta que você gerou foi reprovada pelo sistema de qualidade.

Motivo da reprovação: {reason} (categoria: {category})

━━━ CONTEXTO DO USUÁRIO ━━━
{context}

━━━ ÚLTIMA MENSAGEM DO USUÁRIO ━━━
{last_message}

━━━ RESPOSTA REPROVADA ━━━
{rejected_reply}

Gere uma nova resposta corrigindo especificamente o problema apontado.
Mantenha o tom adequado para WhatsApp. Seja conciso. Não mencione que houve reprovação.
Retorne SOMENTE a nova resposta, sem explicações."""


async def _call_guardrail_llm(messages: list[dict]) -> dict:
    """Calls the LLM in JSON mode to evaluate the response."""
    from llm import get_provider
    try:
        resp = await get_provider().complete(
            messages=messages,
            temperature=0.0,
            max_tokens=200,
            json_mode=True,
        )
        return json.loads(resp.text or '{"aprovado": true}')
    except Exception as e:
        logger.error("Guardrail LLM error: %s", e)
        return {"aprovado": True}


async def _call_correction_llm(messages: list[dict]) -> str:
    """Asks the LLM to correct the rejected response."""
    from llm import get_provider
    try:
        resp = await get_provider().complete(
            messages=messages,
            temperature=0.4,
            max_tokens=400,
        )
        return resp.text or _SAFE_FALLBACK
    except Exception as e:
        logger.error("Guardrail correction LLM error: %s", e)
        return _SAFE_FALLBACK


def _format_history(history: list[dict], max_messages: int = 10) -> str:
    """Formats the last N messages from the history in a readable form."""
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-max_messages:]
    lines = []
    for m in recent:
        role = "Usuário" if m["role"] == "user" else "NOTHA"
        content = m.get("content") or ""
        if content:
            lines.append(f"{role}: {content[:300]}")
    return "\n".join(lines) if lines else "(sem histórico anterior)"


def _last_user_message(history: list[dict]) -> str:
    """Extracts the last user message from the history."""
    for m in reversed(history):
        if m.get("role") == "user" and m.get("content"):
            return m["content"]
    return ""


async def validate_reply(
    reply: str,
    history: list[dict],
    context: str,
    user_message: str = "",
) -> str:
    """
    Validates the generated response before sending to the user.

    Parameters:
      reply        — text generated by the agent
      history      — full conversation history (roles: user/assistant)
      context      — user context string (from _build_context)
      user_message — last user message (if not already in history)

    Returns the original reply if approved, a corrected version if possible,
    or a safe fallback if the problem persists.
    """
    if not reply or not reply.strip():
        logger.warning("Guardrail: empty reply received — using fallback.")
        return _SAFE_FALLBACK

    last_msg = user_message or _last_user_message(history)
    history_fmt = _format_history(history)

    # ── Phase 1: Evaluation ──────────────────────────────────────────────────
    evaluation_prompt = _GUARDRAIL_PROMPT.format(
        context=context or "sem contexto",
        history=history_fmt,
        last_message=last_msg or "(sem mensagem)",
        reply=reply,
    )
    result = await _call_guardrail_llm([
        {"role": "user", "content": evaluation_prompt}
    ])

    if result.get("aprovado", True):
        return reply

    category = result.get("categoria", "desconhecido")
    reason = result.get("motivo", "sem motivo especificado")
    logger.warning("Guardrail REJECTED — category=%s | reason=%s", category, reason)

    # ── Phase 2: Correction attempt ──────────────────────────────────────────
    correction_prompt = _CORRECTION_PROMPT.format(
        reason=reason,
        category=category,
        context=context or "sem contexto",
        last_message=last_msg or "(sem mensagem)",
        rejected_reply=reply,
    )
    corrected_reply = await _call_correction_llm([
        {"role": "user", "content": correction_prompt}
    ])

    # ── Phase 3: Re-evaluate the corrected reply ─────────────────────────────
    re_evaluation_prompt = _GUARDRAIL_PROMPT.format(
        context=context or "sem contexto",
        history=history_fmt,
        last_message=last_msg or "(sem mensagem)",
        reply=corrected_reply,
    )
    final_result = await _call_guardrail_llm([
        {"role": "user", "content": re_evaluation_prompt}
    ])

    if final_result.get("aprovado", True):
        logger.info("Guardrail: corrected reply approved on second attempt.")
        return corrected_reply

    logger.error(
        "Guardrail: corrected reply also rejected — using safe fallback. "
        "category=%s | reason=%s",
        final_result.get("categoria"),
        final_result.get("motivo"),
    )
    return _SAFE_FALLBACK
