from tools.base import Tool

try:
    from pint import UnitRegistry, errors as pint_errors
    _ureg = UnitRegistry()
    _ureg.default_format = "~P"
    _PINT_AVAILABLE = True
except ImportError:
    _PINT_AVAILABLE = False

_UNIT_ALIASES: dict[str, str] = {
    "kg": "kilogram",
    "g": "gram",
    "mg": "milligram",
    "t": "metric_ton",
    "lb": "pound",
    "lbs": "pound",
    "oz": "ounce",
    "km": "kilometer",
    "m": "meter",
    "cm": "centimeter",
    "mm": "millimeter",
    "mi": "mile",
    "ft": "foot",
    "in": "inch",
    "yd": "yard",
    "l": "liter",
    "ml": "milliliter",
    "gal": "gallon",
    "fl oz": "fluid_ounce",
    "c": "degC",
    "f": "degF",
    "k": "kelvin",
    "celsius": "degC",
    "fahrenheit": "degF",
    "kelvin": "kelvin",
    "j": "joule",
    "kj": "kilojoule",
    "cal": "calorie",
    "kcal": "kilocalorie",
    "wh": "watt_hour",
    "kwh": "kilowatt_hour",
    "pa": "pascal",
    "kpa": "kilopascal",
    "mpa": "megapascal",
    "bar": "bar",
    "atm": "atmosphere",
    "psi": "psi",
    "w": "watt",
    "kw": "kilowatt",
    "mw": "megawatt",
    "hp": "horsepower",
    "mph": "mile_per_hour",
    "km/h": "kilometer_per_hour",
    "kmh": "kilometer_per_hour",
    "m/s": "meter_per_second",
    "knot": "knot",
}


def _resolve_unit(unit_str: str) -> str:
    return _UNIT_ALIASES.get(unit_str.strip().lower(), unit_str.strip())


class UnitsTool(Tool):
    name = "converter_unidades"
    description = (
        "Converte valores entre unidades de medida. "
        "Suporta: peso (kg, lb, oz, g), comprimento (km, m, mi, ft, cm), "
        "volume (l, ml, gal), temperatura (°C, °F, K), "
        "energia (J, cal, kWh), pressão (Pa, bar, psi, atm), "
        "potência (W, kW, HP), velocidade (km/h, mph, m/s, knot) e mais."
    )
    parameters = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "Valor numérico a converter.",
            },
            "from_unit": {
                "type": "string",
                "description": "Unidade de origem. Ex: 'kg', 'mile', 'fahrenheit', 'gallon'.",
            },
            "to_unit": {
                "type": "string",
                "description": "Unidade de destino. Ex: 'lb', 'kilometer', 'celsius', 'liter'.",
            },
        },
        "required": ["value", "from_unit", "to_unit"],
    }

    async def execute(self, value: float, from_unit: str, to_unit: str) -> str:
        if not _PINT_AVAILABLE:
            return "Biblioteca de conversão de unidades não disponível no momento."

        try:
            from_resolved = _resolve_unit(from_unit)
            to_resolved = _resolve_unit(to_unit)

            quantity = _ureg.Quantity(value, from_resolved)
            converted = quantity.to(to_resolved)

            result = converted.magnitude
            if isinstance(result, float):
                if abs(result) >= 1000 or abs(result) < 0.001:
                    formatted = f"{result:.6g}"
                else:
                    formatted = f"{result:.4g}"
            else:
                formatted = str(result)

            return f"{value} {from_unit} = {formatted} {to_unit}"

        except pint_errors.DimensionalityError:
            return (
                f"Não é possível converter '{from_unit}' para '{to_unit}': "
                f"as unidades são de tipos diferentes (ex: peso ≠ comprimento)."
            )
        except Exception as e:
            return f"Erro na conversão de '{from_unit}' para '{to_unit}': {e}"
