"""
ConversationRepository — persistência do histórico de mensagens no banco.

Cada mensagem tem: user_id, role (user|assistant|system), content, created_at.
O histórico é carregado do banco e mantido como cache em memória durante a sessão.
"""
import asyncpg
from db.connection import DB

MAX_MESSAGES_DB = 100   # máximo armazenado no banco por usuário
MAX_MESSAGES_LLM = 20   # máximo passado ao LLM por chamada


class ConversationRepository:
    def __init__(self, db: DB):
        self._db = db

    async def add(self, user_id: int, role: str, content: str) -> None:
        """Persiste uma mensagem no banco e descarta as mais antigas se ultrapassar MAX_MESSAGES_DB."""
        await self._db.execute(
            """
            INSERT INTO conversation_messages (user_id, role, content)
            VALUES ($1, $2, $3)
            """,
            user_id,
            role,
            content,
        )
        # Mantém apenas as últimas MAX_MESSAGES_DB mensagens por usuário
        await self._db.execute(
            """
            DELETE FROM conversation_messages
            WHERE user_id = $1
              AND id NOT IN (
                  SELECT id FROM conversation_messages
                  WHERE user_id = $1
                  ORDER BY created_at DESC
                  LIMIT $2
              )
            """,
            user_id,
            MAX_MESSAGES_DB,
        )

    async def get_history(self, user_id: int, limit: int = MAX_MESSAGES_LLM) -> list[dict]:
        """Retorna as últimas `limit` mensagens em ordem cronológica (mais antigas primeiro).

        Formato compatível com a API OpenAI: [{"role": ..., "content": ...}]
        """
        rows = await self._db.fetch_all(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at
                FROM conversation_messages
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            ) sub
            ORDER BY created_at ASC
            """,
            user_id,
            limit,
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def clear(self, user_id: int) -> None:
        """Apaga todo o histórico de um usuário (ex: /reset)."""
        await self._db.execute(
            "DELETE FROM conversation_messages WHERE user_id = $1",
            user_id,
        )

    async def count(self, user_id: int) -> int:
        """Retorna o número total de mensagens armazenadas para o usuário."""
        return await self._db.fetch_val(
            "SELECT COUNT(*) FROM conversation_messages WHERE user_id = $1",
            user_id,
        ) or 0
