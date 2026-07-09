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
from tools.builtin.investment_tools import (
    ListarOportunidades,
    InvestirTool,
    ConsultarInvestimentos,
)
from tools.builtin.investor_profile_tools import (
    ConfigurarPerfilInvestidor,
    ConsultarPerfilInvestidor,
    ListarOfertasPendentes,
    ResponderOfertaInvestimento,
)
from tools.builtin.p2p_tools import (
    RequestLoanP2PTool,
    LaunchCaptureOrderTool,
    ViewCaptureStatusTool,
    PayP2PInstallmentTool,
    ViewUserInstrumentsTool,
    ViewOpenCaptureOrdersTool,
    CommitToCaptureOrderTool,
    ViewCreditorPositionsTool,
    PricePositionTool,
    ProposePositionSaleTool,
)

web_search        = WebSearchTool()
currency          = CurrencyTool()
math              = MathTool()
units             = UnitsTool()
datetime_tool     = DateTimeTool()
restriction_check = RestrictionCheckTool()

# Financial tools — borrower (legacy pool-based flow)
solicitar_emprestimo   = SolicitarEmprestimoTool()
consultar_extrato      = ConsultarExtrato()
consultar_dividas      = ConsultarDividas()
registrar_pagamento    = RegistrarPagamento()
consultar_limite       = ConsultarLimite()
calcular_cotacao_taxa  = CalcularCotacaoTaxa()
aprovar_emprestimo     = AprovarEmprestimoTool()

# Financial tools — creditor/investor (legacy pool-based flow)
listar_oportunidades         = ListarOportunidades()
investir                     = InvestirTool()
consultar_investimentos      = ConsultarInvestimentos()
configurar_perfil_investidor = ConfigurarPerfilInvestidor()
consultar_perfil_investidor  = ConsultarPerfilInvestidor()
listar_ofertas_pendentes     = ListarOfertasPendentes()
responder_oferta_investimento = ResponderOfertaInvestimento()

# P2P tools — borrower side (new SEP-compliant P2P flow)
request_loan_p2p       = RequestLoanP2PTool()
launch_capture_order   = LaunchCaptureOrderTool()
view_capture_status    = ViewCaptureStatusTool()
pay_p2p_installment    = PayP2PInstallmentTool()
view_user_instruments  = ViewUserInstrumentsTool()

# P2P tools — creditor/investor side
view_open_capture_orders  = ViewOpenCaptureOrdersTool()
commit_to_capture_order   = CommitToCaptureOrderTool()
view_creditor_positions   = ViewCreditorPositionsTool()
price_creditor_position   = PricePositionTool()
propose_position_sale     = ProposePositionSaleTool()

ALL_BUILTIN_TOOLS = [
    web_search, currency, math, units, datetime_tool, restriction_check,
    # borrower — legacy pool-based flow
    solicitar_emprestimo, consultar_extrato, consultar_dividas,
    registrar_pagamento, consultar_limite, calcular_cotacao_taxa,
    aprovar_emprestimo,
    # creditor/investor — legacy pool-based flow
    listar_oportunidades, investir, consultar_investimentos,
    configurar_perfil_investidor, consultar_perfil_investidor,
    listar_ofertas_pendentes, responder_oferta_investimento,
    # P2P — borrower side (SEP-compliant)
    request_loan_p2p, launch_capture_order, view_capture_status,
    pay_p2p_installment, view_user_instruments,
    # P2P — creditor/investor side
    view_open_capture_orders, commit_to_capture_order, view_creditor_positions,
    price_creditor_position, propose_position_sale,
]
