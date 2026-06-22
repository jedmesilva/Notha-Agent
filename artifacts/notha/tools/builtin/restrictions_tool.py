"""
Semantic product restriction check.

Flow:
  1. LLM reads the user's description and generates precise search terms
     (normalised name, synonyms, slang, related terms in any language)
  2. DB searches ONLY those terms — returns only matched records, never the full list.
     Location filter applied automatically.
  3. LLM makes a final judgement: do the found records actually apply to THIS specific product?
     (eliminates false positives — e.g. "kitchen knife" may match "combat knife" keywords
     but they are not the same thing)
  4. Returns ALLOWED or RESTRICTED based on the database, not on assumptions.

The LLM never decides on its own — it only interprets and searches.
The database is the source of truth. The LLM is the interpreter and semantic arbiter.
"""
import json
import logging

from tools.base import Tool

logger = logging.getLogger("notha.tools.restrictions")

# ── Step 1: generate search terms ───────────────────────────────────────────
_PROMPT_GENERATE_TERMS = """You are a product classification expert for a global marketplace platform.

The user wants to trade the following product:
"{description}"
User location: {location}

Your task: generate the best search terms to find this product in a database of restricted items.

Generate terms across ALL relevant languages (the user's language, Portuguese, English, Spanish, and any language relevant to the product's origin or the user's region). Include:
- Official/technical name of the product (in multiple languages if applicable)
- Common names and regional variations
- Slang, informal terms and euphemisms in any language
- Brand names if relevant (e.g. "Glock" for a pistol)
- Category-related terms — but be SPECIFIC: "kitchen knife" is NOT "combat knife", "toy gun" is NOT "firearm"

Examples:
- "roscoe" → terms: ["roscoe", "revólver", "pistola", "arma de fogo", "handgun", "revolver", "firearm"]
- "faca de cozinha" → terms: ["faca de cozinha", "kitchen knife", "cuchillo de cocina"] — NOT "arma branca"
- "arm" (body part) → terms: ["braço", "arm", "membro superior"] — NOT "arma", "weapon"
- "marijuana" → terms: ["marijuana", "cannabis", "maconha", "weed", "erva", "baseado"]

Return ONLY valid JSON:
{{"product_identified": "<normalized product name in English>", "search_terms": ["term1", "term2", ...]}}

Maximum 12 terms. Be specific — avoid overly broad terms that cause false positives."""

# ── Step 3: final judgement ──────────────────────────────────────────────────
_PROMPT_JUDGEMENT = """You are a marketplace regulation expert.

The user wants to trade: "{user_product}"
Product identified by the system: "{identified_product}"

The database search returned the following records of possibly restricted items:
{records}

Question: do these records apply SPECIFICALLY to the user's product?

Consider:
- A "kitchen knife" is NOT a "combat knife" — even though both are knives
- A "toy revolver" is NOT a real firearm
- A "prescription medication" is NOT the same as an "illicit drug"
- Be conservative: if the user's product clearly fits the restriction, confirm it
- Be precise: do not restrict legitimate products due to superficial name similarity

Return ONLY valid JSON:

If no record applies to this specific product:
{{"restricted": false}}

If any record applies (list only the IDs that truly apply):
{{"restricted": true, "applicable_ids": [1, 5], "justification": "<concise reason>"}}"""


async def _call_llm_json(prompt: str, max_tokens: int = 300) -> dict:
    """Calls the LLM in JSON mode and returns a dict. Silent failure returns {}."""
    from llm import get_provider
    try:
        resp = await get_provider().complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
        )
        return json.loads(resp.text or "{}")
    except Exception as e:
        logger.error("LLM error in restriction check: %s", e)
        return {}


