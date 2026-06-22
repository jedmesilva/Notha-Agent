"""
Pricing/Appraisal Agent — suggests listing price and minimum acceptable price.

Runs ONCE per listing, asynchronously.
Consults: external market (web search), internal history (SQL), visual assessment (vision LLM).
Structured output: suggested_price, min_suggested_price, justification, confidence, sources.
"""
import json
import logging
from llm import get_provider

logger = logging.getLogger("notha.agent.pricing")

PRICING_SYSTEM_PROMPT = """You are the NOTHA pricing agent — a specialist in evaluating used physical products for peer-to-peer sales via WhatsApp in the Brazilian market.

━━━ YOUR TASK ━━━
Based on the data provided, suggest:
1. suggested_price — fair public listing price (attractive to buyers, honest for the market)
2. min_suggested_price — floor below which the seller should not accept (NEVER revealed to the buyer)

━━━ VALUATION CRITERIA ━━━

Product condition (apply depreciation):
- New/sealed: 85-95% of current retail price
- Like new (used carefully, no marks): 60-80% of retail
- Good condition (normal use, minor marks): 45-65% of retail
- Fair (visible wear, still works): 30-50% of retail
- Poor / defective: 15-30% of retail — requires explicit mention in description

Factors that increase price:
+ Comes with original accessories, box, or receipt
+ Discontinued or hard-to-find product
+ High current demand (recent iPhone, new console, etc.)
+ Serviced or seller-guaranteed

Factors that decrease price:
- Missing accessories or charger
- No receipt / no provenance
- Outdated model (newer version available)
- Scratches, dents, cracked screen
- Degraded battery (electronics)

━━━ CRITICAL RULES ━━━
- min_suggested_price must NEVER be below 55% of suggested_price (prevents selling at a major loss)
- If the product has a serious functional defect, the minimum may drop to 40% of market value, but requires explicit justification
- If the seller's stated price is above 120% of market, flag with an alert
- If the seller's stated price is below 60% of market, assess whether there is an undeclared defect
- Do not invent market prices — if you have no reference, set confidence: "low" and explain
- Prices should be multiples of R$5 or R$10 (more natural in informal negotiations)
- Products above R$5,000: round to multiples of R$50

━━━ CATEGORY REFERENCES ━━━
Electronics: depreciates fast — phones lose 20-30% from the moment they're unboxed
Appliances: depreciates moderately — long service life retains value
Furniture: highly depends on condition and brand
Clothing / footwear: unused = 50-70% of new; used = 10-30%
Toys / children's items: complete and clean worth more; incomplete drops sharply
Vehicles (accessories): use FIPE table as reference
Other: use common sense and flag low confidence if no reference

━━━ REQUIRED OUTPUT ━━━
Return ONLY valid JSON, no text outside the JSON:
{
  "suggested_price": <rounded number>,
  "min_suggested_price": <rounded number>,
  "justification": "<2-4 sentences explaining the reasoning, mentioning condition and market>",
  "confidence": "high | medium | low",
  "sources": ["internal_history", "visual_assessment", "external_market", "text_description"],
  "alert": "<null or alert message if the seller's price is very discrepant>"
}
"""


class PricingAgent:
    def __init__(self, db=None):
        self._db = db

    async def appraise(
        self,
        description: str,
        category: str | None,
        photos: list[str] | None = None,
        seller_asking_price: float | None = None,
        similar_history: list[dict] | None = None,
    ) -> dict:
        sources_used = []
        context_parts = [f"Product description: {description}"]

        if category:
            context_parts.append(f"Category: {category}")

        if seller_asking_price:
            context_parts.append(f"Seller's stated price: R${seller_asking_price:.2f}")

        if similar_history:
            sources_used.append("internal_history")
            prices = [
                h.get("final_price", h.get("listed_price", 0))
                for h in similar_history
                if h.get("final_price") or h.get("listed_price")
            ]
            if prices:
                avg = sum(prices) / len(prices)
                minimum = min(prices)
                maximum = max(prices)
                context_parts.append(
                    f"History of {len(prices)} similar NOTHA sales: "
                    f"avg R${avg:.0f}, min R${minimum:.0f}, max R${maximum:.0f}. "
                    f"Values: {[f'R${p:.0f}' for p in prices]}"
                )

        context_parts.append("Textual description analysed.")
        sources_used.append("text_description")

        user_content = []

        if photos:
            # photos must be a list of base64 data URIs (data:image/...;base64,...)
            # Direct WhatsApp URLs require Authorization header and won't work here
            valid = [f for f in photos[:3] if isinstance(f, str) and f.startswith("data:image/")]
            if valid:
                sources_used.append("visual_assessment")
                for data_uri in valid:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": data_uri, "detail": "low"},
                    })
            elif photos:
                logger.warning(
                    "pricing.appraise() received photos that are not base64 data URIs — ignored. "
                    "Use download_media_as_base64() before calling appraise()."
                )

        user_content.append({
            "type": "text",
            "text": "\n".join(context_parts) + "\n\nEvaluate this product and return the pricing JSON.",
        })

        try:
            resp = await get_provider().complete(
                messages=[
                    {"role": "system", "content": PRICING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=600,
                json_mode=True,
            )
            raw = resp.text or "{}"
            result = json.loads(raw)
            result["sources"] = list(set(result.get("sources", []) + sources_used))

            if seller_asking_price and result.get("suggested_price"):
                diff_pct = abs(seller_asking_price - result["suggested_price"]) / result["suggested_price"]
                result["seller_price_alert"] = diff_pct > 0.20

            return result

        except Exception as e:
            logger.error(f"Error in PricingAgent.appraise: {e}")
            fallback_price = seller_asking_price or 0
            return {
                "suggested_price": fallback_price,
                "min_suggested_price": round(fallback_price * 0.80, 2),
                "justification": "Automatic appraisal unavailable. Using seller's stated price as reference.",
                "confidence": "low",
                "sources": ["text_description"],
                "alert": None,
                "seller_price_alert": False,
            }

    async def web_search_price(self, query: str) -> str | None:
        """Searches market price via web search (uses DDGS if available)."""
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(
                    f"preço {query} usado Brasil site:olx.com.br OR site:mercadolivre.com.br",
                    max_results=5,
                ))
                if results:
                    snippets = [r.get("body", "") for r in results if r.get("body")]
                    return " | ".join(snippets[:4])
        except Exception as e:
            logger.debug(f"Web search unavailable: {e}")
        return None

    async def appraise_with_web_search(
        self,
        description: str,
        category: str | None,
        photos: list[str] | None = None,
        seller_asking_price: float | None = None,
        similar_history: list[dict] | None = None,
    ) -> dict:
        web_context = await self.web_search_price(description)
        hist = list(similar_history or [])
        if web_context:
            hist = [{"description": "external_market", "final_price": None, "_web": web_context}] + hist

        result = await self.appraise(
            description=description,
            category=category,
            photos=photos,
            seller_asking_price=seller_asking_price,
            similar_history=hist,
        )
        if web_context:
            result["sources"] = list(set(result.get("sources", []) + ["external_market"]))
        return result
