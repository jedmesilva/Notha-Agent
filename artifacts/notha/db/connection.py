import asyncpg
import logging
from config import DATABASE_URL

logger = logging.getLogger("notha.db")

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL não configurada — operações de banco indisponíveis.")
        return
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    logger.info("Pool de conexões PostgreSQL inicializado.")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        logger.info("Pool de conexões PostgreSQL encerrado.")


def get_pool() -> asyncpg.Pool | None:
    return _pool


class DB:
    """Wrapper fino sobre asyncpg para facilitar fetch/execute."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def fetch_one(self, query: str, *args) -> asyncpg.Record | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch_val(self, query: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    def transaction(self):
        return self._pool.acquire()


def get_db() -> DB | None:
    pool = get_pool()
    if pool is None:
        return None
    return DB(pool)
