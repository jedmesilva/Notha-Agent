"""
Phone number parsing using Google's libphonenumber (phonenumbers package).

Replaces the hand-coded DDI/DDD dictionary in phone_timezone.py with a proper
library that identifies country, state/province, carrier, timezone, and number
type for any phone number in E.164 format.

WhatsApp delivers phone numbers as raw digit strings without the leading '+'.
All functions here accept that format.
"""
import logging
from dataclasses import dataclass

import phonenumbers
from phonenumbers import (
    geocoder,
    carrier as carrier_module,
    timezone as tz_module,
    PhoneNumberType,
)

logger = logging.getLogger("notha.phone_info")

# ── City → timezone override ──────────────────────────────────────────────────
# Used when the user has a registered city that may differ from the timezone
# implied by their phone number's area code (e.g. someone from Manaus holding
# a São Paulo SIM). Kept small — only cities with non-obvious timezone mappings.
_CITY_TZ_OVERRIDE: dict[str, str] = {
    # Brazil: non-UTC-3 cities (UTC-3 is the default for most of Brazil)
    "manaus":        "America/Manaus",
    "porto velho":   "America/Porto_Velho",
    "cuiabá":        "America/Cuiaba",
    "cuiaba":        "America/Cuiaba",
    "campo grande":  "America/Campo_Grande",
    "rio branco":    "America/Rio_Branco",
    "boa vista":     "America/Boa_Vista",
    "macapá":        "America/Belem",
    "macapa":        "America/Belem",
    "belém":         "America/Belem",
    "belem":         "America/Belem",
    "palmas":        "America/Araguaina",
    "fortaleza":     "America/Fortaleza",
    "natal":         "America/Fortaleza",
    "teresina":      "America/Fortaleza",
    "são luís":      "America/Fortaleza",
    "sao luis":      "America/Fortalaze",
    "recife":        "America/Recife",
    "maceió":        "America/Maceio",
    "maceio":        "America/Maceio",
    "salvador":      "America/Bahia",
    # Portugal archipelagos
    "funchal":       "Atlantic/Madeira",
    "ponta delgada": "Atlantic/Azores",
}


@dataclass
class PhoneInfo:
    """All data extracted from a phone number via phonenumbers library."""
    phone:        str            # original E.164 digits (no +)
    country_code: int   = 0     # international dialing prefix, e.g. 55
    country_iso:  str   = ""    # ISO 3166-1 alpha-2, e.g. "BR"
    country_name: str   = ""    # full name in Portuguese, e.g. "Brasil"
    region:       str   = ""    # state/province/city when available, e.g. "São Paulo"
    carrier:      str   = ""    # mobile operator, e.g. "Vivo"
    timezone:     str   = "UTC" # primary IANA timezone, e.g. "America/Sao_Paulo"
    number_type:  str   = ""    # "MOBILE", "FIXED_LINE", "FIXED_LINE_OR_MOBILE", etc.
    is_valid:     bool  = False


def parse_phone(phone: str) -> PhoneInfo:
    """Parse a WhatsApp phone number and extract all available information.

    Args:
        phone: raw digit string as delivered by WhatsApp (e.g. "5511999999999")

    Returns:
        PhoneInfo dataclass — fields are empty/False if parsing fails.
    """
    info = PhoneInfo(phone=phone)

    raw = phone.strip()
    if not raw:
        return info

    e164 = raw if raw.startswith("+") else f"+{raw}"

    try:
        parsed = phonenumbers.parse(e164)
    except phonenumbers.NumberParseException as e:
        logger.warning("Could not parse phone '%s': %s", phone, e)
        return info

    try:
        info.is_valid    = phonenumbers.is_valid_number(parsed)
        info.country_code = parsed.country_code
        info.country_iso  = phonenumbers.region_code_for_number(parsed) or ""
        info.country_name = geocoder.country_name_for_number(parsed, "pt") or ""
        info.region       = geocoder.description_for_number(parsed, "pt") or ""
        info.carrier      = carrier_module.name_for_number(parsed, "pt") or ""

        tzs          = list(tz_module.time_zones_for_number(parsed))
        info.timezone = tzs[0] if tzs else "UTC"

        ntype         = phonenumbers.number_type(parsed)
        info.number_type = PhoneNumberType.to_string(ntype)

    except Exception as e:
        logger.error("Error extracting phone details for '%s': %s", phone, e)

    return info


def get_timezone(phone: str, city: str | None = None) -> str:
    """Get the most accurate IANA timezone for a user.

    Priority:
      1. Registered city override (catches users whose SIM is from a different
         region than where they actually live)
      2. phonenumbers library result (country code + area code → timezone)
      3. UTC as last resort

    This is a drop-in replacement for phone_timezone.infer_timezone().
    """
    if city:
        tz = _CITY_TZ_OVERRIDE.get(city.lower().strip())
        if tz:
            return tz

    return parse_phone(phone).timezone or "UTC"
