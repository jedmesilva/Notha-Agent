import logging
from tools.base import Tool

logger = logging.getLogger("notha.currency")

_API_URL = "https://open.er-api.com/v6/latest"

_CURRENCY_NAMES: dict[str, str] = {
    "AED": "UAE Dirham",
    "ARS": "Argentine Peso",
    "AUD": "Australian Dollar",
    "BGN": "Bulgarian Lev",
    "BRL": "Brazilian Real",
    "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc",
    "CLP": "Chilean Peso",
    "CNY": "Chinese Yuan",
    "COP": "Colombian Peso",
    "CZK": "Czech Koruna",
    "DKK": "Danish Krone",
    "EGP": "Egyptian Pound",
    "EUR": "Euro",
    "GBP": "British Pound",
    "HKD": "Hong Kong Dollar",
    "HUF": "Hungarian Forint",
    "IDR": "Indonesian Rupiah",
    "ILS": "Israeli Shekel",
    "INR": "Indian Rupee",
    "JPY": "Japanese Yen",
    "KRW": "South Korean Won",
    "MXN": "Mexican Peso",
    "MYR": "Malaysian Ringgit",
    "NOK": "Norwegian Krone",
    "NZD": "New Zealand Dollar",
    "PEN": "Peruvian Sol",
    "PHP": "Philippine Peso",
    "PLN": "Polish Zloty",
    "RON": "Romanian Leu",
    "RUB": "Russian Ruble",
    "SAR": "Saudi Riyal",
    "SEK": "Swedish Krona",
    "SGD": "Singapore Dollar",
    "THB": "Thai Baht",
    "TRY": "Turkish Lira",
    "TWD": "Taiwan Dollar",
    "UAH": "Ukrainian Hryvnia",
    "USD": "US Dollar",
    "UYU": "Uruguayan Peso",
    "ZAR": "South African Rand",
}


def _name(code: str) -> str:
    return _CURRENCY_NAMES.get(code.upper(), code.upper())


def _fmt(value: float) -> str:
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


class CurrencyTool(Tool):
    name = "convert_currency"
    description = (
        "Converts amounts between currencies using live exchange rates (open.er-api.com, ~160 currencies). "
        "Use for questions like 'how much is X dollars in reais today?' or 'what is the euro rate?'. "
        "Supported: USD, BRL, EUR, GBP, JPY, CAD, AUD, CHF, CNY, ARS, MXN, CLP and many more."
    )
    parameters = {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "Amount to convert.",
            },
            "from_currency": {
                "type": "string",
                "description": "Source currency (ISO 4217 code). E.g.: 'USD', 'EUR', 'BRL'.",
            },
            "to_currency": {
                "type": "string",
                "description": (
                    "Target currency or currencies. E.g.: 'BRL', 'EUR'. "
                    "For multiple, separate with comma: 'BRL,EUR,GBP'."
                ),
            },
        },
        "required": ["amount", "from_currency", "to_currency"],
    }

    async def execute(self, amount: float, from_currency: str, to_currency: str) -> str:
        try:
            import httpx
        except ImportError:
            return "httpx library not available."

        from_code = from_currency.strip().upper()
        to_codes = [c.strip().upper() for c in to_currency.split(",") if c.strip()]

        url = f"{_API_URL}/{from_code}"

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return "Could not retrieve exchange rate: request timed out. Please try again."
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404, 422):
                return (
                    f"Currency '{from_code}' not recognised. "
                    f"Use ISO 4217 codes (e.g. USD, BRL, EUR, GBP)."
                )
            return f"Exchange rate API error: {e}"
        except Exception as e:
            return f"Unexpected error fetching exchange rate: {e}"

        if data.get("result") != "success":
            return f"API returned error: {data.get('error-type', 'unknown')}."

        all_rates: dict = data.get("rates", {})
        updated = data.get("time_last_update_utc", "unknown date")

        invalid = [c for c in to_codes if c not in all_rates]
        if invalid:
            return (
                f"Unrecognised currency code(s): {', '.join(invalid)}. "
                f"Use valid ISO 4217 codes (e.g. USD, BRL, EUR)."
            )

        lines = [f"Exchange rates updated: {updated}\n"]
        for code in to_codes:
            rate = all_rates[code]
            converted = amount * rate
            lines.append(
                f"  {amount:g} {from_code} ({_name(from_code)}) = "
                f"{_fmt(converted)} {code} ({_name(code)})"
            )

        return "\n".join(lines)
