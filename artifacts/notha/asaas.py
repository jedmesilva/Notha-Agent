"""
Asaas API client — financial integration layer.

Operations: create Pix charge, transfer to external Pix key, refund.
Every call is idempotent via idempotency_key.
Value retention is controlled by the NOTHA backend — does not use sub-accounts or native escrow.

Only the MAISOR CAPITAL account has KYC in Asaas.
Sellers, buyers and couriers receive via any bank's Pix key (zero onboarding required).
"""
import logging
import httpx
from config import ASAAS_API_KEY, ASAAS_BASE_URL

logger = logging.getLogger("notha.asaas")

TIMEOUT = 30.0


class AsaasError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


class AsaasClient:
    def __init__(self):
        self._base = ASAAS_BASE_URL.rstrip("/")
        self._key = ASAAS_API_KEY

    def _headers(self, idempotency_key: str | None = None) -> dict:
        h = {
            "access_token": self._key,
            "Content-Type": "application/json",
            "User-Agent": "NOTHA/1.0",
        }
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _is_configured(self) -> bool:
        return bool(self._key)

    async def create_charge(
        self,
        amount: float,
        description: str,
        payer_cpf: str | None = None,
        payer_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Creates a Pix charge for the buyer. Amount is held in the MAISOR CAPITAL account."""
        if not self._is_configured():
            logger.warning("Asaas not configured — simulating charge creation.")
            return {
                "id": f"sim_{idempotency_key or 'charge'}",
                "status": "PENDING",
                "invoiceUrl": "https://asaas.com/simulado",
                "pixQrCode": "simulado_qr_code",
                "value": amount,
            }

        payload = {
            "billingType": "PIX",
            "value": amount,
            "dueDate": _today_due_date(),
            "description": description,
        }
        if payer_cpf:
            payload["cpfCnpj"] = payer_cpf
        if payer_name:
            payload["name"] = payer_name

        return await self._post("/payments", payload, idempotency_key)

    async def get_charge(self, charge_id: str) -> dict:
        """Returns the current status of a charge."""
        if not self._is_configured():
            return {"id": charge_id, "status": "RECEIVED"}
        return await self._get(f"/payments/{charge_id}")

    async def transfer_to_pix(
        self,
        pix_key: str,
        amount: float,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """
        Transfers amount to an external Pix key.
        Payment release to seller/courier — occurs ONLY after mutual delivery confirmation.
        """
        if not self._is_configured():
            logger.warning(f"Asaas not configured — simulating transfer of R${amount:.2f} to {pix_key}.")
            return {
                "id": f"sim_transfer_{idempotency_key or 'tx'}",
                "status": "PENDING",
                "value": amount,
                "pixAddressKey": pix_key,
            }

        payload = {
            "value": amount,
            "operationType": "PIX",
            "pixAddressKey": pix_key,
            "description": description,
        }
        return await self._post("/transfers", payload, idempotency_key)

    async def refund(self, charge_id: str, idempotency_key: str | None = None) -> dict:
        """Refunds a paid charge back to the original payment source."""
        if not self._is_configured():
            logger.warning(f"Asaas not configured — simulating refund of {charge_id}.")
            return {"id": charge_id, "status": "REFUNDED"}
        return await self._post(f"/payments/{charge_id}/refund", {}, idempotency_key)

    async def get_pix_key(self, pix_key: str) -> dict | None:
        """Looks up the holder of a Pix key. Used to validate the key before saving."""
        if not self._is_configured():
            logger.warning("Asaas not configured — simulating Pix key lookup.")
            return {"nome": "Titular Simulado", "chave": pix_key}

        try:
            result = await self._get(f"/pix/addressKeys/{pix_key}")
            return result
        except AsaasError as e:
            if e.status_code == 404:
                return None
            raise

    async def _post(self, path: str, payload: dict, idempotency_key: str | None = None) -> dict:
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers(idempotency_key))
        return self._handle_response(resp)

    async def _get(self, path: str) -> dict:
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers())
        return self._handle_response(resp)

    def _handle_response(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        if resp.status_code >= 400:
            errors = body.get("errors", [])
            msg = errors[0].get("description", resp.text) if errors else resp.text
            logger.error(f"Asaas error {resp.status_code}: {msg}")
            raise AsaasError(msg, status_code=resp.status_code, body=body)

        return body


def _today_due_date() -> str:
    from datetime import date
    return date.today().isoformat()
