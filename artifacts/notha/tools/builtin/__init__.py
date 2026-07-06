from tools.builtin.web_search import WebSearchTool
from tools.builtin.currency_tool import CurrencyTool
from tools.builtin.math_tool import MathTool
from tools.builtin.units_tool import UnitsTool
from tools.builtin.datetime_tool import DateTimeTool
from tools.builtin.restrictions_tool import RestrictionCheckTool
from tools.builtin.lending_tools import (
    SolicitarEmprestimoTool,
    ConsultarExtrato,
    ConsultarDividas,
    RegistrarPagamento,
    ConsultarLimite,
    CalcularCotacaoTaxa,
    AprovarEmprestimoTool,
)

web_search        = WebSearchTool()
currency          = CurrencyTool()
math              = MathTool()
units             = UnitsTool()
datetime_tool     = DateTimeTool()
restriction_check = RestrictionCheckTool()

# Ferramentas financeiras
solicitar_emprestimo   = SolicitarEmprestimoTool()
consultar_extrato      = ConsultarExtrato()
consultar_dividas      = ConsultarDividas()
registrar_pagamento    = RegistrarPagamento()
consultar_limite       = ConsultarLimite()
calcular_cotacao_taxa  = CalcularCotacaoTaxa()
aprovar_emprestimo     = AprovarEmprestimoTool()

ALL_BUILTIN_TOOLS = [
    web_search, currency, math, units, datetime_tool, restriction_check,
    solicitar_emprestimo, consultar_extrato, consultar_dividas,
    registrar_pagamento, consultar_limite, calcular_cotacao_taxa,
    aprovar_emprestimo,
]
