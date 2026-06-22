from typing import Any
from storage.base import Storage


class MemoryStorage(Storage):
    """
    In-memory (RAM) storage.
    Used in development — data is lost on server restart.
    For persistence, configure STORAGE_PROVIDER=supabase.
    """

    def __init__(self):
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
