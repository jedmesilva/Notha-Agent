"""
WalletRepository — wallets e wallet_transactions.

Responsabilidades:
  - Criar/buscar wallets (polimórfico: user | group | platform)
  - Registrar transações e manter balance_cache sincronizado
  - Reconciliar saldo real via SUM das transações
"""
import asyncpg
from decimal import Decimal
from db.connection import DB


class WalletRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Wallets ───────────────────────────────────────────────────────────────

    async def get_or_create(self, owner_type: str, owner_id: int) -> asyncpg.Record:
        """Retorna a wallet existente ou cria uma nova."""
        row = await self._db.fetch_one(
            "SELECT * FROM wallets WHERE owner_type = $1 AND owner_id = $2",
            owner_type, owner_id,
        )
        if row:
            return row
        await self._db.execute(
            "INSERT INTO wallets (owner_type, owner_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            owner_type, owner_id,
        )
        return await self._db.fetch_one(
            "SELECT * FROM wallets WHERE owner_type = $1 AND owner_id = $2",
            owner_type, owner_id,
        )

    async def get_by_id(self, wallet_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM wallets WHERE id = $1", wallet_id)

    async def get_by_owner(self, owner_type: str, owner_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM wallets WHERE owner_type = $1 AND owner_id = $2",
            owner_type, owner_id,
        )

    async def true_balance(self, wallet_id: int) -> Decimal:
        """Saldo real: soma de todas as transações (fonte de verdade)."""
        val = await self._db.fetch_val(
            "SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE wallet_id = $1",
            wallet_id,
        )
        return Decimal(str(val or 0))

    # ── Transações ────────────────────────────────────────────────────────────

    async def add_transaction(
        self,
        wallet_id: int,
        amount: Decimal,
        tx_type: str,
        reference_id: str | None = None,
        reference_type: str | None = None,
        description: str | None = None,
    ) -> int:
        """
        Registra uma transação e atualiza balance_cache atomicamente.
        amount > 0 = crédito, amount < 0 = débito.
        Retorna o id da transação gerada.
        """
        tx_id = await self._db.fetch_val(
            """
            INSERT INTO wallet_transactions
                (wallet_id, amount, type, reference_id, reference_type, description)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            wallet_id, amount, tx_type,
            str(reference_id) if reference_id is not None else None,
            reference_type, description,
        )
        await self._db.execute(
            "UPDATE wallets SET balance_cache = balance_cache + $1 WHERE id = $2",
            amount, wallet_id,
        )
        return tx_id

    async def get_transactions(
        self, wallet_id: int, limit: int = 20, offset: int = 0
    ) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM wallet_transactions
            WHERE wallet_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            wallet_id, limit, offset,
        )

    async def reconcile_cache(self, wallet_id: int) -> Decimal:
        """Recalcula e atualiza balance_cache a partir da soma real das transações."""
        true_bal = await self.true_balance(wallet_id)
        await self._db.execute(
            "UPDATE wallets SET balance_cache = $1 WHERE id = $2",
            true_bal, wallet_id,
        )
        return true_bal
