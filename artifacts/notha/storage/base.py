from abc import ABC, abstractmethod
from typing import Any


class Storage(ABC):
    """Interface base para armazenamento de dados do agente."""

    @abstractmethod
    async def get(self, key: str) -> Any:
        """Retorna o valor armazenado ou None se não existir."""
        ...

    @abstractmethod
    async def set(self, key: str, value: Any) -> None:
        """Armazena um valor para a chave dada."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a chave do armazenamento."""
        ...
