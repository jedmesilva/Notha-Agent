"""
Asaas API Client — camada de integração financeira.

Operações: criar cobrança Pix, transferir para chave Pix externa, estornar.
Toda chamada é idempotente via idempotency_key.
Retenção do valor é controlada pelo backend NOTHA — não usa subcontas ou escrow nativo.

Apenas a conta da MAISOR CAPITAL tem KYC no Asaas.
Vendedor, comprador e entregador recebem via chave Pix de qualquer banco (zero onboarding).
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

    async def criar_cobranca(
        self,
        valor: float,
        descricao: str,
        cpf_pagador: str | None = None,
        nome_pagador: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Cria cobrança Pix para o comprador. Valor fica na conta da MAISOR CAPITAL."""
        if not self._is_configured():
            logger.warning("Asaas não configurado — simulando criação de cobrança.")
            return {
                "id": f"sim_{idempotency_key or 'charge'}",
                "status": "PENDING",
                "invoiceUrl": "https://asaas.com/simulado",
                "pixQrCode": "simulado_qr_code",
                "value": valor,
            }

        payload = {
            "billingType": "PIX",
            "value": valor,
            "dueDate": _due_date_hoje(),
            "description": descricao,
        }
        if cpf_pagador:
            payload["cpfCnpj"] = cpf_pagador
        if nome_pagador:
            payload["name"] = nome_pagador

        return await self._post("/payments", payload, idempotency_key)

    async def consultar_cobranca(self, cobranca_id: str) -> dict:
        """Retorna status atual de uma cobrança."""
        if not self._is_configured():
            return {"id": cobranca_id, "status": "RECEIVED"}
        return await self._get(f"/payments/{cobranca_id}")

    async def transferir_para_pix(
        self,
        chave_pix: str,
        valor: float,
        descricao: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """
        Transfere valor para chave Pix externa.
        Liberação de pagamento ao vendedor/entregador — ocorre SOMENTE após confirmação mútua de entrega.
        """
        if not self._is_configured():
            logger.warning(f"Asaas não configurado — simulando transferência de R${valor:.2f} para {chave_pix}.")
            return {
                "id": f"sim_transfer_{idempotency_key or 'tx'}",
                "status": "PENDING",
                "value": valor,
                "pixAddressKey": chave_pix,
            }

        payload = {
            "value": valor,
            "operationType": "PIX",
            "pixAddressKey": chave_pix,
            "description": descricao,
        }
        return await self._post("/transfers", payload, idempotency_key)

    async def estornar(self, cobranca_id: str, idempotency_key: str | None = None) -> dict:
        """Estorna uma cobrança paga de volta para a origem do pagamento."""
        if not self._is_configured():
            logger.warning(f"Asaas não configurado — simulando estorno de {cobranca_id}.")
            return {"id": cobranca_id, "status": "REFUNDED"}
        return await self._post(f"/payments/{cobranca_id}/refund", {}, idempotency_key)

    async def consultar_chave_pix(self, chave_pix: str) -> dict | None:
        """Consulta titular de uma chave Pix. Usado para validar chave antes de salvar."""
        if not self._is_configured():
            logger.warning("Asaas não configurado — simulando consulta de chave Pix.")
            return {"nome": "Titular Simulado", "chave": chave_pix}

        try:
            result = await self._get(f"/pix/addressKeys/{chave_pix}")
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


def _due_date_hoje() -> str:
    from datetime import date
    return date.today().isoformat()
