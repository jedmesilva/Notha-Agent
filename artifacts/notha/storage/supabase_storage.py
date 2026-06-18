import json
import os
from typing import Any
from storage.base import Storage


class SupabaseStorage(Storage):
    """
    Armazenamento persistente via Supabase.

    Variáveis de ambiente necessárias:
      - SUPABASE_URL       → URL do projeto Supabase (ex: https://xxx.supabase.co)
      - SUPABASE_KEY       → chave de serviço (service_role key)

    Schema esperado na tabela 'agent_store':
      CREATE TABLE agent_store (
        key   TEXT PRIMARY KEY,
        value JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW()
      );
    """

    TABLE = "agent_store"

    def __init__(self):
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")

        if not url or not key:
            raise RuntimeError(
                "Supabase não configurado. Defina SUPABASE_URL e SUPABASE_KEY."
            )

        try:
            from supabase import create_client
            self._client = create_client(url, key)
        except ImportError:
            raise RuntimeError(
                "Pacote 'supabase' não instalado. Execute: uv add supabase"
            )

    async def get(self, key: str) -> Any:
        response = (
            self._client.table(self.TABLE)
            .select("value")
            .eq("key", key)
            .single()
            .execute()
        )
        if response.data:
            return response.data["value"]
        return None

    async def set(self, key: str, value: Any) -> None:
        self._client.table(self.TABLE).upsert(
            {"key": key, "value": value}
        ).execute()

    async def delete(self, key: str) -> None:
        self._client.table(self.TABLE).delete().eq("key", key).execute()
