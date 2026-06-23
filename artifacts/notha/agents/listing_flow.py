"""
ListingFlowAgent — state machine for complete product listing via WhatsApp.
ContentSafetyAgent is integrated at the product-description step (selective,
heuristic-gated) — only suspicious descriptions trigger an LLM safety check.

MOVABLE PRODUCTS (electronics, appliances, furniture, vehicles, clothing, etc.):
  product          → What do you want to sell?
  brand_model      → Brand, model and version
  usage_state      → New or used?
  condition        → Condition
  receipt          → Do you have a receipt?
  photos_upload    → Product photos (multiple; text = "done")
  address          → Pickup address (where buyer/courier will collect)
  price            → Desired price and minimum acceptable
  processing       → [automatic] web search + DB + vision + pricing
  review_condition → [conditional] paused when vision detects inconsistent condition
  confirm          → Summary and confirmation
  done             → Listing created

FIXED-LOCATION PRODUCTS (real estate, businesses, commercial spaces, land, etc.):
  product          → What do you want to sell?
  photos_upload    → Property/business photos (multiple; text = "done")
  location_address → Address/location of the property or business
  price            → Desired price and minimum acceptable
  processing       → [automatic] web search + DB + vision + pricing
  confirm          → Summary and confirmation
  done             → Listing created

  Note: fixed-location products skip brand_model, usage_state, condition, receipt,
        and do NOT request a pickup address — they have a fixed location instead.
        No logistics service is needed for these listings.
"""
import json
import logging
from llm import get_provider

logger = logging.getLogger("notha.agent.listing_flow")


