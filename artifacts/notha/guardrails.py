"""
Guardrails de operação — define quais dados do perfil são obrigatórios para cada ação.
"""

# Requisitos por operação
OPERATION_REQUIREMENTS: dict[str, dict] = {
    "view_profile": {
        "label": "ver o perfil",
        "required": [],
        "preferred": [],
        "note": "Qualquer pessoa pode ver seu próprio perfil.",
    },
    "identity_verification": {
        "label": "verificar identidade",
        "required": ["full_name", "tax_id", "identity_submitted"],
        "preferred": [],
        "note": "Necessário para operações financeiras.",
    },
}

FIELD_LABELS: dict[str, str] = {
    "full_name":          "nome completo",
    "nickname":           "apelido",
    "tax_id":             "CPF",
    "identity_submitted": "documento de identidade enviado",
}

def check_requirements(operation: str, profile: dict) -> list[str]:
    reqs = OPERATION_REQUIREMENTS.get(operation, {})
    required = reqs.get("required", [])
    missing = []

    for field in required:
        if field == "identity_submitted":
            status = profile.get("identity_status", "unverified")
            if status == "unverified":
                missing.append(field)
        else:
            if not profile.get(field):
                missing.append(field)

    return missing

def missing_fields_message(operation: str, missing: list[str]) -> str:
    op_label = OPERATION_REQUIREMENTS.get(operation, {}).get("label", operation)
    field_names = [FIELD_LABELS.get(f, f) for f in missing]
    fields_str = ", ".join(field_names)
    return f"Para {op_label}, ainda preciso de: {fields_str}."
