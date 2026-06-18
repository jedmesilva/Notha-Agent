from typing import Any
from storage.base import Storage


class MemoryStorage(Storage):
    """
    Armazenamento em memória RAM.
    Usado em desenvolvimento — dados são perdidos ao reiniciar o servidor.
    Para persistência, configure STORAGE_PROVIDER=supabase.
    """

    def __init__(self):
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
