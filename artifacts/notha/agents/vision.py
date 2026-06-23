"""
ImageAnalysisAgent — single source of truth for all image/photo analysis.

Any part of the system that needs visual information (listing flow, product
photo received in chat, identity documents, condition review) calls this
agent. It returns a single, comprehensive structured result with two
explicit sections:

  LITERAL  — only what is physically written/printed in the image.
  INFERRED — what the model believes based on appearance, even when not
             explicitly written. Always marked as inference, never stated
             as fact.

Usage:
    from agents.vision import ImageAnalysisAgent
    agent = ImageAnalysisAgent()
    result = await agent.analyze(
        images=["data:image/jpeg;base64,..."],
        context={"declared_product": "iPhone 13", "declared_condition": "good"},
    )
"""
import json
import logging
from dataclasses import dataclass, field
from llm import get_provider

logger = logging.getLogger("notha.agent.vision")


@dataclass
class VisionResult:
    """Structured result from ImageAnalysisAgent.analyze().

    Attributes
    ----------
    product_identified : str | None
        Best guess at the product name/type.  Even when nothing is written
        this field is filled using the model's visual inference.
    confidence : str
        Overall identification confidence: "high", "medium" or "low".

    literal
        Sub-dict of information LITERALLY READ from text/labels in the image.
        Keys: brand, model, version, visible_text (list[str]), details (list[str]).

    inferences
        Sub-dict of what the model THINKS based on appearance alone — clearly
        marked as inference, never stated as fact.
        Keys: probable_brand, probable_model, probable_category,
              reasoning (str explaining why).

    visual_state
        Physical condition as seen in the photos.
        Keys: description (str), condition_consistent (bool | None),
              photos_sufficient (bool).
    """

    product_identified: str | None = None
    confidence: str = "low"

    literal: dict = field(default_factory=lambda: {
        "brand":        None,
        "model":        None,
        "version":      None,
        "visible_text": [],
        "details":      [],
    })

    inferences: dict = field(default_factory=lambda: {
        "probable_brand":    None,
        "probable_model":    None,
        "probable_category": None,
        "reasoning":         "",
    })

    visual_state: dict = field(default_factory=lambda: {
        "description":         "",
        "condition_consistent": None,
        "photos_sufficient":   True,
    })

    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "product_identified": self.product_identified,
            "confidence":         self.confidence,
            "literal":            self.literal,
            "inferences":         self.inferences,
            "visual_state":       self.visual_state,
            "error":              self.error,
        }

    # ── Convenience accessors (backward-compat with old _analyze_photos keys) ──

    @property
    def visual_description(self) -> str:
        return self.visual_state.get("description", "")

    @property
    def condition_consistent(self) -> bool:
        v = self.visual_state.get("condition_consistent")
        return v if v is not None else True

    @property
    def visible_brand(self) -> str | None:
        return self.literal.get("brand") or self.inferences.get("probable_brand")

    @property
    def visible_model(self) -> str | None:
        return self.literal.get("model") or self.inferences.get("probable_model")

    @property
    def visible_version(self) -> str | None:
        return self.literal.get("version")

    @property
    def visible_details(self) -> list[str]:
        return self.literal.get("details", [])

    @property
    def photos_sufficient(self) -> bool:
        return self.visual_state.get("photos_sufficient", True)


_SYSTEM_PROMPT = (
    "You are a specialist visual analyst for a physical-product marketplace. "
    "Your job is to extract every piece of useful information from product photos. "
    "You MUST always return valid JSON matching the exact schema provided. "
    "Separate what you READ from text/labels in the image (literal) from what you "
    "INFER based on shape, design and appearance (inferences). "
    "Never confuse the two sections."
)

_USER_PROMPT_TEMPLATE = """
Analyse the photo(s) above and return ONLY valid JSON with this exact structure:

{{
  "product_identified": "<best name for the product — use inferences if nothing written>",
  "confidence": "high | medium | low",

  "literal": {{
    "brand":        "<brand name ONLY if literally printed on label/screen/sticker — else null>",
    "model":        "<model name ONLY if literally written — else null>",
    "version":      "<version/capacity ONLY if literally written — else null>",
    "visible_text": ["<every word/phrase you can read in the image>"],
    "details":      ["<technical details literally read, e.g. '256GB', 'iOS 17'>"]
  }},

  "inferences": {{
    "probable_brand":    "<brand you THINK it is based on appearance — null if uncertain>",
    "probable_model":    "<model you THINK it is based on design/shape — null if uncertain>",
    "probable_category": "<product category, e.g. electronics/furniture/clothing/vehicles/other>",
    "reasoning":         "<one sentence explaining WHY you believe the above inferences>"
  }},

  "visual_state": {{
    "description":          "<objective description of physical state: finish, scratches, stains, dents, wear>",
    "condition_consistent": {condition_check},
    "photos_sufficient":    <true if photos have enough quality and angles for reliable evaluation, else false>
  }}
}}

CONTEXT PROVIDED:
{context_block}

CRITICAL RULES:
- literal.brand/model/version → ONLY if text is physically written in the image. Otherwise null.
- inferences.* → your best visual guess even when nothing is written. Be specific when confident.
- product_identified → always fill this. Use literal brand+model if written, else use inferences.
- confidence: high = clear product visible + readable text; medium = recognisable but some uncertainty; low = unclear/blurry.
- Never suggest prices or authenticity claims.
- Never leave visible_text / details as null — use [] if nothing is readable.
"""


