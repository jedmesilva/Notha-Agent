import logging
from tools.base import Tool

logger = logging.getLogger("notha.currency")

_API_URL = "https://open.er-api.com/v6/latest"

_CURRENCY_NAMES: dict[str, str] = {
    "AED": "Dirham dos EAU",
    "ARS": "Peso argentino",
    "AUD": "Dólar australiano",
    "BGN": "Lev búlgaro",
    "BRL": "Real brasileiro",
    "CAD": "Dólar canadense",
    "CHF": "Franco suíço",
    "CLP": "Peso chileno",
    "CNY": "Yuan chinês",
    "COP": "Peso colombiano",
    "CZK": "Coroa checa",
    "DKK": "Coroa dinamarquesa",
    "EGP": "Libra egípcia",
    "EUR": "Euro",
    "GBP": "Libra esterlina",
    "HKD": "Dólar de Hong Kong",
    "HUF": "Florim húngaro",
    "IDR": "Rupia indonésia",
    "ILS": "Shekel israelense",
    "INR": "Rupia indiana",
    "JPY": "Iene japonês",
    "KRW": "Won sul-coreano",
    "MXN": "Peso mexicano",
    "MYR": "Ringgit malaio",
    "NOK": "Coroa norueguesa",
    "NZD": "Dólar neozelandês",
    "PEN": "Sol peruano",
    "PHP": "Peso filipino",
    "PLN": "Zloty polonês",
    "RON": "Leu romeno",
    "RUB": "Rublo russo",
    "SAR": "Riyal saudita",
    "SEK": "Coroa sueca",
    "SGD": "Dólar de Singapura",
    "THB": "Baht tailandês",
    "TRY": "Lira turca",
    "TWD": "Dólar taiwanês",
    "UAH": "Hryvnia ucraniana",
    "USD": "Dólar americano",
    "UYU": "Peso uruguaio",
    "ZAR": "Rand sul-africano",
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
    name = "converter_moeda"
    description = (
        "Converte valores entre moedas com câmbio atualizado (open.er-api.com, ~160 moedas). "
        "Use para perguntas como 'quanto é X dólares em reais hoje?' ou 'qual é a cotação do euro?'. "
        "Moedas suportadas: USD, BRL, EUR, GBP, JPY, CAD, AUD, CHF, CNY, ARS, MXN, CLP e muito mais."
    )
    parameters = {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "Valor a converter.",
            },
            "from_currency": {
                "type": "string",
                "description": "Moeda de origem (código ISO 4217). Ex: 'USD', 'EUR', 'BRL'.",
            },
            "to_currency": {
                "type": "string",
                "description": (
                    "Moeda(s) de destino. Ex: 'BRL', 'EUR'. "
                    "Para múltiplas, separe por vírgula: 'BRL,EUR,GBP'."
                ),
            },
        },
        "required": ["amount", "from_currency", "to_currency"],
    }

    async def execute(self, amount: float, from_currency: str, to_currency: str) -> str:
        try:
            import httpx
        except ImportError:
            return "Biblioteca httpx não disponível."

        from_code = from_currency.strip().upper()
        to_codes = [c.strip().upper() for c in to_currency.split(",") if c.strip()]

        url = f"{_API_URL}/{from_code}"

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return "Não foi possível obter a cotação: tempo limite excedido. Tente novamente."
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404, 422):
                return (
                    f"Moeda '{from_code}' não reconhecida. "
                    f"Use códigos ISO 4217 (ex: USD, BRL, EUR, GBP)."
                )
            return f"Erro ao consultar câmbio: {e}"
        except Exception as e:
            return f"Erro inesperado ao consultar câmbio: {e}"

        if data.get("result") != "success":
            return f"API retornou erro: {data.get('error-type', 'desconhecido')}."

        all_rates: dict = data.get("rates", {})
        updated = data.get("time_last_update_utc", "data desconhecida")

        invalid = [c for c in to_codes if c not in all_rates]
        if invalid:
            return (
                f"Moeda(s) não reconhecida(s): {', '.join(invalid)}. "
                f"Use códigos ISO 4217 válidos (ex: USD, BRL, EUR)."
            )

        lines = [f"Câmbio atualizado em: {updated}\n"]
        for code in to_codes:
            rate = all_rates[code]
            converted = amount * rate
            lines.append(
                f"  {amount:g} {from_code} ({_name(from_code)}) = "
                f"{_fmt(converted)} {code} ({_name(code)})"
            )

        return "\n".join(lines)
