from tools.builtin.web_search import WebSearchTool
from tools.builtin.currency_tool import CurrencyTool
from tools.builtin.math_tool import MathTool
from tools.builtin.units_tool import UnitsTool
from tools.builtin.datetime_tool import DateTimeTool

web_search = WebSearchTool()
currency = CurrencyTool()
math = MathTool()
units = UnitsTool()
datetime_tool = DateTimeTool()

ALL_BUILTIN_TOOLS = [web_search, currency, math, units, datetime_tool]
