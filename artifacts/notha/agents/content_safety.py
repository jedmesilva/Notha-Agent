"""
ContentSafetyAgent — selective content safety check for listings.

Architecture rule (doc section 10):
  - NOT activated on every listing — only when deterministic heuristics flag something.
  - NEVER blocks automatically. Suspicious listings enter 'em_revisao_manual' status,
    not immediate rejection. Avoids both false positives and blind trust in one LLM pass.

Usage:
    agent = ContentSafetyAgent()
    should_run, signals = agent.should_check(description, category)
    if should_run:
        result = await agent.evaluate(description, category, signals)
        if not result["safe"]:
            # mark listing as em_revisao_manual
"""
import json
import logging
import re

logger = logging.getLogger("notha.agent.content_safety")

_SUSPICIOUS_PATTERNS = [
    (r"\b(sem\s+nota|sem\s+nf|sem\s+documento)\b",              "sem documento fiscal"),
    (r"\b(origem\s+desconhecida|procedência\s+duvidosa)\b",      "origem duvidosa"),
    (r"\b(réplica|replica|imitação|imitacao|fake|falsificado)\b", "possível falsificação"),
    (r"\b(roubado|furtado|desviado|produto\s+quente)\b",         "possível produto furtado"),
    (r"\b(sem\s+serial|serial\s+apagado|chassi\s+adulterado)\b", "identificação adulterada"),
    (r"\b(proibido|restrito|controlado)\b",                      "item possivelmente restrito"),
    (r"\b(receita\s+m[eé]dica|tarja\s+vermelha|tarja\s+preta)\b", "medicamento controlado"),
]

_HIGH_RISK_CATEGORIES = {
    "armas", "arma", "munição", "municao",
    "medicamento", "remédio", "remedio",
    "joia", "joias", "relógio", "relogio",
    "veículo", "veiculo", "carro", "moto",
}

_SAFETY_PROMPT = """You are a content safety reviewer for NOTHA, a WhatsApp second-hand marketplace.

A product listing triggered a heuristic flag. Evaluate whether it presents a genuine safety concern.

━━━ PRODUCT DESCRIPTION ━━━
{description}

━━━ CATEGORY ━━━
{category}

━━━ TRIGGERED SIGNAL ━━━
{signal}

━━━ EVALUATION CRITERIA ━━━
Flag as unsafe (safe: false) ONLY when there is CLEAR evidence of:
1. Illegal items (weapons, drugs, stolen goods, controlled substances without declaration)
2. Intentional misrepresentation (claiming fake item is genuine, hiding illegal origin)
3. Items requiring special regulatory approval that is not declared

Do NOT flag:
- Used or worn items
- Items sold without receipt (extremely common for second-hand goods)
- Legitimate imported goods
- Ambiguous descriptions in otherwise legal categories
- Items in legal gray areas without strong evidence

━━━ RETURN FORMAT ━━━
Return ONLY valid JSON:
{{
  "safe": true|false,
  "reason": "<one concise sentence if unsafe, empty string if safe>",
  "signal": "<specific element that triggered concern, or empty string>"
}}"""


class ContentSafetyAgent:

    def should_check(self, description: str, category: str) -> tuple[bool, list[str]]:
        """Determine if this listing warrants a deeper LLM safety check.

        Uses only deterministic heuristics — no LLM call here.
        Returns (should_check, matched_signals).
        """
        signals: list[str] = []
        text = description.lower()

        for pattern, label in _SUSPICIOUS_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                signals.append(label)

        cat_lower = (category or "").lower()
        is_high_risk = any(kw in cat_lower for kw in _HIGH_RISK_CATEGORIES)

        # Run LLM check when: any heuristic signal, OR high-risk category with multiple signals
        should_run = bool(signals) or (is_high_risk and len(signals) >= 1)
        return should_run, signals

    async def evaluate(
        self, description: str, category: str, signals: list[str]
    ) -> dict:
        """Full LLM safety evaluation — only called when should_check() returns True.

        Returns {"safe": True} or {"safe": False, "reason": "...", "signal": "..."}.
        On any LLM error, defaults to safe=True — never block due to system failure.
        """
        from llm import get_provider

        signal_text = "; ".join(signals) if signals else "heuristic flag"
        prompt = _SAFETY_PROMPT.format(
            description=description[:800],
            category=category or "não especificada",
            signal=signal_text,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=150,
                json_mode=True,
            )
            result = json.loads(resp.text or '{"safe": true}')
            if not result.get("safe", True):
                logger.warning(
                    "ContentSafety flagged listing: reason=%s signal=%s desc=%r",
                    result.get("reason"), result.get("signal"), description[:60],
                )
            return result
        except Exception as e:
            logger.error("ContentSafetyAgent error: %s — defaulting to safe=True", e)
            return {"safe": True}