class RestrictionCheckTool(Tool):
    name = "check_restriction"
    description = (
        "Checks whether a product can be traded on NOTHA. "
        "REQUIRED before accepting any listing or initiating any product search. "
        "Understands the product semantically — slang, synonyms and other languages are recognised. "
        "Returns ALLOWED or RESTRICTED based on real database records."
    )
    parameters = {
        "type": "object",
        "properties": {
            "product_description": {
                "type": "string",
                "description": (
                    "Product description exactly as the user mentioned it. "
                    "Include name, type, brand and relevant characteristics. "
                    "Examples: '9mm pistol', 'wild parrot', 'nike replica shirt', 'calibre 38 revolver'."
                ),
            },
            "state": {
                "type": "string",
                "description": (
                    "User's state/region code (e.g. 'SP', 'RJ', 'MG', 'NY', 'CA') — "
                    "to apply regional restrictions. Optional."
                ),
            },
            "municipality": {
                "type": "string",
                "description": (
                    "User's municipality (e.g. 'São Paulo', 'Campinas', 'New York') — "
                    "to apply municipal restrictions. Optional."
                ),
            },
        },
        "required": ["product_description"],
    }

    async def execute(
        self,
        product_description: str,
        state: str | None = None,
        municipality: str | None = None,
    ) -> str:
        try:
            from db.connection import get_db
            db = get_db()
            if db is None:
                logger.warning("Database unavailable — restriction check skipped.")
                return "DB_UNAVAILABLE: restriction check not performed, proceed with caution."

            from db.repositories.restrictions import RestrictionRepository
            repo = RestrictionRepository(db)

            # ── Step 1: LLM generates specific search terms ──────────────────
            loc_parts = []
            if municipality:
                loc_parts.append(municipality)
            if state:
                loc_parts.append(f"state {state}")
            location_str = ", ".join(loc_parts) if loc_parts else "unknown"

            terms_result = await _call_llm_json(
                _PROMPT_GENERATE_TERMS.format(
                    description=product_description,
                    location=location_str,
                ),
                max_tokens=300,
            )

            terms = terms_result.get("search_terms", [])
            identified_product = terms_result.get("product_identified", product_description)

            if not terms:
                terms = [product_description]

            logger.info(
                "Restriction check: product='%s' terms=%s",
                identified_product, terms[:5]
            )

            # ── Step 2: DB searches ONLY the generated terms ─────────────────
            found = await repo.search_by_terms(
                terms=terms,
                state_code=state or None,
                municipality=municipality or None,
            )

            if not found:
                logger.info("Restriction check: ALLOWED — no records found for '%s'", identified_product)
                return "ALLOWED: no restrictions found for this product."

            # ── Step 3: LLM judges whether records actually apply ─────────────
            records_fmt = "\n".join(
                f"ID {r['id']}: [{r['category'].replace('_', ' ').upper()}] "
                f"{r['description']} — {r['reason']}"
                + (f" (state: {r['state_code']})" if r.get('state_code') else "")
                + (f" (municipality: {r['municipality']})" if r.get('municipality') else "")
                for r in found
            )

            judgement_result = await _call_llm_json(
                _PROMPT_JUDGEMENT.format(
                    user_product=product_description,
                    identified_product=identified_product,
                    records=records_fmt,
                ),
                max_tokens=200,
            )

            if not judgement_result.get("restricted", False):
                logger.info(
                    "Restriction check: ALLOWED after judgement — '%s' does not match found records",
                    identified_product,
                )
                return "ALLOWED: product does not match any registered restrictions."

            # ── Build final response with confirmed records ───────────────────
            confirmed_ids = judgement_result.get("applicable_ids", [])
            confirmed_records = (
                [r for r in found if r["id"] in confirmed_ids]
                if confirmed_ids else found
            )

            logger.warning(
                "Restriction check: RESTRICTED — '%s' | categories=%s",
                identified_product,
                [r["category"] for r in confirmed_records],
            )

            lines = ["RESTRICTED: this product cannot be traded on NOTHA.\n"]
            for r in confirmed_records:
                category = r.get("category", "").replace("_", " ").title()
                reason = r.get("reason", "")
                scope = r.get("scope", "national")
                state_code = r.get("state_code") or ""
                mun = r.get("municipality") or ""

                location = ""
                if scope == "state" and state_code:
                    location = f" (state: {state_code})"
                elif scope == "municipal" and mun:
                    location = f" (municipality: {mun})"

                lines.append(f"- Category: {category}{location}\n  Reason: {reason}")

            return "\n".join(lines)

        except Exception as e:
            logger.error("Error checking restriction: %s", e)
            return f"CHECK_ERROR: could not verify restriction ({e}). Proceed with caution."
