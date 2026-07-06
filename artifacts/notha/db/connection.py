import asyncpg
import logging
from contextlib import asynccontextmanager
from config import DATABASE_URL

logger = logging.getLogger("notha.db")

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not configured — database operations unavailable.")
        return
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    logger.info("PostgreSQL connection pool initialized.")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        logger.info("PostgreSQL connection pool closed.")


def get_pool() -> asyncpg.Pool | None:
    return _pool


class _TransactionalDB:
    """
    DB-compatible wrapper que usa uma conexão fixa (dentro de uma transação).
    Nunca deve ser instanciado diretamente — use DB.atomic().
    """
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def fetch_one(self, query: str, *args) -> asyncpg.Record | None:
        return await self._conn.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args) -> list[asyncpg.Record]:
        return await self._conn.fetch(query, *args)

    async def execute(self, query: str, *args) -> str:
        return await self._conn.execute(query, *args)

    async def fetch_val(self, query: str, *args):
        return await self._conn.fetchval(query, *args)


class DB:
    """Thin wrapper over asyncpg to simplify fetch/execute calls."""

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

    @asynccontextmanager
    async def atomic(self):
        """
        Context manager para operações DB atômicas.

        Adquire uma única conexão do pool, abre uma transação e expõe um
        _TransactionalDB que roteia todos os métodos por essa mesma conexão.
        Ao sair sem exceção, a transação é confirmada; qualquer exceção faz
        rollback automático via asyncpg.

        Uso típico:
            async with db.atomic() as tx:
                wallet_repo = WalletRepository(tx)
                await wallet_repo.add_transaction(...)
                await inv_repo_using_tx.mark_payout_paid(payout_id)
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield _TransactionalDB(conn)

    def transaction(self):
        return self._pool.acquire()


def get_db() -> DB | None:
    pool = get_pool()
    if pool is None:
        return None
    return DB(pool)
