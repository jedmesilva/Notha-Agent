import sympy
from sympy.parsing.sympy_parser import (
    parse_expr,
    standard_transformations,
    implicit_multiplication_application,
)
from tools.base import Tool

_TRANSFORMATIONS = standard_transformations + (implicit_multiplication_application,)

_SAFE_LOCALS = {
    name: getattr(sympy, name)
    for name in dir(sympy)
    if not name.startswith("_")
}


class MathTool(Tool):
    name = "calcular"
    description = (
        "Executa cálculos matemáticos com precisão garantida. "
        "Use SEMPRE que precisar de aritmética, álgebra, equações ou qualquer conta "
        "— nunca confie no seu próprio resultado sem verificar com essa tool. "
        "Suporta: operações básicas, potências, raízes, logaritmos, trigonometria, "
        "fatoração, simplificação de expressões e resolução de equações simples."
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "Expressão matemática a calcular ou verificar. Exemplos: "
                    "'1847 * 293', 'sqrt(144)', 'log(1000, 10)', "
                    "'solve(x**2 - 4, x)', 'simplify((x**2 - 1)/(x - 1))'"
                ),
            }
        },
        "required": ["expression"],
    }

    async def execute(self, expression: str) -> str:
        try:
            result = parse_expr(
                expression,
                local_dict=_SAFE_LOCALS,
                transformations=_TRANSFORMATIONS,
            )
            evaluated = sympy.simplify(result)
            numeric = sympy.N(evaluated, 15)

            if evaluated == numeric or not numeric.is_number:
                return f"{expression} = {evaluated}"

            return f"{expression} = {evaluated} ≈ {numeric}"

        except Exception as e:
            return f"Não foi possível calcular '{expression}': {e}"