def _parse_jsonb(value, default):
    """Converts asyncpg JSONB value to Python — supports dict/list or JSON string."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


CONDITION_LABEL = {
    "like_new":  "Like new (no signs of use)",
    "good":      "Good condition (light use, few marks)",
    "fair":      "Fair condition (normal use, minor wear)",
    "worn":      "Worn (heavy use, visible marks)",
    "defective": "Defective (partially or fully non-functional)",
}


class ListingFlowAgent:

    # ─────────────────────────────────────────────
    # Guardrails — extraction with evidence and retry
    # ─────────────────────────────────────────────

    _EXTRACT_GUARDRAIL = (
        "\n\nALWAYS RETURN VALID JSON following these MANDATORY EXTRACTION RULES:\n"
        "1. For each extracted field, include an 'evidence_<field>' field with the EXACT excerpt "
        "   from the user's message that supports the value. If there is no excerpt to support it, "
        "   the main field MUST be null and 'evidence_<field>' MUST be null.\n"
        "2. NEVER invent, infer, or assume values. Only extract what was explicitly stated.\n"
        "3. NEVER complete implicit information (e.g. user said 'iPhone 13' without citing 'Apple' "
        "   → brand=null, not 'Apple').\n"
        "4. When in doubt, prefer null over an uncertain value."
    )

    async def _extract(self, system: str, user_msg: str) -> dict:
        """Base extraction — use _extract_validated() in business steps."""
        try:
            resp = await get_provider().complete(
                messages=[
                    {"role": "system", "content": system + self._EXTRACT_GUARDRAIL},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=400,
                json_mode=True,
            )
            return json.loads(resp.text or "{}")
        except Exception as e:
            logger.error(f"LLM extraction error: {e}")
            return {}

    async def _extract_validated(
        self,
        system: str,
        user_msg: str,
        validators: dict,
        max_retries: int = 2,
    ) -> dict:
        """
        Extraction with guardrails:
          - Requires an 'evidence_<field>' field for each extracted field
          - Verifies that the evidence is a real substring of the user message
          - Applies validators[field](value) for each field — returns None if invalid
          - Retry with error feedback (max_retries attempts)

        validators: {field: callable(value) -> validated_value | None}
        """
        messages = [
            {"role": "system", "content": system + self._EXTRACT_GUARDRAIL},
            {"role": "user", "content": user_msg},
        ]
        last_result: dict = {}

        for attempt in range(max_retries):
            try:
                resp = await get_provider().complete(
                    messages=messages,
                    temperature=0.0,
                    max_tokens=500,
                    json_mode=True,
                )
                raw = json.loads(resp.text or "{}")
            except Exception as e:
                logger.error(f"Validated extraction failed (attempt {attempt+1}): {e}")
                break

            errors: list[str] = []
            result: dict = {}
            user_lower = user_msg.lower()

            for field, validator in validators.items():
                raw_value = raw.get(field)
                evidence = raw.get(f"evidence_{field}")

                if raw_value is not None:
                    if evidence is None or str(evidence).lower() not in user_lower:
                        errors.append(
                            f"Field '{field}': value '{raw_value}' extracted without textual evidence in the user message. "
                            f"Return null for '{field}' and null for 'evidence_{field}'."
                        )
                        result[field] = None
                        continue

                validated_value = validator(raw_value)
                if raw_value is not None and validated_value is None:
                    errors.append(
                        f"Field '{field}': value '{raw_value}' is invalid. "
                        f"Return null or one of the allowed values. Include 'evidence_{field}' with the exact excerpt."
                    )
                result[field] = validated_value

            last_result = result

            if not errors:
                return result

            logger.warning(f"Extraction with errors (attempt {attempt+1}): {errors}")
            messages.append({"role": "assistant", "content": json.dumps(raw)})
            messages.append({
                "role": "user",
                "content": (
                    "Your extraction has problems. Correct them and return a new valid JSON:\n"
                    + "\n".join(f"- {e}" for e in errors)
                ),
            })

        return last_result

    # ─────────────────────────────────────────────
    # Reusable validators
    # ─────────────────────────────────────────────

    @staticmethod
    def _val_condition(v):
        valid = {"like_new", "good", "fair", "worn", "defective"}
        return v if isinstance(v, str) and v in valid else None

    @staticmethod
    def _val_usage_state(v):
        return v if isinstance(v, str) and v in {"new", "used"} else None

    @staticmethod
    def _val_bool(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            if v.lower() in ("true", "sim", "yes", "1"):
                return True
            if v.lower() in ("false", "não", "nao", "no", "0"):
                return False
        return None

    @staticmethod
    def _val_price(v):
        """Price must be a positive number between R$1 and R$9,999,999."""
        try:
            f = float(v)
            if 1.0 <= f <= 9_999_999.0:
                return round(f, 2)
        except (TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _val_str_or_none(v):
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    @staticmethod
    def _val_ready(v):
        if isinstance(v, bool):
            return v
        return None

    # ─────────────────────────────────────────────
    # Product type classification
    # ─────────────────────────────────────────────

    async def _classify_product_type(self, description: str) -> dict:
        """
        Classifies the product as movable or fixed_location using the LLM.
        Works in any language — no keyword lists.

        Returns:
          {
            "product_type": "movable" | "fixed_location",
            "needs_logistics": True | False,
            "type_label": str  (short label in the user's language, e.g. "imóvel", "business", "producto físico")
          }
        """
        try:
            resp = await get_provider().complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify the item described by the user into one of two categories.\n\n"
                            "FIXED_LOCATION — items that have a permanent physical address and cannot be shipped:\n"
                            "  - Real estate: apartments, houses, land, farms, warehouses, commercial units\n"
                            "  - Businesses for sale: restaurants, shops, bakeries, clinics, hotels, franchises, any operating business\n"
                            "  - Commercial spaces: offices, showrooms, stores\n\n"
                            "MOVABLE — anything that can be physically picked up and transported:\n"
                            "  - Electronics, appliances, furniture, clothing, vehicles, tools, toys, books, animals, etc.\n\n"
                            "The description may be in ANY language. Understand it as-is.\n\n"
                            "Return ONLY valid JSON:\n"
                            "{\"product_type\": \"movable\" | \"fixed_location\", \"type_label\": \"<3–5 word label in the same language as the description>\"}\n\n"
                            "Examples:\n"
                            "  'lanchonete' → {\"product_type\": \"fixed_location\", \"type_label\": \"negócio comercial\"}\n"
                            "  'iPhone 13' → {\"product_type\": \"movable\", \"type_label\": \"produto físico\"}\n"
                            "  'apartment in London' → {\"product_type\": \"fixed_location\", \"type_label\": \"real estate\"}\n"
                            "  'tienda de ropa' → {\"product_type\": \"fixed_location\", \"type_label\": \"negocio comercial\"}\n"
                            "  'laptop' → {\"product_type\": \"movable\", \"type_label\": \"produto físico\"}"
                        ),
                    },
                    {"role": "user", "content": description},
                ],
                temperature=0.0,
                max_tokens=80,
                json_mode=True,
            )
            raw = json.loads(resp.text or "{}")
            product_type = raw.get("product_type", "movable")
            if product_type not in ("movable", "fixed_location"):
                product_type = "movable"
            return {
                "product_type":    product_type,
                "needs_logistics": product_type == "movable",
                "type_label":      raw.get("type_label", "product"),
            }
        except Exception as e:
            logger.warning(f"Product type classification failed: {e} — defaulting to movable")
            return {
                "product_type":    "movable",
                "needs_logistics": True,
                "type_label":      "product",
            }

    async def _reply(self, instruction: str) -> str:
        """
        Generates a conversational response from a script instruction.
        The LLM can only write the message — it does not decide data, does not invent information.
        """
        try:
            resp = await get_provider().complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are NOTHA, a product sales assistant via WhatsApp. "
                            "Your only role here is to write the message described in the instruction, "
                            "in a short, direct and natural way. Maximum 3 sentences. No markdown. "
                            "Detect the user's language from the conversation context and reply in that language.\n\n"
                            "PROHIBITED:\n"
                            "- Inventing or inferring data not in the instruction\n"
                            "- Suggesting prices or values not provided\n"
                            "- Asking questions beyond what the instruction requests\n"
                            "- Starting with greetings (Hi!, Hello!, Sure!, Perfect!)\n"
                            "- Giving product information beyond what was provided\n"
                            "- Promising features or deadlines not confirmed"
                        ),
                    },
                    {"role": "user", "content": instruction},
                ],
                temperature=0.4,
                max_tokens=250,
            )
            return resp.text or instruction
        except Exception:
            return instruction

    # ─────────────────────────────────────────────
    # Flow entry point
    # ─────────────────────────────────────────────

    async def start(self) -> str:
        return await self._reply(
            "Start the listing by asking what the user wants to sell. "
            "One simple and direct question, at most one sentence."
        )

    # ─────────────────────────────────────────────
    # Main dispatcher
    # ─────────────────────────────────────────────

    async def handle_message(
        self,
        flow: dict,
        text: str,
        seller_profile=None,
        db=None,
    ) -> tuple[dict, list, str, bool]:
        """
        Processes a text message in the listing flow.

        Returns: (data, photos, reply, completed)
          - completed=True when the step is 'confirm' and the user confirmed
        """
        step = flow["step"]
        data   = _parse_jsonb(flow.get("data"), {})
        photos = _parse_jsonb(flow.get("photos"), [])

        handlers = {
            "product":          self._step_product,
            "brand_model":      self._step_brand_model,
            "usage_state":      self._step_usage_state,
            "condition":        self._step_condition,
            "receipt":          self._step_receipt,
            "photos_upload":    self._step_photos_text,
            "address":          self._step_address,
            "location_address": self._step_location_address,
            "price":            self._step_price,
            "review_condition": self._step_review_condition,
            "confirm":          self._step_confirm,
        }

        handler = handlers.get(step)
        if not handler:
            return data, photos, "", False

        if step in ("photos_upload", "address", "location_address"):
            return await handler(data, photos, text, seller_profile)
        elif step == "price":
            return await handler(data, photos, text, db)
        else:
            return await handler(data, photos, text)

    async def handle_media(
        self,
        flow: dict,
        media_id: str,
        mime_type: str,
        caption: str,
    ) -> tuple[list, str]:
        """
        Processes received media. Only accepts photos during the 'photos_upload' step.
        Returns (photos_updated, reply).
        """
        step   = flow["step"]
        photos = _parse_jsonb(flow.get("photos"), [])

        if step != "photos_upload":
            return photos, ""

        photos.append({"media_id": media_id, "mime_type": mime_type, "caption": caption or ""})
        n = len(photos)
        if n == 1:
            reply = await self._reply(
                "Received the first product photo! "
                "Tell the user they can send more photos from different angles, the label, or packaging. "
                "When done, just type 'done'."
            )
        else:
            reply = await self._reply(
                f"Received photo {n}! Send more or type 'done' when finished."
            )
        return photos, reply

    # ─────────────────────────────────────────────
    # Step handlers
    # ─────────────────────────────────────────────

    async def _step_product(self, data, photos, text):
        description = text.strip()
        data["description"] = description

        # ── Selective content safety check ─────────────────────────────────
        # Only activated when heuristics flag something suspicious.
        # On any LLM/import error, we default to safe=True (never block on failure).
        try:
            from agents.content_safety import ContentSafetyAgent as _CSA
            _csa = _CSA()
            _should_check, _signals = _csa.should_check(description, "")
            if _should_check:
                _safety = await _csa.evaluate(description, "", _signals)
                if not _safety.get("safe", True):
                    data["safety_flag"] = _safety.get("reason", "conteúdo suspeito")
                    data["status"] = "em_revisao_manual"
                    logger.warning(
                        "Listing flagged for manual review: reason=%s",
                        data["safety_flag"],
                    )
                    return data, photos, await self._reply(
                        "Tell the user their listing was received and is under a quick review "
                        "process that usually takes up to 24 hours. After that it will be published "
                        "automatically if approved. Do not mention AI or algorithms."
                    ), False
        except Exception as _safety_err:
            logger.warning("Content safety check skipped (error): %s", _safety_err)

        # Classify product type to determine flow path
        classification = await self._classify_product_type(description)
        data["product_type"]    = classification["product_type"]
        data["needs_logistics"] = classification["needs_logistics"]
        data["type_label"]      = classification["type_label"]

        if classification["product_type"] == "fixed_location":
            # Fixed-location products (real estate, businesses): go straight to photos
            question = await self._reply(
                f"The user wants to sell a fixed-location asset: '{text}' "
                f"(classified as: {classification['type_label']}). "
                "Ask them to send photos of the property or business — "
                "they can send as many as they like from different angles. "
                "When done sending photos, just type 'done'."
            )
            data["step_next"] = "photos_upload"
        else:
            # Movable products: standard flow with brand/model/condition/etc.
            question = await self._reply(
                f"The user wants to sell: '{text}'. "
                "Now ask for the brand, model and version (if applicable). "
                "Example expected response: 'iPhone 13 Pro, 256GB' or 'Nike Air Max 90'. "
                "If there is no brand/model, they can answer 'no brand' or 'don't know'."
            )
            data["step_next"] = "brand_model"

        return data, photos, question, False

    async def _step_brand_model(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "Extract brand, model and version from the text.\n"
                "Return JSON with fields: brand, model, version.\n"
                "Examples:\n"
                "  'iPhone 13 Pro 256GB' → brand=null (user did not say 'Apple'), model='iPhone 13 Pro', version='256GB'\n"
                "  'Nike Air Max 90' → brand='Nike', model='Air Max 90', version=null\n"
                "  'no brand' or 'don't know' → all null\n"
                "If the user did not explicitly mention the brand, return brand=null. "
                "Do not fill in brands you 'know' — only extract what was actually said."
            ),
            user_msg=text,
            validators={
                "brand":   self._val_str_or_none,
                "model":   self._val_str_or_none,
                "version": self._val_str_or_none,
            },
        )
        data.update({
            "brand":   ext.get("brand"),
            "model":   ext.get("model"),
            "version": ext.get("version"),
        })
        question = await self._reply(
            "Ask if the product is new (never used, may still be in the box) or used."
        )
        data["step_next"] = "usage_state"
        return data, photos, question, False

    async def _step_usage_state(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "Determine whether the product is new or used based EXCLUSIVELY on what the user said.\n"
                "Allowed values for 'usage_state': 'new' or 'used'.\n"
                "Words indicating new: new, never used, sealed, in the box, brand new, zero km, nunca usado, lacrado, na caixa, zerado.\n"
                "When in doubt, return null — do not automatically assume 'used'."
            ),
            user_msg=text,
            validators={"usage_state": self._val_usage_state},
        )
        data["usage_state"] = ext.get("usage_state") or "used"
        options = "\n".join(f"  {i+1}. {v}" for i, v in enumerate(CONDITION_LABEL.values()))
        question = await self._reply(
            f"Product declared as {data['usage_state']}. "
            "Now ask about the condition. The options are:\n"
            f"{options}\n"
            "Ask the user to choose a number or describe it in their own words."
        )
        data["step_next"] = "condition"
        return data, photos, question, False

    async def _step_condition(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "Classify the condition based ONLY on what the user said.\n"
                "Allowed values for 'condition': like_new, good, fair, worn, defective.\n"
                "Number mapping: 1=like_new, 2=good, 3=fair, 4=worn, 5=defective.\n"
                "In 'condition_description', copy the user's exact words — do not paraphrase.\n"
                "If the message is ambiguous, return condition=null."
            ),
            user_msg=text,
            validators={
                "condition":             self._val_condition,
                "condition_description": self._val_str_or_none,
            },
        )
        data["condition"]             = ext.get("condition") or "fair"
        data["condition_description"] = ext.get("condition_description") or text.strip()
        question = await self._reply("Ask if the product has a receipt.")
        data["step_next"] = "receipt"
        return data, photos, question, False

    async def _step_receipt(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "The user is saying whether the product has a receipt.\n"
                "Extract only the field 'has_receipt' (true or false).\n"
                "Words indicating 'has receipt': yes, have, includes, came with, it has, tem, sim, tenho, possui, veio com, inclui.\n"
                "Words indicating 'no receipt': no, without, lost, don't have, não, sem, perdi, não tenho, não tem.\n"
                "If ambiguous, return null — do not automatically assume false."
            ),
            user_msg=text,
            validators={"has_receipt": self._val_bool},
        )
        data["has_receipt"] = ext.get("has_receipt") if ext.get("has_receipt") is not None else False
        question = await self._reply(
            "Instruct the user to send product photos now. "
            "Say they can send multiple photos showing different angles, "
            "and can also photograph the label, packaging or receipt if they have one. "
            "When done, just type 'done'."
        )
        data["step_next"] = "photos_upload"
        return data, photos, question, False

    async def _step_photos_text(self, data, photos, text, seller_profile):
        """Text received during the photos step — usually indicates they're done."""
        is_fixed = data.get("product_type") == "fixed_location"

        if not photos:
            if is_fixed:
                reply = await self._reply(
                    "No photo received yet. Please send at least one photo of the property or business to continue!"
                )
            else:
                reply = await self._reply(
                    "No photo received yet. Please send at least one product photo to continue!"
                )
            return data, photos, reply, False

        ready = await self._extract_validated(
            system=(
                "The user is in the process of sending photos.\n"
                "Determine ONLY whether the message indicates they have finished sending photos.\n"
                "Field 'ready': true if the message signals completion, false if they want to send more.\n"
                "Words indicating completion: done, ok, that's it, finished, go ahead, continue, all done, pronto, ok, é isso, terminei, pode seguir.\n"
                "If the message is a question, comment or description — it is not 'ready'."
            ),
            user_msg=text,
            validators={"ready": self._val_ready},
        )
        if not ready.get("ready", False):
            data["photo_notes"] = text
            reply = await self._reply("Got it! Send more photos or type 'done' to continue.")
            return data, photos, reply, False

        if is_fixed:
            # Fixed-location: ask for the address WHERE the property/business is located
            question = await self._reply(
                f"Received {len(photos)} photo(s)! "
                f"Now I need the address of the {data.get('type_label', 'property/business')}. "
                "Ask for the full address: street, number, neighbourhood, city and state."
            )
            data["step_next"] = "location_address"
        else:
            # Movable product: ask for pickup address
            pickup_address = (seller_profile or {}).get("pickup_address")
            if pickup_address:
                question = await self._reply(
                    f"Received {len(photos)} photo(s)! "
                    f"The registered pickup address is: {pickup_address}. "
                    "Ask if they want to use this address or provide a different one for this product."
                )
            else:
                question = await self._reply(
                    f"Received {len(photos)} photo(s)! "
                    "Now I need the pickup address after the sale. "
                    "Ask for the full address: street, number, neighbourhood, city and postcode."
                )
            data["_suggested_address"] = pickup_address
            data["step_next"] = "address"

        return data, photos, question, False

    async def _step_location_address(self, data, photos, text, seller_profile):
        """Collects the location address for fixed-location products (real estate, businesses)."""
        data["location_address"] = text.strip()
        data["pickup_address"]   = None  # Explicitly no pickup address for fixed-location
        question = await self._reply(
            "Now ask what selling price the seller wants to list at, "
            "and what is the minimum they would accept. "
            "Explain that the minimum is confidential and will never be revealed to the buyer."
        )
        data["step_next"] = "price"
        return data, photos, question, False

    async def _step_address(self, data, photos, text, seller_profile):
        suggested = data.get("_suggested_address")
        if suggested:
            ext = await self._extract_validated(
                system=(
                    "The user was asked whether to confirm the registered address or provide a new one.\n"
                    "Extract:\n"
                    "  'confirms_existing': true if the user accepted the already registered address.\n"
                    "  'new_address': string with the new address, or null if none provided.\n"
                    "Confirmation words: yes, use that one, same one, the registered one, ok, sure, sim, pode usar, esse mesmo, o cadastrado, tá bom.\n"
                    "NEVER invent an address — if the user did not provide address text, new_address=null."
                ),
                user_msg=text,
                validators={
                    "confirms_existing": self._val_bool,
                    "new_address":       self._val_str_or_none,
                },
            )
            if ext.get("confirms_existing"):
                data["pickup_address"] = suggested
            elif ext.get("new_address"):
                data["pickup_address"] = ext["new_address"]
            else:
                data["pickup_address"] = text.strip()
        else:
            data["pickup_address"] = text.strip()

        question = await self._reply(
            "Now ask what selling price the seller wants to list the product at, "
            "and what is the minimum they would accept. "
            "Explain that the minimum is confidential and will never be revealed to the buyer."
        )
        data["step_next"] = "price"
        return data, photos, question, False

    async def _step_price(self, data, photos, text, db):
        ext = await self._extract_validated(
            system=(
                "The user is providing the sale price and/or the minimum price they would accept.\n"
                "Extract:\n"
                "  'asking_price': numeric value in the local currency (e.g. 'I want 500' → 500.0), or null.\n"
                "  'seller_min_price': numeric minimum acceptable value (e.g. 'I accept at least 400' → 400.0), or null.\n"
                "Values written in words are accepted: 'five hundred' → 500.\n"
                "NEVER invent a minimum price if the user did not mention one. "
                "NEVER round or adjust the value — use exactly what the user said."
            ),
            user_msg=text,
            validators={
                "asking_price":    self._val_price,
                "seller_min_price": self._val_price,
            },
        )
        data["asking_price"]    = ext.get("asking_price")
        data["seller_min_price"] = ext.get("seller_min_price")
        data["step_next"] = "processing"
        reply = await self._reply(
            "Tell the user you received everything and will now search for the product online "
            "and in the platform history to suggest the best price. "
            "Say this takes a few seconds."
        )
        return data, photos, reply, False

    async def _step_review_condition(self, data, photos, text):
        """
        Pause activated when visual analysis detects inconsistency with the declared condition.

        Shows the seller what was detected in the photos and offers two options:
          1. Keep the declared condition (they confirm it's correct)
          2. Correct the condition (choose one of the 5 options)

        Only advances to 'confirm' after a valid response.
        """
        vision_data = _parse_jsonb(data.get("vision_analysis"), {})
        _vis_state  = vision_data.get("visual_state") or {}
        visual_desc = _vis_state.get("description", "") or vision_data.get("visual_description", "")
        current_condition = data.get("condition", "fair")

        valid_options = list(CONDITION_LABEL.keys())
        options_text = "\n".join(
            f"  {i+1}. {v}" for i, v in enumerate(CONDITION_LABEL.values())
        )

        ext = await self._extract_validated(
            system=(
                "The seller is responding about the product's condition.\n"
                "They may be confirming the declared condition or correcting it to a new one.\n\n"
                "Extract:\n"
                "  'kept': true if they confirmed keeping the current condition, false if they want to correct it.\n"
                f"  'new_condition': value of the new condition if corrected, null if kept.\n"
                f"Valid values for 'new_condition': {', '.join(valid_options)}.\n"
                "Number mapping: 1=like_new, 2=good, 3=fair, 4=worn, 5=defective.\n"
                "Confirmation words: yes, keep it, that's correct, it's fine, exactly, sim, mantenho, está correto, pode deixar.\n"
                "If ambiguous, kept=false and new_condition=null (ask again)."
            ),
            user_msg=text,
            validators={
                "kept":          self._val_bool,
                "new_condition": self._val_condition,
            },
        )

        kept          = ext.get("kept")
        new_condition = ext.get("new_condition")

        if kept is True:
            data["condition_revised"] = False
            data["step_next"] = "confirm"
            reply = await self._reply(
                f"The seller kept the declared condition: {CONDITION_LABEL.get(current_condition, current_condition)}. "
                "Confirm it is registered and that we will proceed to the listing summary."
            )
            return data, photos, reply, False

        if new_condition:
            data["condition"]             = new_condition
            data["condition_description"] = f"Corrected by seller after visual analysis: {text.strip()}"
            data["condition_revised"]     = True
            data["step_next"] = "confirm"
            reply = await self._reply(
                f"The seller corrected the condition to: {CONDITION_LABEL.get(new_condition, new_condition)}. "
                "Confirm the correction and say we will proceed to the summary."
            )
            return data, photos, reply, False

        msg = await self._reply(
            f"Response not understood. Present the condition options and ask which applies:\n"
            f"{options_text}\n"
            "Or say 'yes' to confirm the already declared condition."
        )
        return data, photos, msg, False

    async def _step_confirm(self, data, photos, text):
        ext = await self._extract_validated(
            system=(
                "The user is responding to the listing summary to confirm or reject it.\n"
                "Extract:\n"
                "  'confirmed': true if the user accepted and wants to publish, false if they refused or want to change something.\n"
                "  'new_price': numeric value if the user explicitly asked to list at a different price, null otherwise.\n"
                "Clear confirmations: yes, confirm, list it, deal, ok, sure, go ahead, sim, confirmo, pode anunciar, fechou.\n"
                "Rejections: no, changed my mind, I want to change, cancel, wait, não, mudei de ideia.\n"
                "NEVER infer 'confirmed=true' if the message is ambiguous. When in doubt, confirmed=false."
            ),
            user_msg=text,
            validators={
                "confirmed":  self._val_bool,
                "new_price":  self._val_price,
            },
        )
        if ext.get("confirmed") is True:
            data["confirmed"]  = True
            data["step_next"]  = "done"
            return data, photos, "", True

        new_price = ext.get("new_price")
        if new_price:
            data["listed_price"] = new_price
            reply = await self._reply(
                f"The user wants to list at R$ {new_price:.2f}. "
                "Confirm the change and ask if they want to publish at this price."
            )
        else:
            reply = await self._reply(
                "The user did not confirm. Ask what they would like to adjust in the listing."
            )
        return data, photos, reply, False

    # ─────────────────────────────────────────────
    # Automatic processing (step: processing)
    # ─────────────────────────────────────────────

    async def processar(
        self,
        flow: dict,
        listing_repo=None,
        db=None,
    ) -> tuple[dict, str]:
        """
        Runs the full processing pipeline:
          1. Web search: market prices + product specs
          2. DB history: similar sold listings (by category)
          3. Visual analysis: GPT-4o Vision on submitted photos
          4. PricingAgent: combines all data to generate suggested price + minimum

        Returns (updated_data, confirmation_message).
        """
        from agents.pricing import PricingAgent
        from tools.builtin.web_search import WebSearchTool

        data   = _parse_jsonb(flow.get("data"), {})
        photos = _parse_jsonb(flow.get("photos"), [])

        description    = data.get("description", "")
        brand          = data.get("brand") or ""
        model          = data.get("model") or ""
        version        = data.get("version") or ""
        condition      = data.get("condition", "fair")
        usage_state    = data.get("usage_state", "used")
        has_receipt    = data.get("has_receipt", False)
        asking_price   = data.get("asking_price")
        seller_min     = data.get("seller_min_price")
        pickup_address = data.get("pickup_address", "")

        is_fixed_loc = data.get("product_type") == "fixed_location"
        product_name = " ".join(filter(None, [brand, model, version])) or description

        # 1. Web search — prices + specs (adapted for fixed-location vs movable)
        searcher = WebSearchTool()
        web_prices, web_specs = None, None
        try:
            if is_fixed_loc:
                location_addr = data.get("location_address", "")
                city_hint = _extract_city(location_addr) or ""
                web_prices = await searcher.execute(
                    f"preço venda {description} {city_hint} site:vivareal.com.br OR site:zapimoveis.com.br OR site:olx.com.br"
                )
            else:
                web_prices = await searcher.execute(
                    f"price {product_name} used site:olx.com.br OR site:mercadolivre.com.br"
                )
        except Exception as e:
            logger.warning(f"Price search failed: {e}")
        try:
            if is_fixed_loc:
                web_specs = await searcher.execute(
                    f"avaliação {description} características venda"
                )
            else:
                web_specs = await searcher.execute(f"{product_name} specifications technical sheet")
        except Exception as e:
            logger.warning(f"Specs search failed: {e}")

        data["web_info"] = {
            "prices": (web_prices or "")[:600],
            "specs":  (web_specs  or "")[:400],
        }

        # 2. Similar sales history from DB
        similar_history = []
        if is_fixed_loc:
            category = data.get("category") or "fixed_location"
        else:
            category = data.get("category") or _infer_category(product_name)
        data["category"] = category
        if db and listing_repo:
            try:
                rows = await listing_repo.find_similar_sold(category)
                similar_history = [dict(r) for r in rows]
            except Exception as e:
                logger.warning(f"DB history failed: {e}")

        # 3. Download images as base64 (once only)
        base64_images: list[str] = []
        if photos:
            from whatsapp import download_media_as_base64
            for photo in photos[:4]:
                data_uri = await download_media_as_base64(
                    photo.get("media_id", ""),
                    photo.get("mime_type", "image/jpeg"),
                )
                if data_uri:
                    base64_images.append(data_uri)
            logger.info(f"Downloaded {len(base64_images)}/{len(photos[:4])} photos for visual analysis")

        # 3b. Visual analysis of photos — single ImageAnalysisAgent handles all vision
        from agents.vision import ImageAnalysisAgent
        vision_result = None
        if base64_images:
            vision_result = await ImageAnalysisAgent().analyze(
                images=base64_images,
                context={
                    "declared_product":   product_name,
                    "declared_condition": condition,
                    "purpose":            "listing",
                },
            )
            if vision_result.error:
                logger.warning(f"Visual analysis error: {vision_result.error}")
                vision_result = None
        data["vision_analysis"] = vision_result.to_dict() if vision_result else None

        # 3c. Fill empty fields with visually extracted data
        if vision_result:
            filled = []
            if not data.get("brand") and vision_result.visible_brand:
                data["brand"]        = vision_result.visible_brand
                data["brand_source"] = "vision"
                filled.append(f"brand='{data['brand']}'")
            if not data.get("model") and vision_result.visible_model:
                data["model"]        = vision_result.visible_model
                data["model_source"] = "vision"
                filled.append(f"model='{data['model']}'")
            if not data.get("version") and vision_result.visible_version:
                data["version"]        = vision_result.visible_version
                data["version_source"] = "vision"
                filled.append(f"version='{data['version']}'")
            if vision_result.visible_details:
                data["vision_technical_details"] = vision_result.visible_details
            if filled:
                logger.info(f"Fields filled via vision: {', '.join(filled)}")
            if not vision_result.condition_consistent:
                logger.warning(
                    f"Condition inconsistency: seller declared '{condition}' "
                    f"but vision detected: {vision_result.visual_description[:100]}"
                )
                data["_condition_inconsistent"] = True

        # Recalculate product_name with vision-enriched fields
        brand        = data.get("brand") or ""
        model        = data.get("model") or ""
        version      = data.get("version") or ""
        product_name = " ".join(filter(None, [brand, model, version])) or description

        visual_desc  = vision_result.visual_description if vision_result else ""
        condition_ok = vision_result.condition_consistent if vision_result else True
        condition_alert = (
            f"WARNING: visual analysis detects inconsistency with declared condition. "
            f"Visual description: {visual_desc}. "
            if not condition_ok else ""
        )

        # 4. Pricing with all data
        rich_description = (
            f"{product_name}. "
            f"State: {usage_state}. "
            f"Condition: {CONDITION_LABEL.get(condition, condition)}. "
            f"Receipt: {'yes' if has_receipt else 'no'}. "
            + (f"Visual analysis: {visual_desc}. " if visual_desc else "")
            + condition_alert
            + (f"Prices found on the web: {web_prices[:300]}." if web_prices else "")
        )
        pricing_agent = PricingAgent(db)
        appraisal = await pricing_agent.appraise(
            description=rich_description,
            category=category,
            seller_asking_price=asking_price,
            similar_history=similar_history,
            photos=base64_images or None,
        )
        data["appraisal"] = appraisal

        is_fixed = data.get("product_type") == "fixed_location"
        location_address = data.get("location_address", "")

        if is_fixed:
            data["seller_city"] = _extract_city(location_address)
        else:
            data["seller_city"] = _extract_city(pickup_address)

        price_agent   = appraisal.get("suggested_price", 0) or 0
        min_agent     = appraisal.get("min_suggested_price", 0) or 0
        justification = appraisal.get("justification", "")
        confidence    = appraisal.get("confidence", "low")

        listed_price = asking_price or price_agent
        floor_price  = seller_min or min_agent

        data["listed_price"] = listed_price
        data["floor_price"]  = floor_price
        # Fixed-location products never have condition inconsistency to review
        if is_fixed:
            data["step_next"] = "confirm"
        else:
            data["step_next"] = "review_condition" if data.get("_condition_inconsistent") else "confirm"

        price_alert = ""
        if asking_price and price_agent > 0:
            diff = abs(asking_price - price_agent) / price_agent
            if diff > 0.30:
                direction = "above" if asking_price > price_agent else "below"
                price_alert = (
                    f"Note: your price of R$ {asking_price:.2f} is "
                    f"{diff*100:.0f}% {direction} the market value of R$ {price_agent:.2f}. "
                )

        type_label = data.get("type_label", "product")
        lines = [f"{'Property/Business' if is_fixed else 'Product'}: {product_name or description}"]

        if not is_fixed:
            origin_vision = [
                c for c in ("brand", "model", "version")
                if data.get(f"{c}_source") == "vision"
            ]
            if origin_vision:
                lines.append(f"  (detected in photos: {', '.join(origin_vision)})")

        if vision_result and vision_result.visible_details:
            details = ", ".join(vision_result.visible_details[:4])
            lines.append(f"Details read from photos: {details}")

        if is_fixed:
            lines += [
                f"Type: {type_label}",
                f"Photos: {len(photos)} submitted",
                f"Location: {location_address or 'not provided'}",
            ]
        else:
            lines += [
                f"State: {usage_state} | Condition: {CONDITION_LABEL.get(condition, condition)}",
                f"Receipt: {'yes' if has_receipt else 'no'}",
                f"Photos: {len(photos)} submitted",
                f"Pickup: {pickup_address or 'not provided'}",
            ]

        if asking_price:
            lines.append(f"Your price: R$ {asking_price:.2f}")
        if seller_min:
            lines.append(f"Your minimum: R$ {seller_min:.2f} (confidential)")
        lines.append(f"NOTHA appraisal: R$ {price_agent:.2f} (confidence: {confidence})")
        lines.append(f"Reason: {justification}")
        if price_alert:
            lines.append(price_alert)
        lines.append(f"Will be listed at: R$ {listed_price:.2f}")

        summary = "\n".join(lines)

        if data.get("_condition_inconsistent"):
            declared_label = CONDITION_LABEL.get(condition, condition)
            visual_excerpt = visual_desc[:200] if visual_desc else "not available"
            msg = await self._reply(
                f"The photo analysis detected a possible inconsistency in the declared condition.\n"
                f"Declared condition: {declared_label}.\n"
                f"What was observed in the photos: {visual_excerpt}\n\n"
                "Ask the seller to confirm whether the condition is correct or correct it to one of the options:\n"
                "1. Like new (no signs of use)\n"
                "2. Good condition (light use, few marks)\n"
                "3. Fair condition (normal use, minor wear)\n"
                "4. Worn (heavy use, visible marks)\n"
                "5. Defective (describe the defect)\n"
                "Or say 'yes' to confirm the already declared condition."
            )
            return data, msg

        msg = await self._reply(
            f"Present the listing summary and ask if they confirm:\n\n{summary}"
        )
        return data, msg



# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _infer_category(name: str) -> str:
    """
    Fast category inference from product name.
    For fixed-location products (real_estate, business), the category is set
    directly from the product_type classification — this function is only
    called for movable products and uses a short English keyword list as a
    lightweight heuristic (not a translation list — just common brand/model names).
    """
    n = name.lower()
    categories = {
        "electronics": [
            "iphone", "samsung", "celular", "smartphone", "notebook", "computador",
            "tablet", "ipad", "tv", "monitor", "fone", "headphone", "console",
            "playstation", "xbox", "nintendo", "câmera", "camera",
        ],
        "appliances": [
            "geladeira", "fogão", "micro-ondas", "lavadora", "máquina de lavar",
            "ar condicionado", "ventilador", "liquidificador", "batedeira", "churrasqueira",
        ],
        "furniture": [
            "sofá", "sofa", "mesa", "cadeira", "cama", "guarda-roupa", "armário",
            "estante", "escrivaninha", "rack",
        ],
        "clothing": [
            "camisa", "camiseta", "calça", "vestido", "sapato", "tênis", "sandália",
            "casaco", "jaqueta", "bolsa", "mochila",
        ],
        "vehicles": ["carro", "moto", "bicicleta", "patinete", "scooter"],
        "toys": ["brinquedo", "boneca", "lego", "jogo de tabuleiro"],
        "sports": ["esteira", "haltere", "peso", "raquete", "bola", "bike"],
        "books": ["livro", "revista", "manual", "apostila"],
    }
    for cat, keywords in categories.items():
        if any(k in n for k in keywords):
            return cat
    return "other"


def _extract_city(address: str) -> str | None:
    if not address:
        return None
    parts = [p.strip().rstrip(",") for p in address.split() if p.strip()]
    for i, part in enumerate(parts):
        if len(part) == 2 and part.isupper() and i > 0:
            return parts[i - 1]
    if len(parts) >= 2:
        return parts[-2]
    return None
