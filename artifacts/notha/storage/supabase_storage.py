import json
import os
from typing import Any
from storage.base import Storage


class SupabaseStorage(Storage):
    """
    Persistent storage via Supabase.

    Required environment variables:
      - SUPABASE_URL       → Supabase project URL (e.g. https://xxx.supabase.co)
      - SUPABASE_KEY       → service role key

    Expected schema in the 'agent_store' table:
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
                "Supabase not configured. Set SUPABASE_URL and SUPABASE_KEY."
            )

        try:
            from supabase import create_client
            self._client = create_client(url, key)
        except ImportError:
            raise RuntimeError(
                "Package 'supabase' not installed. Run: uv add supabase"
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
