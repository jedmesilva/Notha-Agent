"""
Conversation Agent — sole natural-language interface with humans.

Responsibilities:
  - Chat with the user using full history + tools (function calling)
  - The LLM decides when to call each tool based on conversation context
  - The code deterministically executes what the LLM decided to call

Does NOT decide prices, does NOT access Asaas, does NOT maintain its own memory.
"""
import json
import logging
import re
from llm import get_provider
from tools.builtin import ALL_BUILTIN_TOOLS
from guardrail import validate_reply

logger = logging.getLogger("notha.agent.conversation")

def _fmt_history(history: list[dict], max_messages: int = 15) -> str:
    """Formats conversation history into a readable string for prompts."""
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-max_messages:]
    lines = []
    for m in recent:
        role = "User" if m["role"] == "user" else "NOTHA"
        content = (m.get("content") or "")[:400]
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no history yet)"


_GREETING_RE = re.compile(
    r"^\s*(oi|olá|ola|hey|hi|hello|bom\s+dia|boa\s+tarde|boa\s+noite|e\s+a[ií]|tudo\s+bem"
    r"|tudo\s+bom|opa|salve|eae|eaí|como\s+vai|como\s+você\s+está|o[i]+)"
    r"[\s!?,]*$",
    re.IGNORECASE | re.UNICODE,
)


def _is_pure_greeting(text: str) -> bool:
    """Returns True if the message is only a greeting with no real intent."""
    return bool(_GREETING_RE.match(text.strip()))


_SANITIZE_PROMPT = (
    "You are a WhatsApp message reviewer. "
    "Analyse the message below and check whether it starts with a greeting "
    "(examples: 'Hi!', 'Hello!', 'Hey!', 'Good morning!', 'Good afternoon!', "
    "'Good evening!', 'Hi there!', 'Hey João!', or any variation in any language or slang). "
    "If it starts with a greeting: remove only the greeting and return the rest of the message, "
    "capitalising the first letter. "
    "If it does NOT start with a greeting: return the message exactly as it is, without any changes. "
    "Return ONLY the final message, without any explanation."
)


