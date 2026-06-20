import json
import asyncpg
from db.connection import DB


class ListingRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        seller_id: int,
        descricao: str,
        categoria: str | None = None,
        fotos: list | None = None,
        preco_informado_vendedor: float | None = None,
        preco_sugerido: float | None = None,
        preco_anunciado: float = 0,
        preco_minimo: float = 0,
        appraisal_data: dict | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO listings
                (seller_id, descricao, categoria, fotos, preco_informado_vendedor,
                 preco_sugerido, preco_anunciado, preco_minimo, appraisal_data, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'disponivel')
            RETURNING *
            """,
            seller_id,
            descricao,
            categoria,
            json.dumps(fotos or []),
            preco_informado_vendedor,
            preco_sugerido,
            preco_anunciado,
            preco_minimo,
            json.dumps(appraisal_data) if appraisal_data else None,
        )

    async def find_by_id(self, listing_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM listings WHERE id = $1", listing_id)

    async def find_available(self, categoria: str | None = None, limit: int = 20) -> list[asyncpg.Record]:
        if categoria:
            return await self._db.fetch_all(
                "SELECT * FROM listings WHERE status = 'disponivel' AND categoria ILIKE $1 ORDER BY created_at DESC LIMIT $2",
                f"%{categoria}%",
                limit,
            )
        return await self._db.fetch_all(
            "SELECT * FROM listings WHERE status = 'disponivel' ORDER BY created_at DESC LIMIT $1",
            limit,
        )

    async def find_by_seller(self, seller_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM listings WHERE seller_id = $1 ORDER BY created_at DESC",
            seller_id,
        )

    async def set_status(self, listing_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE listings SET status = $1, updated_at = now() WHERE id = $2",
            status,
            listing_id,
        )

    async def confirm_price(
        self,
        listing_id: int,
        preco_anunciado: float,
        preco_minimo: float,
        preco_sugerido: float | None = None,
        appraisal_data: dict | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE listings SET
                preco_anunciado = $1,
                preco_minimo = $2,
                preco_sugerido = COALESCE($3, preco_sugerido),
                appraisal_data = COALESCE($4, appraisal_data),
                updated_at = now()
            WHERE id = $5
            """,
            preco_anunciado,
            preco_minimo,
            preco_sugerido,
            json.dumps(appraisal_data) if appraisal_data else None,
            listing_id,
        )

    async def lock_for_negotiation(self, listing_id: int) -> asyncpg.Record | None:
        """Tenta travar o listing para negociação. Retorna None se indisponível."""
        async with self._db._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM listings WHERE id = $1 AND status = 'disponivel' FOR UPDATE",
                    listing_id,
                )
                if row:
                    await conn.execute(
                        "UPDATE listings SET status = 'em_negociacao', updated_at = now() WHERE id = $1",
                        listing_id,
                    )
                return row

    async def find_similar_sold(self, categoria: str, limit: int = 10) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT l.*, n.preco_atual_proposto as preco_final
            FROM listings l
            JOIN negotiations n ON n.listing_id = l.id
            WHERE l.categoria ILIKE $1 AND l.status = 'vendido' AND n.status = 'paga'
            ORDER BY l.updated_at DESC LIMIT $2
            """,
            f"%{categoria}%",
            limit,
        )

    async def add_to_interest_queue(
        self, listing_id: int, buyer_id: int, oferta_inicial: float | None = None
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO interest_queue (listing_id, buyer_id, oferta_inicial)
            VALUES ($1, $2, $3)
            ON CONFLICT (listing_id, buyer_id) DO NOTHING
            """,
            listing_id,
            buyer_id,
            oferta_inicial,
        )

    async def next_in_queue(self, listing_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM interest_queue WHERE listing_id = $1 ORDER BY timestamp ASC LIMIT 1",
            listing_id,
        )

    async def remove_from_queue(self, queue_id: int) -> None:
        await self._db.execute("DELETE FROM interest_queue WHERE id = $1", queue_id)

    async def get_queue_count(self, listing_id: int) -> int:
        return await self._db.fetch_val(
            "SELECT COUNT(*) FROM interest_queue WHERE listing_id = $1", listing_id
        ) or 0
