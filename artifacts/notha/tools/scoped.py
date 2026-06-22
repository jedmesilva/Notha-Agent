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

PRICING_AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_preco_mercado",
            "description": (
                "Busca o preço de mercado de um produto novo/similar na web. "
                "Use quando precisar de referência de teto de preço para calibrar sugestão."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "produto": {"type": "string", "description": "Descrição do produto"},
                },
                "required": ["produto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_historico_similar",
            "description": (
                "Busca preços finais de negociações fechadas no NOTHA para produtos da mesma categoria. "
                "Fonte mais confiável que preço externo — reflete comportamento real na plataforma."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {"type": "string"},
                    "atributos": {
                        "type": "object",
                        "description": "Marca, modelo, estado de uso, etc.",
                    },
                },
                "required": ["categoria"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "avaliar_imagens",
            "description": (
                "Avalia visualmente fotos do produto para estimar condição e impacto no preço. "
                "Use quando houver fotos disponíveis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "listing_id": {"type": "integer", "description": "ID do listing com fotos"},
                },
                "required": ["listing_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_interesse_anterior",
            "description": (
                "Consulta fila histórica de interesse para estimar demanda pelo produto. "
                "Maior demanda = maior potencial de preço."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria":  {"type": "string"},
                    "search_city": {"type": "string"},
                },
                "required": ["categoria"],
            },
        },
    },
]

LISTING_FLOW_AGENT_TOOLS: list[dict] = [
    restriction_check.to_openai_schema(),
]

COURIER_MATCHING_AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_entregadores_disponiveis",
            "description": "Busca entregadores disponíveis na região de origem do produto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cidade": {"type": "string"},
                    "estado": {"type": "string"},
                },
                "required": ["cidade"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calcular_distancia_entrega",
            "description": "Estima distância entre endereço de origem e destino da entrega.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origem":  {"type": "string"},
                    "destino": {"type": "string"},
                },
                "required": ["origem", "destino"],
            },
        },
    },
]

CONVERSATION_AGENT_TOOLS: list[dict] = [
    web_search.to_openai_schema(),
    currency.to_openai_schema(),
    math.to_openai_schema(),
    units.to_openai_schema(),
    datetime_tool.to_openai_schema(),
    restriction_check.to_openai_schema(),
]

AGENT_TOOLS_MAP: dict[str, list[dict]] = {
    "pricing_agent":          PRICING_AGENT_TOOLS,
    "listing_flow_agent":     LISTING_FLOW_AGENT_TOOLS,
    "courier_matching_agent": COURIER_MATCHING_AGENT_TOOLS,
    "conversation_agent":     CONVERSATION_AGENT_TOOLS,
}


def tools_for(agent_name: str) -> list[dict]:
    """Returns the scoped tool list for the given agent name."""
    return AGENT_TOOLS_MAP.get(agent_name, [])
