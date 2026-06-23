"""
Schema validator for structured agent output.

Architecture rule (doc section 12):
  All structured output from any Agent must be validated against a minimum schema
  before being used downstream. Malformed output is never silently "fixed" —
  it triggers a logged warning and applies a safe default (repair=True, default)
  or raises SchemaValidationError (repair=False, for strict callers).
"""
import logging

logger = logging.getLogger("notha.schema_validator")


class SchemaValidationError(Exception):
    """Raised when agent output fails schema validation and repair=False."""


_VALID_INTENTS = {
    "buy", "sell", "negotiate", "confirm", "reject", "counteroffer",
    "chitchat", "info", "onboarding", "decline", "out_of_scope", "other",
    "data_update",
}

_VALID_ASSESS_DECISIONS = {"continue", "done", "replan", "abort"}
_VALID_PROXY_DECISIONS  = {"accept", "counter", "reject"}
_VALID_PENDING_RESOLUTIONS = {"yes", "no", "ambiguous"}


def validate_understand(data: dict, *, repair: bool = True) -> dict:
    """Validates understand() output schema.

    Required keys: objective, intent, needs_tools.
    Optional key: pending_resolution — must be 'yes' | 'no' | 'ambiguous' if present.

    Args:
        data:   Raw dict from LLM JSON parse.
        repair: When True (default), replaces invalid values with safe defaults and
                logs a warning for every repair. When False, raises on first violation.

    Returns:
        Validated (and possibly repaired) dict — same object, mutated in place.
    """
    if not isinstance(data, dict):
        if repair:
            logger.warning("validate_understand: non-dict input %r — returning empty fallback", type(data))
            return {"objective": "", "intent": "other", "needs_tools": True}
        raise SchemaValidationError(f"understand() must return dict, got {type(data)}")

    for key, default in (("objective", ""), ("intent", "other"), ("needs_tools", True)):
        if key not in data:
            if repair:
                logger.warning("validate_understand: missing key %r — using default %r", key, default)
                data[key] = default
            else:
                raise SchemaValidationError(f"understand() missing required key: {key!r}")

    intent = data.get("intent", "")
    if intent not in _VALID_INTENTS:
        if repair:
            logger.warning("validate_understand: invalid intent=%r — replacing with 'other'", intent)
            data["intent"] = "other"
        else:
            raise SchemaValidationError(f"understand() invalid intent: {intent!r}")

    pr = data.get("pending_resolution")
    if pr is not None and pr not in _VALID_PENDING_RESOLUTIONS:
        if repair:
            logger.warning(
                "validate_understand: invalid pending_resolution=%r — replacing with 'no'", pr
            )
            data["pending_resolution"] = "no"
        else:
            raise SchemaValidationError(f"understand() invalid pending_resolution: {pr!r}")

    if not isinstance(data.get("needs_tools"), bool):
        if repair:
            data["needs_tools"] = bool(data.get("needs_tools", True))
        else:
            raise SchemaValidationError("understand() needs_tools must be bool")

    return data


def validate_assess(data: dict, *, repair: bool = True) -> dict:
    """Validates assess_result() output schema.

    Required key: decision — must be 'continue' | 'done' | 'replan' | 'abort'.
    """
    if not isinstance(data, dict):
        if repair:
            logger.warning("validate_assess: non-dict input — returning safe default")
            return {"decision": "continue", "reason": "", "progress_message": None, "new_steps": []}
        raise SchemaValidationError(f"assess_result() must return dict, got {type(data)}")

    decision = data.get("decision")
    if decision not in _VALID_ASSESS_DECISIONS:
        if repair:
            logger.warning(
                "validate_assess: invalid decision=%r — replacing with 'continue'", decision
            )
            data["decision"] = "continue"
        else:
            raise SchemaValidationError(f"assess_result() invalid decision: {decision!r}")

    return data


def validate_proxy(data: dict, *, repair: bool = True) -> dict:
    """Validates Buyer/Seller Proxy and Contextual Evaluator output schema.

    Required key: decision — must be 'accept' | 'counter' | 'reject'.
    """
    if not isinstance(data, dict):
        if repair:
            logger.warning("validate_proxy: non-dict input — returning safe default")
            return {"decision": "reject", "reason": "malformed proxy output"}
        raise SchemaValidationError(f"proxy output must be dict, got {type(data)}")

    decision = data.get("decision")
    if decision not in _VALID_PROXY_DECISIONS:
        if repair:
            logger.warning(
                "validate_proxy: invalid decision=%r — replacing with 'reject'", decision
            )
            data["decision"] = "reject"
        else:
            raise SchemaValidationError(f"proxy invalid decision: {decision!r}")

    return data