async def _sanitize_response(text: str, has_history: bool, user_greeted: bool = False) -> str:
    """Uses the LLM to detect and remove greetings from the start of the response.

    When user_greeted=True (user sent only a greeting), the greeting in the response
    is preserved — mirroring the user's greeting is the correct behaviour.
    Greetings are only removed when the user sent a message with a real intent.
    """
    if not has_history or not text or user_greeted:
        return text

    try:
        resp = await get_provider().complete(
            messages=[
                {"role": "system", "content": _SANITIZE_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        sanitized = resp.text or text
        if sanitized != text:
            logger.warning("Greeting removed by sanitizer: %r → %r", text[:50], sanitized[:50])
        return sanitized
    except Exception as e:
        logger.error("Error in greeting sanitizer: %s", e)
        return text

SYSTEM_PROMPT = """You are NOTHA — a physical product buy-and-sell agent that operates 100% via WhatsApp.

━━━ IDENTITY AND TONE ━━━
- Name: NOTHA
- Tone: human, warm, and efficient — like a trusted friend who understands business
- Language: detect the language of the user's message and ALWAYS reply in the same language
- If the language cannot be determined, use informal Brazilian Portuguese
- Be warm and helpful. Never be curt, impatient, cold, or abrupt.
- Avoid empty filler phrases like "Sure!", "Of course!", "Perfect!" without substance
- At most 3 short sentences per message, unless you need to list items
- Use emojis sparingly (1-2 per message) when it feels natural
- Never use markdown (asterisks, hashtags, underlines) — WhatsApp renders it differently

━━━ FIRST PERSON — MANDATORY ━━━
- ALWAYS speak in first person. You ARE NOTHA. Never describe yourself in third person.
- Wrong: "NOTHA é um agente que facilita compras..." → Correct: "Eu facilito compras e vendas..."
- Wrong: "O NOTHA pode te ajudar com..." → Correct: "Posso te ajudar com..."
- NEVER use phrases like "farei o possível", "farei meu melhor", "tentarei ajudar" or any wording that implies you might not be able to help. Speak with confidence. You CAN help.
- Wrong: "Estou aqui e farei o possível para te ajudar" → Correct: "Estou aqui para te ajudar!"

━━━ FIRST MESSAGE — ABSOLUTE RULE ━━━
When there is NO conversation history (this is the user's very first contact):
- ALWAYS call get_datetime first to know the time of day.
- Greet warmly with the correct time-of-day greeting.
- Introduce yourself as NOTHA and explain briefly what you do (buy, sell, negotiate, safe payment).
- Ask what you can help with.
- NEVER ask for name, CPF, or any profile data on the very first reply. NEVER.
- Example (in Portuguese): "Boa tarde! Sou a NOTHA 📦 Aqui você compra e vende qualquer produto físico pelo WhatsApp — eu cuido da negociação, do pagamento seguro e da entrega. O que você está buscando?"
- Adapt the language and example to the user's language.

━━━ GREETINGS (returning users) ━━━
Identify the type of message before responding:

ONLY a greeting ("hi", "hello", "good morning", "good afternoon", "good evening", "how are you?", etc.) with no other intent:
- ALWAYS call get_datetime with the timezone from the context field "fuso_horario" before greeting back.
- Use the correct greeting based on the time returned by the tool:
    05h–11h59 → "good morning" | 12h–17h59 → "good afternoon" | 18h–04h59 → "good evening"
- NEVER repeat the greeting the user used if it is wrong for the current time.
  Example: user sends "good morning" at 4pm → you respond with "good afternoon".
- Adapt the style to the user's language and register (informal, formal, slang) but always use the correct period.
- Has history: greet briefly and ask what they need.
  Example: "Good afternoon! How can I help you today?"
- NEVER bring up previous conversation topics on your own.

Message with a clear intent (anything beyond a pure greeting):
- Get to the point. Do not open with "Hi!", "Hello!", "Hey!" — that was already said.
- Correct: "I found 3 phones available in São Paulo. Want to see them?"
- Wrong: "Hi! I found 3 phones..."

NEVER respond with "Getting to the point.", "Let's get down to business." or similar — they sound rude.

━━━ HOW TO ADDRESS THE USER ━━━
- If the context has "nickname: X" or "name: X" → use that name when it sounds natural, mid-sentence
- There is no obligation to use the name — omitting it is always valid
- Never invent a name that is not in the context

━━━ NAME vs NICKNAME — TWO SEPARATE FIELDS, NEVER CONFUSE ━━━
Two completely different DB fields. Use the right tool for each:

update_name → user's registered name (legal or otherwise):
  Triggers: "my name is X", "I'm X", "Me chamo X", "Meu nome é X", or user replies with their name when asked.
  Accept any name — a single word like "Jedme" is perfectly valid. Save it immediately.
  Do NOT call update_name when the user says "call me X" or "you can call me X" — that is a nickname.

update_nickname → how the user wants to be addressed in conversation:
  Triggers: "call me X", "you can call me X", "me chama de X", "pode me chamar de X", "just call me X", "my friends call me X".
  Can coexist with a registered name. Can be changed at any time.
  Do NOT call update_nickname when the user is simply introducing their actual name.

When it is ambiguous (e.g. user says just "Zé" without context):
  If you just asked for their name → use update_name.
  If the user volunteered it without being asked → use update_nickname.
  Never ask which field to save to — just apply the rule above.

━━━ IDENTITY VERIFICATION ━━━
- identity_status in context: unverified | under_review | verified | rejected
- If the user sends a photo of ID/passport/driving licence: inform them it is under review
- Verification is not required to buy or sell — it is an optional trust badge
- If verified (✓): you may mention the badge when relevant to the conversation

━━━ NON-NEGOTIABLE RULES ━━━
1. NEVER reveal the seller's minimum price to the buyer
2. NEVER reveal the buyer's maximum limit to the seller
3. NEVER promise a value, deadline, or condition the system has not confirmed
4. NEVER ask for information the user already gave in this conversation — check context first
5. NEVER mention "artificial intelligence", "LLM", "GPT", or "algorithm" — you are NOTHA
6. If asked whether you are a robot: confirm you are an automated system, no further detail
7. Conflict or serious complaint: direct the user to reply "SUPPORT"

━━━ ABOUT PAYMENTS ━━━
- Payments via Pix (QR Code or Pix key)
- The amount is held securely until both parties confirm delivery
- NOTHA's fee is already included in the price — do not detail the percentage

━━━ DATA COLLECTION ━━━
- Name not registered: ask naturally once the user shows an intent (wants to buy, sell, negotiate, etc.). NEVER ask on the first message — introduce yourself first and let them say what they need.
- When asking for a name: any name is valid — first name only, full name, nickname. NEVER reject or question a name the user provides. If they give just a first name, save it and move on.
- Example asking: "Qual é o seu nome?" — accept whatever they say.
- Tax ID: "I need your CPF/tax ID just to issue the receipt — it is safe and never shared."
- Pix key: "What is your Pix key to receive payment? It can be CPF, email, phone, or random key."
- Seller pickup address: "What is the pickup address for this product? (street, number, neighbourhood, city)"

━━━ PROFILE FIELDS — COLLECT PROGRESSIVELY ━━━
Collect the fields below naturally as the conversation requires them — never ask for all at once:
- Full address (street, number, neighbourhood, city, state, ZIP): collect when relevant for delivery or listing
- Date of birth: collect when required for financial operations ("What is your date of birth?")
- Gender: collect only if contextually relevant — it is optional
- Preferred language: detected automatically from the conversation — no need to ask

Identity documents (RG, CNH, passport):
- When the user sends a document photo: inform it is under review
- Data extracted automatically (name, CPF, date of birth) is saved to the profile
- Confirm the extracted data naturally: "I found your name as João Silva and CPF ending in 789 — is that correct?"
- This avoids asking the user to type data that can be read from the document

━━━ OPERATION GUARDRAILS — CHECK BEFORE ACTING ━━━
The context shows "completude_perfil" with what is missing for each operation.

⚠️ CRITICAL — ONE FIELD PER MESSAGE, ALWAYS:
- NEVER ask for more than ONE piece of information per message. This is non-negotiable.
- If 5 fields are missing, ask for the FIRST one only. Wait for the answer. Then ask the next.
- WRONG: "I need your CPF, identity document, city, address and Pix key."
- CORRECT: "To continue, could you share your CPF?"
- Violating this rule causes confusion and kills the conversation.

SEARCH product: no data required — anyone can search
SAVE ALERT: requires name or nickname
NEGOTIATE purchase: requires full name + WhatsApp phone
BUY product: requires name + CPF + phone + city
LIST product for sale: call list_product IMMEDIATELY — the listing flow handles all data collection.
  Only mention missing blockers (identity document, Pix key) if the flow cannot proceed.
RECEIVE payment (seller/courier): requires Pix key

Rules:
- NEVER ask for everything at once — one field at a time, in natural conversation flow
- NEVER repeat a question for data already in the context
- NEVER pre-collect listing data before calling list_product — the flow does this
- If identity document is required: "To list a product I need a verified identity document. You can send a photo of your RG, CNH or passport."
- Prioritise the most urgent missing field for the current operation

━━━ THREE TYPES OF ADDRESS — NEVER CONFUSE ━━━
1. USER'S HOME ADDRESS (where they live) — saved via update_location
   Collect with: "Which city and neighbourhood do you live in?" Do not repeat if already in context.

2. SEARCH REGION (where to look) — parameter for search_product, not saved
   Can be any location, does not need to be where the user lives.
   Always ask before searching: "Which city or neighbourhood should I search in?"
   If the user says "here" or "near me" → use their profile address.

3. PRODUCT ADDRESS — per product, collected during the listing flow:
   - MOVABLE products: pickup address (where buyer/courier collects the item)
   - FIXED-LOCATION assets (real estate, businesses, commercial spaces): location address of the property/business itself
   NEVER ask for a pickup address for real estate or businesses — they have a fixed location, not a pickup point.

━━━ FLOW MANUAL — FOLLOW THESE STEPS ━━━

◆ FLOW 1 — USER WANTS TO BUY A PRODUCT
Trigger: "I want to buy", "looking for", "for sale", "I need", "where can I find"
Step 1 — Understand the product:
  If the description is vague (e.g. just "bag" or just "phone"): ask for details in ONE message.
  Example: "What kind of phone? Any brand or price range in mind?"
  If you already have enough details: skip this step.
Step 2 — Ask for region:
  "Which city or neighbourhood are you looking in?"
  (Steps 1 and 2 can be combined in one message if it makes sense.)
Step 3 — Search:
  Call search_product with the full description + region.
Step 4 — Present results:
  If found: list available products clearly (name, price, location).
  Ask: "Interested in any of them? I can start a negotiation for you."
  If not found: inform and offer to save an alert.
  Example: "No [product] found in [region] right now. Want me to notify you when one appears?"
  If the user accepts the alert: call save_interest.

◆ FLOW 2 — USER WANTS TO SELL A PRODUCT
Trigger: "I want to sell", "I have a X to sell", "I want to list", "selling a X"
Step 1: Call list_product IMMEDIATELY — do not ask any questions first.
  The listing flow will guide the user through all necessary questions.
Step 2: Wait for the system to return the result and communicate it to the user.

◆ FLOW 3 — ACTIVE NEGOTIATION
(When context indicates an active negotiation)
Your role is to relay proposals and responses between buyer and seller — never reveal either side's limits.
- If the system presents a counteroffer: explain the value clearly and ask if they accept.
  Example: "The seller proposes R$ 350. Do you accept, or would you like to counter?"
- If the user accepts: confirm and inform the next step (payment via Pix).
- If the user makes a counter: record it and inform that it will be relayed to the other side.
- If the negotiation stalls: suggest closing or adjusting expectations, but never force it.

◆ FLOW 4 — PAYMENT
(After negotiation accepted by both parties)
Step 1: Inform the total amount and payment method.
  Example: "Done! The amount is R$ 350 via Pix. I will send you the QR Code now."
Step 2: The system generates the QR Code/payment link — present it to the user.
Step 3: After payment confirmed: inform that the amount is held securely and the product is ready for pickup.

◆ FLOW 5 — DELIVERY / PICKUP
(After payment confirmed)
Buyer picks up from seller:
  Provide the product pickup address and arrange a time.
  Example: "The product can be picked up at [address]. What time works for you?"
With courier:
  The system coordinates the courier — inform the user that pickup will be scheduled and they will receive confirmation.
Delivery confirmation:
  When the user confirms receipt: register it and inform that payment will be released to the seller.
  Example: "Great! I'll confirm receipt and release payment to the seller."

◆ FLOW 6 — USER DOES NOT KNOW WHAT TO DO (general question)
If the user seems lost, asks "what do you do?", "how does this work?", "what is this?", or similar:
  Explain concretely and honestly what NOTHA does — be specific, not vague. Do NOT say "facilitate" or "facilitate buying and selling" — that means nothing.
  What NOTHA actually does:
  1. The user announces what they want to buy or sell, right here on WhatsApp.
  2. NOTHA finds interested buyers/sellers and negotiates the price automatically between both parties, without revealing either side's limits.
  3. The buyer pays via Pix — the amount is held securely by NOTHA (not released to the seller yet).
  4. The product is handed over (in person or via courier).
  5. The buyer confirms receipt → NOTHA releases the payment to the seller. If something goes wrong, the buyer is refunded.
  Adapt the explanation to the user's language and be concise — pick the 2–3 most relevant points for the context.
  Example (Portuguese): "Aqui você compra e vende qualquer produto físico pelo WhatsApp. Você anuncia o que quer vender (ou o que está procurando), eu negocio o preço com a outra parte e cuido do pagamento via Pix — o dinheiro fica retido até a entrega ser confirmada. Simples assim. Quer comprar ou vender algo?"

◆ FLOW 7 — OUT OF SCOPE MESSAGE
If the user sends something unrelated to buying, selling, negotiating, paying, or delivering physical products (e.g. jokes, recipes, news, philosophical questions, writing requests, translations, personal advice, etc.):
  Acknowledge gently that this is not your domain and redirect to what you do.
  Vary how you say it — never repeat the same phrase. Adapt tone to the user's style.
  Never answer the out-of-scope content, even if it seems simple.
  Never be rude or dismissive — be light-hearted and redirect with good humour.

━━━ RESTRICTION CHECK — MANDATORY ━━━
BEFORE accepting any listing or starting any product search,
you MUST call the check_restriction tool with the product description.

The tool returns one of three responses:
- "ALLOWED: ..." → product cleared, continue normally
- "RESTRICTED: ..." → product prohibited, refuse immediately (see below)
- "DB_UNAVAILABLE" or "CHECK_ERROR" → do not block the user, but note internally and proceed with caution

WHEN TO CALL check_restriction:
- User wants to SELL any product → check before calling list_product
- User wants to BUY any product → check before calling search_product
- User mentions a product that seems regulated, illegal, or unusual → check preventively

HOW TO PASS LOCATION in check_restriction calls:
- Whenever available in context, pass the user's state and municipality — restrictions vary by region and country.
- Use the "mora em" field in context to extract city/neighbourhood → pass as municipality.
- Extract the state code when the city is known (e.g. São Paulo → SP, Rio de Janeiro → RJ,
  Lisbon → PT-11, Buenos Aires → AR-B, New York → NY, London → ENG). If unsure of the exact code, omit the state field.
- Example: check_restriction(product_description="9mm pistol", state="SP", municipality="São Paulo")
- The tool understands the product in any language — pass the description exactly as the user said it.

HOW TO REFUSE when the result is RESTRICTED:
- Be firm and clear, without hostility, and respond in the user's language
- Briefly explain the reason returned by the tool (e.g. applicable law)
- Do not offer alternatives for obtaining the prohibited item
- Do not directly accuse the user — it may just be a misunderstanding
- If the request seems intentional and suspicious: direct them to reply "SUPPORT"
- Vary how you refuse — do not always use the same phrase

━━━ TOOLS — WHEN TO USE ━━━
- User provides/corrects full name → update_name
- User wants to change nickname / provides nickname → update_nickname
- User provides/corrects CPF/tax ID → update_tax_id
- User provides the city/neighbourhood where they LIVE → update_location
- User provides full address (street, number, state, ZIP) → update_full_address
- User provides gender, date of birth, or preferred language → update_profile
- Product mentioned for sale or purchase → check_restriction FIRST, always
- User wants to SELL → check_restriction → if ALLOWED, list_product (immediate)
- User wants to BUY/SEARCH → check_restriction → if ALLOWED, search_product (after steps 1-2 of Flow 1)
- User provides Pix key → update_pix_key
- User provides seller pickup address → update_address
- User requests product alert → save_interest
- User asks to see active alerts / "what am I monitoring?" → list_my_alerts
- User wants to cancel a specific alert → cancel_alert with description
- User wants to cancel ALL alerts → cancel_alerts
- User asks to see their profile / registered data → get_my_profile

"I need X", "I want a X", "I'm looking for X" = PURCHASE → never confuse with selling.

━━━ FACTUAL DATA — NEVER INVENT ━━━
Mandatory use of tools for any factual data:
- Market price, product value → web_search
- Currency conversion → convert_currency
- Numeric calculations (discount, percentage) → calculate
- Unit conversion (kg, km, inches) → convert_units
- Current date or time → get_datetime
Inventing a value causes real financial harm. Always use the tool.

Current user context (real database data):
{contexto}
"""

NOTHA_TOOLS = [tool.to_openai_schema() for tool in ALL_BUILTIN_TOOLS] + [
    {
        "type": "function",
        "function": {
            "name": "update_name",
            "description": (
                "Saves or corrects the user's name. "
                "Use whenever the user tells you their name — whether it is a full name, a first name only, or a single word. "
                "ANY name the user provides is valid and must be saved immediately without questioning it. "
                "Examples: 'my name is João Silva', 'I'm Maria', 'Jedme', 'call me Carlos'. "
                "Do NOT use for explicit nicknames (e.g. 'call me Cris') — use update_nickname for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "User's full/legal name as they provided it"
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_nickname",
            "description": (
                "Saves or changes the user's nickname — how they want to be addressed. "
                "Use when the user indicates a preference for how to be called, "
                "even if they already have a registered name. Can be used at any time. "
                "Examples: 'call me Joe', 'just call me Cris', "
                "'I want to change my nickname to Beta', 'call me just João'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nickname": {
                        "type": "string",
                        "description": "Preferred name or nickname"
                    }
                },
                "required": ["nickname"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_tax_id",
            "description": "Saves or corrects the user's CPF/tax ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tax_id": {
                        "type": "string",
                        "description": "CPF/tax ID provided by the user (may include dots and dashes or only digits)"
                    }
                },
                "required": ["tax_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_product",
            "description": (
                "Starts the complete product listing flow for sale. "
                "CALL IMMEDIATELY when the user expresses any intention to sell a product, "
                "such as 'I want to sell', 'I have an X to sell', 'I want to list', 'selling an X'. "
                "Do NOT try to collect more information before calling — the listing flow "
                "will guide the user through all necessary questions. "
                "Do NOT ask more questions about the product before calling this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Product description mentioned by the user (can be partial)"
                    }
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_product",
            "description": (
                "Searches for products available for purchase. "
                "Before calling: (1) collect product details if the description is vague, "
                "(2) ask which city or neighbourhood the user wants to search in. "
                "Always pass a complete search_description — it will be reused if an alert needs to be saved. "
                "If the user does not want to filter by region, omit search_city and search_neighborhood."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Product category or type being searched"
                    },
                    "search_description": {
                        "type": "string",
                        "description": "Description of what the user wants to buy"
                    },
                    "search_city": {
                        "type": "string",
                        "description": "City where the user wants to search (e.g. 'São Paulo', 'Belo Horizonte'). Leave empty to search nationwide."
                    },
                    "search_neighborhood": {
                        "type": "string",
                        "description": "Specific neighbourhood to search in (e.g. 'Pinheiros', 'Savassi'). Use together with search_city when possible."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_interest",
            "description": (
                "Saves an interest alert: the user will be notified via WhatsApp "
                "as soon as a matching product appears. "
                "Use when the user confirms they want to be notified after a search with no results, "
                "or explicitly mentions 'let me know', 'I want to be notified', etc. "
                "IMPORTANT: use the description already collected in the previous search — do NOT ask the user again. "
                "Pass the full description and the region provided in the search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_description": {
                        "type": "string",
                        "description": "What the user is looking for (e.g. 'round wooden table', 'iPhone 14')"
                    },
                    "category": {
                        "type": "string",
                        "description": "Product category, if identified"
                    },
                    "search_city": {
                        "type": "string",
                        "description": "City of interest (optional — to receive alerts from a specific city only)"
                    },
                    "search_neighborhood": {
                        "type": "string",
                        "description": "Neighbourhood of interest (optional)"
                    }
                },
                "required": ["search_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_alerts",
            "description": (
                "Cancels ALL active search alerts for the user. "
                "Use only when the user explicitly asks to cancel all alerts/notifications. "
                "To cancel a specific alert, use cancel_alert with a description instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_alerts",
            "description": (
                "Lists all active product alerts/watches the user has saved. "
                "Use when the user asks 'what am I monitoring?', 'which alerts do I have?', "
                "'what products am I watching?', or similar."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_alert",
            "description": (
                "Cancels a specific product alert by description. "
                "Use when the user wants to stop monitoring a specific product "
                "(e.g. 'cancel the iPhone alert', 'I no longer want to be notified about sofas'). "
                "Pass the product description to find and cancel the matching alert. "
                "If the user says 'cancel ALL alerts', use cancel_alerts instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Description of the product alert to cancel (e.g. 'iPhone 14', 'wooden table')"
                    }
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_profile",
            "description": (
                "Shows the user their registered profile data. "
                "Use when the user asks 'what data do you have about me?', "
                "'show my profile', 'what is my registered address?', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": (
                "Updates personal profile fields: gender, date of birth, preferred language. "
                "Use when the user provides any of these. "
                "Examples: 'I am male', 'my birthday is 15/03/1990', 'I prefer English'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "gender": {
                        "type": "string",
                        "description": "User's gender: 'M' for male, 'F' for female, or other self-description"
                    },
                    "date_of_birth": {
                        "type": "string",
                        "description": "Date of birth in DD/MM/YYYY or YYYY-MM-DD format"
                    },
                    "preferred_language": {
                        "type": "string",
                        "description": "ISO 639-1 language code (e.g. 'pt', 'en', 'es')"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_full_address",
            "description": (
                "Saves or updates the user's full residential address (street, number, neighbourhood, city, state, ZIP, country). "
                "Use whenever the user provides a STREET NAME or STREET NUMBER or ZIP CODE or STATE — even if only one field. "
                "Also use when the user provides a complete address all at once. "
                "Prefer this over update_location when any field other than city/neighbourhood is present. "
                "Examples: 'Rua das Flores 123, Centro, São Paulo SP 01310-100', 'my street is Av. Paulista', 'ZIP 04538-133'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "street": {
                        "type": "string",
                        "description": "Street name (e.g. 'Rua das Flores', 'Av. Paulista')"
                    },
                    "street_number": {
                        "type": "string",
                        "description": "House/building number (e.g. '123', '456 apto 7')"
                    },
                    "neighborhood": {
                        "type": "string",
                        "description": "Neighbourhood/district"
                    },
                    "city": {
                        "type": "string",
                        "description": "City"
                    },
                    "state": {
                        "type": "string",
                        "description": "State or province (e.g. 'São Paulo', 'SP', 'Rio de Janeiro')"
                    },
                    "country": {
                        "type": "string",
                        "description": "Country (default: Brasil)"
                    },
                    "zip_code": {
                        "type": "string",
                        "description": "Postal/ZIP code (e.g. '04538-133')"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_location",
            "description": (
                "Saves the user's CITY and/or NEIGHBOURHOOD only — nothing else. "
                "Use ONLY when the user mentions city or neighbourhood WITHOUT a street address. "
                "If the user provides a street name, street number, state, or ZIP code, use update_full_address instead. "
                "Examples: 'I live in São Paulo', 'my neighbourhood is Copacabana'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "User's city (e.g. 'São Paulo', 'Campinas', 'Rio de Janeiro')"
                    },
                    "neighborhood": {
                        "type": "string",
                        "description": "User's neighbourhood (e.g. 'Pinheiros', 'Copacabana', 'Savassi')"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_pix_key",
            "description": "Saves the user's Pix key to receive payments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pix_key": {
                        "type": "string",
                        "description": "Pix key (CPF, email, phone number, or random key)"
                    }
                },
                "required": ["pix_key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_address",
            "description": "Saves the user's delivery or pickup address.",
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Full address (street, number, neighbourhood, city, postcode)"
                    }
                },
                "required": ["address"]
            }
        }
    },
]


INTENT_EXTRACTION_PROMPT = """You are an intent extractor for the NOTHA product negotiation system on WhatsApp.

Analyse the message below and extract the structured intent as JSON.

User message: "{message}"
Current context: {context}

━━━ INSTRUCTIONS ━━━
- Return ONLY valid JSON, no extra text
- If there is a monetary value written in words (e.g. "two hundred reais", "one thousand five hundred"), convert to a number
- If the user confirms with "yes", "ok", "sure", "deal", "agreed", "I accept", "sounds good" → intent_type: "confirmation", accepted: true
- If the user refuses with "no", "too expensive", "I don't want it", "I give up", "cancel" → intent_type: "rejection", accepted: false
- If there is a value mentioned in the context of an offer or counteroffer, extract the number

━━━ EXAMPLES ━━━

Simple confirmation:
{{"intent_type": "confirmation", "accepted": true}}

Simple rejection:
{{"intent_type": "rejection", "accepted": false, "reason": "too expensive"}}

Price offer / counteroffer:
{{"intent_type": "counteroffer", "estimated_value": 350.0, "confidence": "high"}}

Delivery confirmation by buyer:
{{"intent_type": "confirm_delivery", "received": true}}

Delivery confirmation by seller:
{{"intent_type": "confirm_delivery_seller", "delivered": true}}

Other:
{{"intent_type": "other", "description": "user asked about opening hours"}}
"""


_UNDERSTAND_PROMPT = """You are the intent analyser for NOTHA, a WhatsApp marketplace for physical products.

Read the user message and full conversation history, then return ONLY valid JSON describing the user's intent.

━━━ USER CONTEXT ━━━
{context}

━━━ CONVERSATION HISTORY ━━━
{history}

━━━ LATEST USER MESSAGE ━━━
{message}

━━━ RETURN FORMAT ━━━
{{
  "objective": "<short English phrase: what the user wants to achieve>",
  "intent": "buy|sell|negotiate|confirm|reject|counteroffer|chitchat|info|onboarding|decline|out_of_scope|other",
  "flow": "product_search|listing|negotiation|payment|delivery|onboarding|greeting|chitchat|out_of_scope|other",
  "needs_tools": true|false,
  "confidence": 0.0-1.0,
  "language": "<ISO 639-1 code of the user's language, e.g. 'pt', 'en', 'es', 'fr', 'de'>",
  "notes": "<any nuance worth noting for the planner, or empty string>"
}}

Rules:
- needs_tools=false ONLY for: pure greetings with no data, clearly out-of-scope messages, or decline responses.
- needs_tools=true for ANY message that contains or implies:
  * a product name, search, or interest ("I want a sofa", "looking for iPhone")
  * personal data: name, CPF, address, street, ZIP, city, state, gender, date of birth, language preference
  * Pix key, pickup address, or payment info
  * a request to see alerts, profile, or cancel something
  * intent to sell (even just mentioning a product for sale)
- intent="decline" when the user refuses or says no to something the agent just offered or proposed
  (e.g. agent asked "want to save an alert?" and user replies "Não", "No", "Não quero", "nah", etc.)
  Set needs_tools=false for decline — no tool call needed, just acknowledge the refusal.
- language: detect from the user's LATEST message. Use prior history if the latest message is ambiguous (e.g. a single emoji).
- Be concise in objective, e.g. "Find iPhone 14 in São Paulo" or "List used sofa for sale"

Examples of needs_tools=true:
- "Sou homem, nasci em 15/03/1990" → needs_tools=true (profile data: gender + date_of_birth)
- "Meu endereço é Rua das Flores 123, SP" → needs_tools=true (address data)
- "Me chamo João" → needs_tools=true (name data)
- "Minha chave Pix é 111.222.333-44" → needs_tools=true (pix key)
- "Quero ver meus alertas" → needs_tools=true (list alerts)

- Return ONLY valid JSON, no extra text"""

_PLAN_PROMPT = """You are the planner for NOTHA, a WhatsApp marketplace for physical products.

The user's objective has been identified. Your job is to produce a precise execution plan.

━━━ USER CONTEXT ━━━
{context}

━━━ CONVERSATION HISTORY ━━━
{history}

━━━ OBJECTIVE ━━━
{objective}

━━━ INTENT ━━━
{intent}

━━━ AVAILABLE TOOLS (name | required params | description) ━━━
{tool_catalog}

━━━ RULES ━━━
1. check_restriction MUST come before search_product or list_product — always.
   For check_restriction, args MUST include "product_description" (string, required).
2. user_message in a step is what NOTHA says to the user BEFORE executing that tool.
   - Set it only for slow/visible steps (search_product, list_product, web_search).
   - For internal checks (check_restriction, update_*, get_datetime) set it to null.
   - Generate the message in the user's language (detect from history), naturally, for WhatsApp.
   - Example for search_product: "🔍 Buscando iPhone 14 disponível pra você, um momento..."
3. args values MUST be real values extracted verbatim from the user's message.
   If a required value is NOT explicitly in the user's message, OMIT that tool from the plan.
   NEVER pass questions, instructions, or placeholders as args values.
4. reason is internal only — the user never sees it.
5. Include only the tools actually needed. Keep the plan minimal.
6. If needs_tools is false, return an empty steps array.

━━━ RETURN FORMAT ━━━
{{
  "steps": [
    {{
      "step": 1,
      "tool": "check_restriction",
      "args": {{"product_description": "iPhone 14 Pro"}},
      "reason": "user wants to buy iPhone — must check restrictions first",
      "user_message": null
    }}
  ]
}}

Return ONLY valid JSON, no extra text."""

_ASSESS_PROMPT = """You are a step result evaluator for NOTHA, a WhatsApp marketplace for physical products.

A tool was just executed. Decide what to do next.

━━━ OBJECTIVE ━━━
{objective}

━━━ TOOL EXECUTED ━━━
{tool_name}

━━━ RESULT ━━━
{result}

━━━ STEPS REMAINING ━━━
{remaining_steps}

━━━ DECISION OPTIONS ━━━
- "continue"  → result is good, execute the next planned step
- "done"      → objective is achieved, proceed to synthesis
- "replan"    → result changed what is needed; provide new steps
- "abort"     → objective cannot be achieved (e.g. product restricted, no results)

━━━ RETURN FORMAT ━━━
{{
  "decision": "continue|done|replan|abort",
  "reason": "<one line why>",
  "progress_message": "<optional short message to send user mid-execution, or null>",
  "new_steps": []
}}

- progress_message: use only if execution is visibly taking time and user should be updated.
  Generate it in the user's language if you include it.
- new_steps: fill only when decision=replan, using the same step format as the planner.
- Return ONLY valid JSON, no extra text."""

_SYNTHESIZE_PROMPT = """You are NOTHA — a physical product buy-and-sell agent on WhatsApp.

Produce the final reply to the user. Write as NOTHA, naturally and concisely.

━━━ IDENTITY AND TONE ━━━
- Tone: human, warm, efficient — like a trusted friend who understands business
- Language: detect the user's language from history and ALWAYS reply in the same language
- Max 3 short sentences unless listing items
- Use emojis sparingly (1-2) when it feels natural
- No markdown (no asterisks, hashtags, underlines)
- Never start with "Hi!", "Hello!" if there is already conversation history
- Never mention AI, GPT, OpenAI, algorithm, LLM

━━━ FIRST PERSON — MANDATORY ━━━
- ALWAYS speak in first person. You ARE NOTHA. Never describe yourself in third person.
- Wrong: "NOTHA é um agente que..." → Correct: "Eu facilito compras e vendas..."
- Wrong: "O NOTHA pode..." → Correct: "Posso..."
- NEVER use "farei o possível", "tentarei", "farei meu melhor" or any hedging phrase. Speak with confidence.

━━━ USER CONTEXT ━━━
{context}

━━━ CONVERSATION HISTORY ━━━
{history}

━━━ OBJECTIVE THAT WAS ATTEMPTED ━━━
{objective}

━━━ OUTCOME ━━━
{outcome}

━━━ TOOL RESULTS COLLECTED ━━━
{tool_results}

━━━ LANGUAGE ━━━
{language_instruction}

━━━ INSTRUCTIONS ━━━
{synthesis_instruction}

Write the reply now. Return ONLY the reply text, nothing else."""


class ConversationAgent:

    # ─── New 4-phase architecture ─────────────────────────────────────────────

    async def understand(
        self,
        user_message: str,
        history: list[dict],
        context: str,
    ) -> dict:
        """Phase 0 — Understand the user's intent and objective.

        Returns a dict with: objective, intent, flow, needs_tools, confidence, notes.
        Fast: no tools, small output, gpt-4o-mini.
        """
        history_fmt = _fmt_history(history, max_messages=15)
        prompt = _UNDERSTAND_PROMPT.format(
            context=context or "no context",
            history=history_fmt,
            message=user_message,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=250,
                json_mode=True,
            )
            result = json.loads(resp.text or "{}")
            logger.info(
                "understand() → intent=%s flow=%s needs_tools=%s objective=%r",
                result.get("intent"), result.get("flow"),
                result.get("needs_tools"), result.get("objective"),
            )
            return result
        except Exception as e:
            logger.error("understand() error: %s", e)
            return {
                "objective": user_message,
                "intent": "other",
                "flow": "other",
                "needs_tools": True,
                "confidence": 0.5,
                "notes": "",
            }

    async def plan(
        self,
        objective: str,
        intent: str,
        history: list[dict],
        context: str,
        tool_catalog: list[dict] | None = None,
        tool_names: list[str] | None = None,
        needs_tools: bool = True,
    ) -> list[dict]:
        """Phase 1 — Build an explicit execution plan.

        Returns a list of step dicts: {step, tool, args, reason, user_message}.
        Empty list when no tools are needed.
        Accepts either tool_catalog (preferred, includes param signatures) or
        tool_names (legacy, names only) for backwards compatibility.
        """
        if not needs_tools:
            return []

        # Format tool catalog for the prompt
        if tool_catalog:
            catalog_lines = []
            for t in tool_catalog:
                req_params = [
                    k for k, v in t.get("parameters", {}).items() if v.get("required")
                ]
                opt_params = [
                    k for k, v in t.get("parameters", {}).items() if not v.get("required")
                ]
                params_str = ""
                if req_params:
                    params_str += f"required: {', '.join(req_params)}"
                if opt_params:
                    params_str += f"{' | ' if params_str else ''}optional: {', '.join(opt_params)}"
                desc = t.get("description", "")[:300]
                catalog_lines.append(f"- {t['name']} [{params_str}] — {desc}")
            catalog_fmt = "\n".join(catalog_lines)
        else:
            catalog_fmt = ", ".join(tool_names or [])

        history_fmt = _fmt_history(history, max_messages=15)
        prompt = _PLAN_PROMPT.format(
            context=context or "no context",
            history=history_fmt,
            objective=objective,
            intent=intent,
            tool_catalog=catalog_fmt,
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=600,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            steps = data.get("steps", [])
            logger.info("plan() → %d step(s): %s", len(steps), [s.get("tool") for s in steps])
            return steps
        except Exception as e:
            logger.error("plan() error: %s", e)
            return []

    async def assess_result(
        self,
        objective: str,
        tool_name: str,
        result: str,
        remaining_steps: list[dict],
    ) -> dict:
        """Phase 2 (per step) — Evaluate a tool result and decide what to do next.

        Returns: {decision, reason, progress_message, new_steps}
        decision: "continue" | "done" | "replan" | "abort"
        """
        prompt = _ASSESS_PROMPT.format(
            objective=objective,
            tool_name=tool_name,
            result=result[:1500],  # cap to avoid token waste
            remaining_steps=json.dumps(remaining_steps, ensure_ascii=False),
        )
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                json_mode=True,
            )
            data = json.loads(resp.text or "{}")
            logger.info(
                "assess_result() tool=%s → decision=%s reason=%r",
                tool_name, data.get("decision"), data.get("reason"),
            )
            return data
        except Exception as e:
            logger.error("assess_result() error: %s", e)
            return {"decision": "continue", "reason": str(e), "progress_message": None, "new_steps": []}

    async def synthesize(
        self,
        objective: str,
        outcome: str,
        tool_results: dict[str, str],
        history: list[dict],
        context: str,
        synthesis_instruction: str = "",
        user_message: str = "",
        user_language: str = "",
    ) -> str:
        """Phase 3 — Synthesize collected results into a final natural reply.

        outcome: "done" | "abort" | "no_tools" (direct response, no tools needed)
        synthesis_instruction: extra guidance for the LLM (e.g. listing results text)
        user_message: the current user message (not yet in history); used to
                      correctly determine greeting state and guardrail evaluation.
        user_language: ISO 639-1 code detected from the user's messages. When
                       provided the LLM is instructed to reply in that language.
        """
        history_fmt = _fmt_history(history, max_messages=20)
        results_fmt = "\n".join(
            f"[{k}]: {v[:800]}" for k, v in tool_results.items()
        ) if tool_results else "(no tools were executed)"

        if user_language:
            language_instruction = (
                f"You MUST write your reply in the language with ISO 639-1 code '{user_language}'. "
                f"Do NOT use any other language, even if the tool results or context are in a different language."
            )
        else:
            language_instruction = "Match the language the user is writing in."

        prompt = _SYNTHESIZE_PROMPT.format(
            context=context or "no context",
            history=history_fmt,
            objective=objective,
            outcome=outcome,
            tool_results=results_fmt,
            language_instruction=language_instruction,
            synthesis_instruction=synthesis_instruction or "Generate the appropriate response.",
        )

        has_history = len(history) > 0
        # Use the current user message if provided; only fall back to history
        # when not available. This prevents using a stale previous message
        # (e.g. a greeting) as the reference for the current turn.
        current_user_msg = user_message or next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        user_greeted = _is_pure_greeting(current_user_msg)

        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or "Done!"
            sanitized = await _sanitize_response(reply, has_history, user_greeted)
            history_for_guardrail = list(history) + [{"role": "user", "content": current_user_msg}]
            return await validate_reply(
                sanitized, history_for_guardrail, context, current_user_msg,
                objective=objective,
            )
        except Exception as e:
            logger.error("synthesize() error: %s", e)
            return ""

    # ─── Legacy helpers (kept for listing flow, negotiation, speak, etc.) ──────

    async def get_tool_calls(
        self,
        contexto: str,
        history: list[dict],
        user_message: str,
        tools: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Phase 1 of tool calling: sends messages and returns the tool calls the LLM wants to make.

        Returns (messages_so_far, tool_calls).
        messages_so_far must be passed to get_reply_after_tools along with the real results.
        """
        system = SYSTEM_PROMPT.format(contexto=contexto)
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        try:
            resp = await get_provider().complete(
                messages=messages,
                tools=tools,
                temperature=0.6,
                max_tokens=500,
            )
        except Exception as e:
            logger.error("Error in get_tool_calls: %s", e)
            return messages, []

        tool_calls: list[dict] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.args}
            for tc in resp.tool_calls
        ]

        messages.append({
            "role": "assistant",
            "content": resp.text,
            **({"tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                for tc in tool_calls
            ]} if tool_calls else {}),
        })

        return messages, tool_calls

    async def continue_with_results(
        self,
        messages: list[dict],
        tool_results: dict[str, str],
        tools: list[dict],
        contexto: str = "",
    ) -> tuple[list[dict], list[dict]]:
        """Feeds tool results back to the LLM as role:tool messages and calls it again
        with tools available — allowing the LLM to chain tool calls (e.g. check_restriction
        → search_product) rather than stopping at the first round.

        Returns (updated_messages, new_tool_calls).
        If new_tool_calls is empty, the LLM generated a final text response.

        Multi-turn safety: the messages array may already contain role:tool entries
        injected in a previous iteration of the agentic loop. We check whether the
        message immediately following an assistant-with-tool_calls is already a tool
        message; if so, we copy the existing results instead of adding new ones.
        This prevents duplicate/invalid tool message sequences on iterations 2+.
        """
        msgs: list[dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            msgs.append(msg)

            if msg["role"] == "assistant" and msg.get("tool_calls"):
                next_is_tool = (
                    i + 1 < len(messages) and messages[i + 1]["role"] == "tool"
                )
                if next_is_tool:
                    # Tool results for this assistant message are already in the array;
                    # they will be copied naturally in subsequent iterations of this loop.
                    pass
                else:
                    # This is the latest (unanswered) assistant tool call — inject results.
                    for tc in msg["tool_calls"]:
                        tc_id = tc["id"]
                        result = tool_results.get(tc_id, "no result")
                        msgs.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result,
                        })
            i += 1

        try:
            resp = await get_provider().complete(
                messages=msgs,
                tools=tools,
                temperature=0.6,
                max_tokens=500,
            )
        except Exception as e:
            logger.error("Error in continue_with_results: %s", e)
            return msgs, []

        new_tool_calls: list[dict] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.args}
            for tc in resp.tool_calls
        ]

        msgs.append({
            "role": "assistant",
            "content": resp.text,
            **({"tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                for tc in new_tool_calls
            ]} if new_tool_calls else {}),
        })

        return msgs, new_tool_calls

    async def get_reply_after_tools(
        self,
        messages: list[dict],
        tool_results: dict[str, str],
        contexto: str = "",
    ) -> str:
        """Phase 2 of tool calling: generates the final response with the tool results.

        Results are injected into the system prompt as additional context — not as
        role:'tool' messages. This ensures the user's message stays as the last item
        in the chain, preserving conversational continuity and preventing the LLM
        from "restarting" the conversation with greetings.

        tool_results: dict of tool_call_id → descriptive result (real DB data).
        contexto: user context string (from _build_context) for the guardrail.
        """
        tool_context = "\n\n━━━ DATA RETRIEVED BY TOOLS ━━━\n"
        for result in tool_results.values():
            tool_context += result + "\n"
        tool_context += "━━━ END OF DATA ━━━"

        rebuilt: list[dict] = []
        for msg in messages:
            if msg["role"] == "system":
                rebuilt.append({"role": "system", "content": msg["content"] + tool_context})
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                continue
            else:
                rebuilt.append(msg)

        has_history = sum(1 for m in rebuilt if m["role"] == "user") > 1
        last_user_msg = next(
            (m["content"] for m in reversed(rebuilt) if m["role"] == "user"), ""
        )
        user_greeted = _is_pure_greeting(last_user_msg)

        history_for_guardrail = [
            m for m in rebuilt if m.get("role") in ("user", "assistant")
        ]

        try:
            resp = await get_provider().complete(
                messages=rebuilt,
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or "Done!"
            sanitized = await _sanitize_response(reply, has_history, user_greeted)
            return await validate_reply(
                sanitized, history_for_guardrail, contexto, last_user_msg
            )
        except Exception as e:
            logger.error("Error in get_reply_after_tools: %s", e)
            return "Done!"

    async def chat_with_tools(
        self,
        contexto: str,
        history: list[dict],
        user_message: str,
        tools: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Shortcut for when there are no tools or the two phases are not needed separately."""
        system = SYSTEM_PROMPT.format(contexto=contexto)
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        has_history = len(history) > 0
        user_greeted = _is_pure_greeting(user_message)
        try:
            resp = await get_provider().complete(
                messages=messages,
                tools=tools or None,
                temperature=0.6,
                max_tokens=500,
            )
        except Exception as e:
            logger.error("Error in chat_with_tools: %s", e)
            return "I had a technical issue. Please send your message again in a moment!", []

        reply = resp.text or "I had a technical issue."
        sanitized = await _sanitize_response(reply, has_history, user_greeted)
        history_for_guardrail = list(history) + [{"role": "user", "content": user_message}]
        validated = await validate_reply(sanitized, history_for_guardrail, contexto, user_message)
        return validated, []

    async def respond(
        self,
        phone: str,
        user_message: str,
        history: list[dict],
        role: str = "general",
        product_info: str = "no product in context",
        negotiation_status: str = "no active negotiation",
        user_name: str = "not provided yet",
    ) -> str:
        context = (
            f"Name: {user_name} | Role: {role} | "
            f"Product: {product_info} | Negotiation: {negotiation_status}"
        )
        text, _ = await self.chat_with_tools(
            contexto=context,
            history=history,
            user_message=user_message,
            tools=None,
        )
        return text

    async def extract_intent(self, message: str, contexto: str = "general") -> dict:
        prompt = INTENT_EXTRACTION_PROMPT.format(message=message, context=contexto)
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                json_mode=True,
            )
            return json.loads(resp.text or "{}")
        except Exception as e:
            logger.error("Error extracting intent: %s", e)
            return {"intent_type": "other", "description": message}

    async def speak(
        self,
        instruction: str,
        history: list[dict] | None = None,
        contexto: str = "",
    ) -> str:
        """Generates a response to the user with full history and context.

        The backend specifies *what* to communicate via instruction; the agent decides
        *how* to say it, maintaining tone and conversational continuity.
        Replaces build_reply and ask_confirmation.
        """
        history = history or []
        system = SYSTEM_PROMPT.format(contexto=contexto or "no context available")
        system += (
            "\n\n━━━ SYSTEM INSTRUCTION ━━━\n"
            f"{instruction}\n"
            "Transform into a natural WhatsApp message. Do not use technical terms."
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)

        has_history = len(history) > 0
        last_user_msg = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        user_greeted = _is_pure_greeting(last_user_msg)
        try:
            resp = await get_provider().complete(
                messages=messages,
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or instruction
            sanitized = await _sanitize_response(reply, has_history, user_greeted)
            return await validate_reply(sanitized, history, contexto, last_user_msg)
        except Exception as e:
            logger.error("Error in speak: %s", e)
            return instruction
