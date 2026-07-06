"""
Scoped tools — each agent receives only the tools relevant to its domain.

Architecture rule (doc section 13.4):
  Restricting an agent's tool set is a technical constraint, not just an
  organizational convention. If a tool is not in the schema sent to the LLM,
  the LLM structurally cannot call it — regardless of its reasoning.
  This is the same domain-separation logic from section 3, applied as a
  capability limit, not just an instruction.
"""
from tools.builtin import (
    web_search, currency, math, units, datetime_tool, restriction_check,
)

CONVERSATION_AGENT_TOOLS: list[dict] = [
    web_search.to_openai_schema(),
    currency.to_openai_schema(),
    math.to_openai_schema(),
    units.to_openai_schema(),
    datetime_tool.to_openai_schema(),
    restriction_check.to_openai_schema(),
]

AGENT_TOOLS_MAP: dict[str, list[dict]] = {
    "conversation_agent":     CONVERSATION_AGENT_TOOLS,
}

def tools_for(agent_name: str) -> list[dict]:
    """Returns the scoped tool list for the given agent name."""
    return AGENT_TOOLS_MAP.get(agent_name, [])
