"""
TurnStateService — business-logic wrapper over TurnStateRepository.

The Orchestrator calls this before every message to know whether a specific
field was asked in the previous turn, and after a data save to clear it.

This is the piece described in section 4 of the architecture document:
"There is something specific we asked in the previous message, still unanswered."

Circuit breaker (doc section 12):
  MAX_ATTEMPTS controls how many times the system will ask for the same pending
  field before giving up. Once is_exhausted() returns True, the orchestrator
  clears the pending state and does not re-set it for that field — breaking the
  loop and letting the conversation move forward naturally.
"""
import logging
from db.connection import DB
from db.repositories.turn_state import TurnStateRepository

logger = logging.getLogger("notha.engine.turn_state")

MAX_ATTEMPTS = 3

# Maps tool_name → pending_field it resolves
_TOOL_RESOLVES_FIELD: dict[str, str] = {
    "update_name":         "full_name",
    "update_nickname":     "nickname",
    "update_tax_id":       "tax_id",
    "update_pix_key":      "pix_key",
    "update_address":      "pickup_address",
    "update_location":     "city",
    "update_full_address": "full_address",
    "update_profile":      "profile",
}

_FIELD_LABELS: dict[str, str] = {
    "full_name":      "nome completo",
    "nickname":       "apelido",
    "tax_id":         "CPF",
    "pix_key":        "chave Pix",
    "pickup_address": "endereço de retirada",
    "city":           "cidade/bairro",
    "full_address":   "endereço completo",
    "profile":        "dados de perfil",
}


class TurnStateService:
    def __init__(self, db: DB):
        self._repo = TurnStateRepository(db)

    async def get_pending(self, phone: str) -> dict | None:
        """Returns {pending_field, operation, context_data, asked_at, attempt_count} or None.

        Each call atomically increments attempt_count — this is how the circuit
        breaker tracks how many times the pending question has been presented.
        """
        return await self._repo.get(phone)

    async def set_pending(
        self,
        phone: str,
        pending_field: str,
        operation: str,
        context_data: dict | None = None,
    ) -> None:
        """Record that we just asked the user for a specific field.
        Resets attempt_count to 0.
        """
        await self._repo.set(phone, pending_field, operation, context_data)

    async def clear(self, phone: str) -> None:
        """Remove any pending turn state (e.g. on conversation reset)."""
        await self._repo.clear(phone)

    async def resolve_if_tool_matches(self, phone: str, tool_name: str) -> bool:
        """After a data-save tool executes, clear turn_state if the field matches.
        Returns True if something was cleared."""
        resolved_field = _TOOL_RESOLVES_FIELD.get(tool_name)
        if not resolved_field:
            return False
        return await self._repo.clear_if_field(phone, resolved_field)

    def is_exhausted(self, pending: dict) -> bool:
        """Returns True when a pending field has been attempted too many times.

        Checked BEFORE setting a new pending state — if exhausted, the orchestrator
        should not re-set the same field and instead let the conversation continue
        without a pending expectation (breaking the loop).
        """
        return (pending.get("attempt_count") or 0) >= MAX_ATTEMPTS

    def build_context_note(self, pending: dict) -> str:
        """Returns a context string injected into the LLM context when pending."""
        field = pending.get("pending_field", "")
        operation = pending.get("operation", "")
        label = _FIELD_LABELS.get(field, field)
        attempt = pending.get("attempt_count", 1)
        note = (
            f"PENDÊNCIA ATIVA: Na mensagem anterior NOTHA perguntou pelo(a) {label} "
            f"(operação pendente: {operation}). "
            f"Avalie primeiro se a mensagem atual responde a isso antes de interpretar livremente."
        )
        if attempt >= 2:
            note += f" [tentativa {attempt}/{MAX_ATTEMPTS}]"
        return note
