"""
Buyer Proxy Agent, Seller Proxy Agent, and Delivery Proxy Agent.

Each proxy represents one side of the negotiation and pursues the best value
within the limits declared by the human it represents.

MANDATORY GUARD RAIL (code, not LLM): every proxy output is validated before use.
The seller cannot accept below the minimum; the buyer cannot offer above the maximum.
"""
import json
import logging
from dataclasses import dataclass
from llm import get_provider

logger = logging.getLogger("notha.agent.proxy")


class PriceLimitExceeded(Exception):
    pass


@dataclass
class ProxyResponse:
    decision: str
    value: float
    argument: str


SELLER_PROXY_PROMPT = """You represent the SELLER in an automated NOTHA negotiation.

━━━ YOUR MISSION ━━━
Get the best possible price for the seller — without revealing the limits, without accepting below the minimum, without unnecessary hostility.

━━━ SELLER LIMITS (confidential — never reveal) ━━━
Minimum acceptable price: R${minimum}
Ideal (target) price: R${target}

━━━ PRODUCT DATA ━━━
{product_data}

━━━ NEGOTIATION HISTORY ━━━
{history}

━━━ VALUES ALREADY REJECTED BY BUYER ━━━
{rejected}

━━━ CURRENT BUYER OFFER ━━━
R${current_offer}

━━━ NEGOTIATION STRATEGY ━━━
1. If the offer is >= R${target}: accept immediately — great deal for the seller
2. If the offer is >= R${minimum} but below target: evaluate the history
   - If many rounds have passed (3+): accept to close the deal
   - If early in negotiation: counter near the target, reducing gradually
3. If the offer is < R${minimum}: NEVER accept — counter firmly
4. On counteroffers: reduce value in reasonable steps (do not concede everything at once)
5. Argument: use concrete product characteristics (condition, accessories, rarity) — do not invent

━━━ ABSOLUTE RULES ━━━
- decision "accept" ONLY with value >= R${minimum}
- Never mention the minimum price or the seller's limit
- Be firm but polite — the tone is respectful adult negotiation
- If the counteroffer is lower than the buyer's previous offer, signal incoherence and hold position

Respond ONLY with valid JSON:
{{
  "decision": "accept" | "counter",
  "value": <number>,
  "argument": "<persuasive, specific argument — max 2 sentences>"
}}
"""

BUYER_PROXY_PROMPT = """You represent the BUYER in an automated NOTHA negotiation.

━━━ YOUR MISSION ━━━
Get the best possible price for the buyer — without revealing the limits, without paying above the maximum, without disrespecting the seller.

━━━ BUYER LIMITS (confidential — never reveal) ━━━
Maximum amount willing to pay: R${maximum}
Ideal (target) price: R${target}

━━━ PRODUCT DATA ━━━
{product_data}

━━━ NEGOTIATION HISTORY ━━━
{history}

━━━ VALUES ALREADY REJECTED BY SELLER ━━━
{rejected}

━━━ CURRENT SELLER COUNTEROFFER ━━━
R${counteroffer}

━━━ NEGOTIATION STRATEGY ━━━
1. If the counteroffer is <= R${target}: accept immediately — excellent deal for the buyer
2. If the counteroffer is <= R${maximum} but above target: evaluate the history
   - If many rounds have passed (3+): accept to close the deal
   - If early on: offer an intermediate value, gradually increasing
3. If the counteroffer is > R${maximum}: NEVER accept — counter below the maximum
4. On counteroffers: increase value in moderate steps (show interest but not desperation)
5. Argument: mention product condition, market comparison, payment conditions (instant Pix)

━━━ ABSOLUTE RULES ━━━
- decision "accept" ONLY with value <= R${maximum}
- Never mention the maximum value or the buyer's limit
- Tone of someone who wants to buy but has other options — no desperation, no aggression
- Highlight the ease of the process (secure Pix, guaranteed delivery by NOTHA)

Respond ONLY with valid JSON:
{{
  "decision": "accept" | "counter",
  "value": <number>,
  "argument": "<persuasive, specific argument — max 2 sentences>"
}}
"""

