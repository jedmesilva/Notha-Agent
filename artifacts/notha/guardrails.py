"""
Guardrails de operação — define quais dados do perfil são obrigatórios para cada ação.

Usado pelo orquestrador e repositórios para verificar antes de executar ações.
"""

# Requisitos por operação
# Cada campo é verificado contra o perfil do usuário no banco.
OPERATION_REQUIREMENTS: dict[str, dict] = {
    "search_product": {
        "label": "pesquisar um produto",
        "required": [],
        "preferred": ["name_or_nickname"],
        "note": "Qualquer pessoa pode pesquisar — nenhum dado obrigatório.",
    },
    "save_alert": {
        "label": "salvar um alerta de produto",
        "required": ["name_or_nickname"],
        "preferred": [],
        "note": "Precisa de nome ou apelido para personalizar as notificações.",
    },
    "start_negotiation": {
        "label": "iniciar uma negociação",
        "required": ["full_name", "phone_valid"],
        "preferred": ["tax_id", "city"],
        "note": "Nome e telefone válido são obrigatórios para negociar.",
    },
    "buy_product": {
        "label": "comprar um produto",
        "required": ["full_name", "tax_id", "phone_valid", "city"],
        "preferred": ["street", "state", "zip_code"],
        "note": "Dados completos são necessários para emissão de nota e entrega.",
    },
    "list_product": {
        "label": "anunciar um produto para venda",
        "required": [
            "full_name", "tax_id", "identity_submitted",
            "phone_valid", "city", "pickup_address", "pix_key",
        ],
        "preferred": ["street", "state", "zip_code"],
        "note": (
            "Documento de identidade, endereço e chave Pix são obrigatórios "
            "para garantir a segurança das transações."
        ),
    },
    "receive_payment": {
        "label": "receber um pagamento",
        "required": ["pix_key"],
        "preferred": ["full_name", "tax_id"],
        "note": "Chave Pix obrigatória para transferência.",
    },
    "be_courier": {
        "label": "trabalhar como entregador",
        "required": [
            "full_name", "tax_id", "identity_submitted",
            "phone_valid", "pix_key",
        ],
        "preferred": ["city", "state"],
        "note": "Entregadores precisam de identificação completa e chave Pix.",
    },
}

# Rótulos legíveis para os campos (usados nas mensagens ao usuário)
FIELD_LABELS: dict[str, str] = {
    "full_name":          "nome completo",
    "nickname":           "apelido",
    "name_or_nickname":   "nome ou apelido",
    "tax_id":             "CPF",
    "phone_valid":        "telefone WhatsApp válido",
    "city":               "cidade",
    "neighborhood":       "bairro",
    "street":             "rua e número",
    "state":              "estado",
    "zip_code":           "CEP",
    "country":            "país",
    "gender":             "sexo",
    "date_of_birth":      "data de nascimento",
    "preferred_language": "idioma preferido",
    "pix_key":            "chave Pix",
    "pickup_address":     "endereço de retirada do produto",
    "identity_submitted": "documento de identidade enviado (RG, CNH ou passaporte)",
    "identity_verified":  "documento de identidade verificado",
}


def check_requirements(operation: str, profile: dict) -> list[str]:
    """Retorna lista de campos obrigatórios faltando para a operação.

    profile deve conter os campos relevantes do usuário:
      full_name, tax_id, city, street, state, zip_code,
      pix_key, pickup_address, identity_status, phone_valid, ...
    """
    reqs = OPERATION_REQUIREMENTS.get(operation, {})
    required = reqs.get("required", [])
    missing = []

    for field in required:
        if field == "name_or_nickname":
            if not profile.get("full_name") and not profile.get("nickname"):
                missing.append(field)
        elif field == "identity_submitted":
            status = profile.get("identity_status", "unverified")
            if status == "unverified":
                missing.append(field)
        elif field == "identity_verified":
            if profile.get("identity_status") != "verified":
                missing.append(field)
        elif field == "phone_valid":
            # O telefone WhatsApp sempre existe (é por onde vieram)
            # mas verificamos se o número foi parseado/validado
            if not profile.get("phone_valid", True):
                missing.append(field)
        else:
            if not profile.get(field):
                missing.append(field)

    return missing


def missing_fields_message(operation: str, missing: list[str]) -> str:
    """Gera texto de instrução para o agente solicitar os campos faltando."""
    op_label = OPERATION_REQUIREMENTS.get(operation, {}).get("label", operation)
    field_names = [FIELD_LABELS.get(f, f) for f in missing]
    fields_str = ", ".join(field_names)
    return (
        f"Para {op_label}, ainda preciso de: {fields_str}. "
        "Solicite naturalmente, um item por vez, sem listar tudo de uma vez."
    )
