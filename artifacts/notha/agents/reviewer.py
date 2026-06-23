"""
Scope/Safety Reviewer Agent — post-synthesis guard rail.

Architecture rule (doc section 12):
  Runs AFTER any response has been assembled by another Agent, as the last
  check before sending to WhatsApp. Verifies:
    1. Response is within NOTHA's scope (physical product marketplace).
    2. Response is factually coherent with real DB data injected in context.
  If either check fails → response is replaced by a safe generic alternative.
  If the LLM call itself fails → original is returned (fail-open for availability).
"""
import json
import logging
import re

logger = logging.getLogger("notha.agent.reviewer")

_OUT_OF_SCOPE_USER_SIGNALS = re.compile(
    r"\b(receita\s+de|ingrediente|piada|joke|notícia|noticia|política|politica"
    r"|futebol|esporte|filosofia|traduz|translate\s+this|poema|música|musica"
    r"|letra\s+de|write\s+me|escreve\s+para|me\s+conta\s+uma|me\s+diz\s+uma)\b",
    re.IGNORECASE,
)

_NOTHA_SCOPE_SIGNALS = re.compile(
    r"\b(comprar?|vender?|produto|negoci|pagamento|entrega|pix|frete|retirada"
    r"|anunci|buscar?|encontrar?|offer|counter|accept|payment|delivery|pickup"
    r"|cadastr|listing|courier|alert|perfil|cpf|chave)\b",
    re.IGNORECASE,
)

_REVIEWER_PROMPT = """You are a quality-control reviewer for NOTHA, a WhatsApp marketplace for physical products.

A reply was just generated to send to a user. Evaluate it on two criteria:

━━━ CRITERION 1 — SCOPE ━━━
NOTHA only discusses: buying/selling physical products, negotiating prices, payments via Pix,
product delivery/pickup, and user profile data (name, CPF, Pix key, address).
VIOLATION: the reply provides substantive help with off-topic requests (recipes, jokes, news,
translations, sports, philosophy, etc.) instead of redirecting to NOTHA's scope.
NOT a violation: politely declining and redirecting is correct behaviour.

━━━ CRITERION 2 — FACTUAL COHERENCE ━━━
The reply must not contradict real user data shown in the context.
VIOLATION: inventing a name/amount/product that contradicts the context, or confirming
data that is not in the context (e.g. "your name is João" when no name exists in context).

━━━ USER CONTEXT (real DB data) ━━━
{context}

━━━ USER MESSAGE ━━━
{user_message}

━━━ REPLY TO REVIEW ━━━
{reply}

━━━ DECISION RULES ━━━
- Approve (approved: true) when both criteria pass.
- Reject (approved: false) ONLY for clear, unambiguous violations.
- Do NOT reject for tone, phrasing, style, or minor imprecisions.
- A reply that says "I can't help with that" for off-topic content is CORRECT — approve it.

━━━ RETURN FORMAT ━━━
Return ONLY valid JSON:
{{"approved": true|false, "reason": "<one sentence if rejected, empty string if approved>"}}"""

_SAFE_FALLBACK = [
    "Posso te ajudar com compras, vendas e negociações aqui pelo WhatsApp. O que você precisa?",
    "Foco em produtos físicos — compra, venda, negociação e pagamento seguro. Como posso ajudar?",
    "Estou aqui para compras e vendas de produtos. Me conta o que você está buscando ou quer anunciar.",
]
_fallback_idx = 0


def _next_fallback() -> str:
    global _fallback_idx
    msg = _SAFE_FALLBACK[_fallback_idx % len(_SAFE_FALLBACK)]
    _fallback_idx += 1
    return msg


def _should_review(reply: str, user_message: str) -> bool:
    """Fast heuristic: skip LLM review for replies that are obviously fine.

    Returns True only when there is a meaningful chance the reply violates scope
    or coherence — saving LLM calls for the vast majority of clean replies.
    """
    if len(reply.strip()) < 25:
        return False

    user_is_off_topic = bool(_OUT_OF_SCOPE_USER_SIGNALS.search(user_message))
    reply_is_in_scope = bool(_NOTHA_SCOPE_SIGNALS.search(reply))

    if user_is_off_topic and not reply_is_in_scope:
        return True

    if _OUT_OF_SCOPE_USER_SIGNALS.search(reply):
        return True

    return False


class ScopeReviewerAgent:
    """Post-synthesis guard rail — last check before any reply reaches the user."""

    async def review(
        self,
        reply: str,
        context: str,
        user_message: str,
        history: list[dict],
    ) -> str:
        """Validate the reply and return it if approved, or a safe fallback if rejected.

        Fail-open: any exception returns the original reply to preserve availability.
        The cost of a missed violation is lower than silently swallowing all replies.
        """
        if not reply or not reply.strip():
            return reply

        if not _should_review(reply, user_message):
            return reply

        try:
            from llm import get_provider
            prompt = _REVIEWER_PROMPT.format(
                context=(context or "no context")[:800],
                user_message=(user_message or "")[:300],
                reply=reply[:600],
            )
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=80,
                json_mode=True,
            )
            result = json.loads(resp.text or '{"approved": true, "reason": ""}')
            if result.get("approved", True):
                return reply
            logger.warning(
                "ScopeReviewer REJECTED reply — reason=%r | user_msg=%r | reply_start=%r",
                result.get("reason", ""),
                (user_message or "")[:60],
                reply[:60],
            )
            return _next_fallback()
        except Exception as exc:
            logger.error("ScopeReviewerAgent error (fail-open): %s", exc)
            return reply
