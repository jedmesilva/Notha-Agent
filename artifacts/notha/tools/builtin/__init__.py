from tools.builtin.web_search import WebSearchTool
from tools.builtin.currency_tool import CurrencyTool
from tools.builtin.math_tool import MathTool
from tools.builtin.units_tool import UnitsTool
from tools.builtin.datetime_tool import DateTimeTool
from tools.builtin.restrictions_tool import RestrictionCheckTool

web_search = WebSearchTool()
currency = CurrencyTool()
math = MathTool()
units = UnitsTool()
datetime_tool = DateTimeTool()
restriction_check = RestrictionCheckTool()

ALL_BUILTIN_TOOLS = [web_search, currency, math, units, datetime_tool, restriction_check]
