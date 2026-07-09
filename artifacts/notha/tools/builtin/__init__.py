from tools.builtin.web_search import WebSearchTool
from tools.builtin.currency_tool import CurrencyTool
from tools.builtin.math_tool import MathTool
from tools.builtin.units_tool import UnitsTool
from tools.builtin.datetime_tool import DateTimeTool
from tools.builtin.restrictions_tool import RestrictionCheckTool
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

# Investor profile tools (P2P — creditor side)
configurar_perfil_investidor  = ConfigurarPerfilInvestidor()
consultar_perfil_investidor   = ConsultarPerfilInvestidor()
listar_ofertas_pendentes      = ListarOfertasPendentes()
responder_oferta_investimento = ResponderOfertaInvestimento()

# P2P tools — borrower side (SEP-compliant)
request_loan_p2p      = RequestLoanP2PTool()
launch_capture_order  = LaunchCaptureOrderTool()
view_capture_status   = ViewCaptureStatusTool()
pay_p2p_installment   = PayP2PInstallmentTool()
view_user_instruments = ViewUserInstrumentsTool()

# P2P tools — creditor/investor side
view_open_capture_orders  = ViewOpenCaptureOrdersTool()
commit_to_capture_order   = CommitToCaptureOrderTool()
view_creditor_positions   = ViewCreditorPositionsTool()
price_creditor_position   = PricePositionTool()
propose_position_sale     = ProposePositionSaleTool()

ALL_BUILTIN_TOOLS = [
    web_search, currency, math, units, datetime_tool, restriction_check,
    # creditor/investor profile
    configurar_perfil_investidor, consultar_perfil_investidor,
    listar_ofertas_pendentes, responder_oferta_investimento,
    # P2P — borrower side (SEP-compliant)
    request_loan_p2p, launch_capture_order, view_capture_status,
    pay_p2p_installment, view_user_instruments,
    # P2P — creditor/investor side
    view_open_capture_orders, commit_to_capture_order, view_creditor_positions,
    price_creditor_position, propose_position_sale,
]