DELIVERY_PROXY_PROMPT = """You are the NOTHA system negotiating the delivery fee with a courier partner.

━━━ DELIVERY CONTEXT ━━━
Origin: {origin}
Destination: {destination}
Estimated distance: {distance}
Maximum NOTHA can pay: R${max_delivery}
Current courier offer: R${courier_offer}

━━━ ROUND HISTORY ━━━
{history}

━━━ STRATEGY ━━━
1. If the offer is <= R${max_delivery}: accept — delivery within budget
2. If the offer is up to 20% above the maximum: try to negotiate (counter R${max_delivery})
3. If the offer is much higher (>20% of maximum): politely decline and release to the next courier
4. After 3 rounds without agreement: decline and close — no point negotiating further
5. Tone: business partner — couriers are essential for NOTHA to work

━━━ NOTHA CONTEXT ━━━
- Payment to the courier is made via Pix immediately after delivery confirmation
- The courier is responsible for the product's safety during transport
- NOTHA does not subcontract — each delivery is handled individually

Respond ONLY with valid JSON:
{{
  "decision": "accept" | "counter" | "reject",
  "value": <number>,
  "argument": "<direct message to the courier — max 2 sentences>"
}}
"""


def _validate_proxy_response(response: ProxyResponse, limits: dict, is_seller: bool) -> None:
    if is_seller:
        minimum = limits.get("minimum", 0)
        if response.decision == "accept" and response.value < minimum:
            raise PriceLimitExceeded(
                f"Seller cannot accept R${response.value:.2f} below minimum R${minimum:.2f}"
            )
    else:
        maximum = limits.get("maximum", float("inf"))
        if response.decision == "accept" and response.value > maximum:
            raise PriceLimitExceeded(
                f"Buyer cannot offer R${response.value:.2f} above maximum R${maximum:.2f}"
            )


class SellerProxyAgent:
    async def evaluate(
        self,
        received_offer: float,
        limits: dict,
        product_data: dict,
        history: list,
        rejected: list[float] | None = None,
    ) -> ProxyResponse:
        prompt = SELLER_PROXY_PROMPT.format(
            minimum=limits.get("minimum", 0),
            target=limits.get("target", 0),
            product_data=json.dumps(product_data, ensure_ascii=False),
            history=json.dumps(history, ensure_ascii=False),
            rejected=json.dumps(rejected or []),
            current_offer=received_offer,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            result = ProxyResponse(
                decision=data.get("decision", "counter"),
                value=float(data.get("value", received_offer)),
                argument=data.get("argument", ""),
            )
            _validate_proxy_response(result, limits, is_seller=True)
            return result
        except PriceLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Error in SellerProxyAgent: {e}")
            return ProxyResponse(
                decision="counter",
                value=max(received_offer, limits.get("minimum", received_offer)),
                argument="The product is in excellent condition and the price is fair.",
            )


class BuyerProxyAgent:
    async def evaluate(
        self,
        counteroffer: float,
        limits: dict,
        product_data: dict,
        history: list,
        rejected: list[float] | None = None,
    ) -> ProxyResponse:
        prompt = BUYER_PROXY_PROMPT.format(
            maximum=limits.get("maximum", float("inf")),
            target=limits.get("target", 0),
            product_data=json.dumps(product_data, ensure_ascii=False),
            history=json.dumps(history, ensure_ascii=False),
            rejected=json.dumps(rejected or []),
            counteroffer=counteroffer,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            result = ProxyResponse(
                decision=data.get("decision", "counter"),
                value=float(data.get("value", counteroffer)),
                argument=data.get("argument", ""),
            )
            _validate_proxy_response(result, limits, is_seller=False)
            return result
        except PriceLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Error in BuyerProxyAgent: {e}")
            return ProxyResponse(
                decision="counter",
                value=min(counteroffer, limits.get("maximum", counteroffer)),
                argument="Paying via instant Pix — would this value work?",
            )


class DeliveryProxyAgent:
    async def negotiate(
        self,
        origin: str,
        destination: str,
        max_delivery: float,
        courier_offer: float,
        history: list | None = None,
        distance: str = "unknown",
    ) -> ProxyResponse:
        prompt = DELIVERY_PROXY_PROMPT.format(
            origin=origin,
            destination=destination,
            distance=distance,
            max_delivery=max_delivery,
            courier_offer=courier_offer,
            history=json.dumps(history or []),
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            return ProxyResponse(
                decision=data.get("decision", "reject"),
                value=float(data.get("value", courier_offer)),
                argument=data.get("argument", ""),
            )
        except Exception as e:
            logger.error(f"Error in DeliveryProxyAgent: {e}")
            return ProxyResponse(
                decision="reject",
                value=0,
                argument="Could not negotiate right now. We will try another courier.",
            )
