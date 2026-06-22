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
    name = "convert_units"
    description = (
        "Converts values between units of measurement. "
        "Supports: weight (kg, lb, oz, g), length (km, m, mi, ft, cm), "
        "volume (l, ml, gal), temperature (°C, °F, K), "
        "energy (J, cal, kWh), pressure (Pa, bar, psi, atm), "
        "power (W, kW, HP), speed (km/h, mph, m/s, knot) and more."
    )
    parameters = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "Numeric value to convert.",
            },
            "from_unit": {
                "type": "string",
                "description": "Source unit. E.g.: 'kg', 'mile', 'fahrenheit', 'gallon'.",
            },
            "to_unit": {
                "type": "string",
                "description": "Target unit. E.g.: 'lb', 'kilometer', 'celsius', 'liter'.",
            },
        },
        "required": ["value", "from_unit", "to_unit"],
    }

    async def execute(self, value: float, from_unit: str, to_unit: str) -> str:
        if not _PINT_AVAILABLE:
            return "Unit conversion library is not available at this time."

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
                f"Cannot convert '{from_unit}' to '{to_unit}': "
                f"the units are of different types (e.g. weight ≠ length)."
            )
        except Exception as e:
            return f"Error converting '{from_unit}' to '{to_unit}': {e}"
