"""
Pluggy API client — Open Finance Brazil.

Responsabilidades:
- Autenticação (clientId + clientSecret → apiKey)
- Geração de connect token (sessão temporária para o usuário)
- Criação e consulta de Items (conexões bancárias)
- Listagem de contas e saldo
- Listagem de transações
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("notha.pluggy")

PLUGGY_BASE_URL = "https://api.pluggy.ai"


def _get_credentials() -> tuple[str, str]:
    client_id = os.environ.get("PLUGGY_CLIENT_ID", "")
    client_secret = os.environ.get("PLUGGY_CLIENT_SECRET", "")
    return client_id, client_secret


class PluggyClient:
    """Async client para a Pluggy API."""

    def __init__(self) -> None:
        self._api_key: str | None = None

    async def _authenticate(self) -> str:
        """Obtém (ou renova) a apiKey via clientId + clientSecret."""
        client_id, client_secret = _get_credentials()
        if not client_id or not client_secret:
            raise ValueError("PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET não configurados.")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PLUGGY_BASE_URL}/auth",
                json={"clientId": client_id, "clientSecret": client_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            api_key = data.get("apiKey")
            if not api_key:
                raise ValueError(f"Pluggy auth falhou: {data}")
            self._api_key = api_key
            logger.debug("Pluggy autenticado com sucesso.")
            return api_key

    async def _api_key_valid(self) -> str:
        """Retorna apiKey atual, autenticando se necessário."""
        if not self._api_key:
            await self._authenticate()
        return self._api_key  # type: ignore[return-value]

    async def _get(self, path: str, params: dict | None = None) -> Any:
        api_key = await self._api_key_valid()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{PLUGGY_BASE_URL}{path}",
                headers={"X-API-KEY": api_key},
                params=params or {},
            )
            if resp.status_code == 401:
                # Token expirado — renova e tenta mais uma vez
                await self._authenticate()
                resp = await client.get(
                    f"{PLUGGY_BASE_URL}{path}",
                    headers={"X-API-KEY": self._api_key},
                    params=params or {},
                )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: dict) -> Any:
        api_key = await self._api_key_valid()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PLUGGY_BASE_URL}{path}",
                headers={"X-API-KEY": api_key},
                json=body,
            )
            if resp.status_code == 401:
                await self._authenticate()
                resp = await client.post(
                    f"{PLUGGY_BASE_URL}{path}",
                    headers={"X-API-KEY": self._api_key},
                    json=body,
                )
            resp.raise_for_status()
            return resp.json()

    async def create_connect_token(
        self,
        client_user_id: str,
        item_id: str | None = None,
    ) -> str:
        """
        Gera um connect token vinculado a um usuário da plataforma.

        Args:
            client_user_id: ID do usuário no nosso sistema (ex: phone number).
            item_id: Se fornecido, o token reabre um item existente (atualização).

        Returns:
            access_token — string opaca a ser passada para o frontend/widget.
        """
        body: dict[str, Any] = {"clientUserId": client_user_id}
        if item_id:
            body["itemId"] = item_id

        data = await self._post("/connect_token", body)
        token = data.get("accessToken")
        if not token:
            raise ValueError(f"Connect token inválido: {data}")
        return token

    async def list_connectors(self, connector_type: str = "PERSONAL_BANK") -> list[dict]:
        """
        Lista os conectores disponíveis (bancos/instituições).

        Args:
            connector_type: "PERSONAL_BANK" | "BUSINESS_BANK" | "INVESTMENT" etc.

        Returns:
            Lista de dicts com id, name, imageUrl, etc.
        """
        data = await self._get("/connectors", {"type": connector_type})
        return data.get("results", [])

    async def get_connector(self, connector_id: int) -> dict:
        """Retorna detalhes de um conector específico."""
        return await self._get(f"/connectors/{connector_id}")

    async def create_item(
        self,
        connector_id: int,
        parameters: dict,
        client_user_id: str | None = None,
        webhook_url: str | None = None,
    ) -> dict:
        """
        Cria um novo Item (conexão bancária).

        Args:
            connector_id: ID do conector (banco).
            parameters: Credenciais necessárias pelo conector (cpf, senha, etc.).
            client_user_id: ID do usuário na nossa plataforma.
            webhook_url: URL para receber notificações de status.

        Returns:
            Dict com id, status, executionStatus, etc.
        """
        body: dict[str, Any] = {
            "connectorId": connector_id,
            "parameters": parameters,
        }
        if client_user_id:
            body["clientUserId"] = client_user_id
        if webhook_url:
            body["webhookUrl"] = webhook_url

        return await self._post("/items", body)

    async def get_item(self, item_id: str) -> dict:
        """Consulta o status de um Item."""
        return await self._get(f"/items/{item_id}")

    async def list_accounts(self, item_id: str) -> list[dict]:
        """
        Lista as contas bancárias de um Item.

        Returns:
            Lista de dicts com id, name, type, number, balance, currencyCode.
        """
        data = await self._get("/accounts", {"itemId": item_id})
        return data.get("results", [])

    async def get_account(self, account_id: str) -> dict:
        """Retorna detalhes de uma conta específica."""
        return await self._get(f"/accounts/{account_id}")

    async def list_transactions(
        self,
        account_id: str,
        from_date: str | None = None,
        to_date: str | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """
        Lista transações de uma conta.

        Args:
            account_id: ID da conta.
            from_date: Data inicial no formato "YYYY-MM-DD".
            to_date: Data final no formato "YYYY-MM-DD".
            page_size: Número de resultados por página (máx 500).

        Returns:
            Lista de dicts com date, description, amount, type, etc.
        """
        params: dict[str, Any] = {"accountId": account_id, "pageSize": page_size}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        data = await self._get("/transactions", params)
        return data.get("results", [])

    async def list_investments(self, item_id: str) -> list[dict]:
        """Lista investimentos de um Item."""
        data = await self._get("/investments", {"itemId": item_id})
        return data.get("results", [])

    async def get_identity(self, item_id: str) -> dict:
        """Retorna dados de identidade associados ao Item (nome, CPF, etc.)."""
        return await self._get(f"/identity?itemId={item_id}")


# Instância global (lazy — não conecta na startup)
_pluggy_client: PluggyClient | None = None


def get_pluggy_client() -> PluggyClient:
    global _pluggy_client
    if _pluggy_client is None:
        _pluggy_client = PluggyClient()
    return _pluggy_client