class ImageAnalysisAgent:
    """Central visual analysis agent. Call analyze() for any image in the system."""

    async def analyze(
        self,
        images: list[str],
        context: dict | None = None,
    ) -> VisionResult:
        """
        Analyse one or more product images.

        Parameters
        ----------
        images : list[str]
            List of data URIs (data:image/jpeg;base64,...) or HTTPS URLs.
            Up to 4 images; extras are silently dropped.
        context : dict, optional
            Additional context to help the model:
              - declared_product (str)     — what the seller said the product is
              - declared_condition (str)   — condition declared by the seller
                                            (like_new/good/fair/worn/defective)
              - purpose (str)              — "listing" | "chat" | "document"
                                            (default: "listing")

        Returns
        -------
        VisionResult
            Always returns a VisionResult, even on failure (error field set).
        """
        if not images:
            return VisionResult(error="no_images_provided")

        ctx = context or {}
        declared_product   = ctx.get("declared_product", "")
        declared_condition = ctx.get("declared_condition", "")

        # Build context block for the prompt
        ctx_lines = []
        if declared_product:
            ctx_lines.append(f"- Declared product: {declared_product}")
        if declared_condition:
            ctx_lines.append(f"- Condition declared by seller: {declared_condition}")
        ctx_lines.append(f"- Purpose: {ctx.get('purpose', 'listing')}")
        context_block = "\n".join(ctx_lines) if ctx_lines else "None"

        # condition_consistent field is only relevant when a declared condition exists
        condition_check = (
            "true or false — does the declared condition match what you see in the photos"
            if declared_condition
            else "null"
        )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            context_block=context_block,
            condition_check=condition_check,
        )

        # Build multimodal content — up to 4 images
        content: list = []
        for data_uri in images[:4]:
            content.append({
                "type": "image_url",
                "image_url": {"url": data_uri, "detail": "high"},
            })
        content.append({"type": "text", "text": user_prompt})

        try:
            resp = await get_provider().complete(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": content},
                ],
                model="gpt-4o",
                temperature=0.0,
                max_tokens=800,
                json_mode=True,
            )
            raw = json.loads(resp.text or "{}")
        except Exception as exc:
            logger.warning("ImageAnalysisAgent: LLM call failed: %s", exc)
            return VisionResult(error=str(exc))

        try:
            literal    = raw.get("literal", {}) or {}
            inferences = raw.get("inferences", {}) or {}
            vis_state  = raw.get("visual_state", {}) or {}

            cond_consistent = vis_state.get("condition_consistent")
            if isinstance(cond_consistent, str):
                cond_consistent = cond_consistent.lower() == "true"

            result = VisionResult(
                product_identified=raw.get("product_identified") or None,
                confidence=str(raw.get("confidence", "low")).lower(),
                literal={
                    "brand":        literal.get("brand") or None,
                    "model":        literal.get("model") or None,
                    "version":      literal.get("version") or None,
                    "visible_text": list(literal.get("visible_text") or []),
                    "details":      list(literal.get("details") or []),
                },
                inferences={
                    "probable_brand":    inferences.get("probable_brand") or None,
                    "probable_model":    inferences.get("probable_model") or None,
                    "probable_category": inferences.get("probable_category") or None,
                    "reasoning":         str(inferences.get("reasoning") or ""),
                },
                visual_state={
                    "description":          str(vis_state.get("description") or ""),
                    "condition_consistent": cond_consistent,
                    "photos_sufficient":    bool(vis_state.get("photos_sufficient", True)),
                },
            )
            logger.info(
                "ImageAnalysisAgent: product=%r confidence=%s literal_brand=%r inferred_brand=%r",
                result.product_identified,
                result.confidence,
                result.literal.get("brand"),
                result.inferences.get("probable_brand"),
            )
            return result

        except Exception as exc:
            logger.warning("ImageAnalysisAgent: failed to parse LLM response: %s", exc)
            return VisionResult(error=f"parse_error: {exc}")
