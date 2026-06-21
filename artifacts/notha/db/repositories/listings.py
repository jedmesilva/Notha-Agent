import json
import asyncpg
from db.connection import DB


class ListingRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        seller_id: int,
        description: str,
        category: str | None = None,
        photos: list | None = None,
        seller_asking_price: float | None = None,
        suggested_price: float | None = None,
        listed_price: float = 0,
        floor_price: float = 0,
        appraisal_data: dict | None = None,
        brand: str | None = None,
        model: str | None = None,
        version: str | None = None,
        usage_state: str | None = None,
        condition: str | None = None,
        has_receipt: bool | None = None,
        info_photos: list | None = None,
        seller_minimum_price: float | None = None,
        web_info: dict | None = None,
        seller_city: str | None = None,
        vision_analysis: str | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO listings (
                seller_id, description, category, photos,
                seller_asking_price, suggested_price, listed_price, floor_price,
                appraisal_data, status,
                brand, model, version, usage_state, condition, has_receipt,
                info_photos, seller_minimum_price, web_info, seller_city, vision_analysis
            )
            VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, 'available',
                $10, $11, $12, $13, $14, $15,
                $16, $17, $18, $19, $20
            )
            RETURNING *
            """,
            seller_id,
            description,
            category,
            json.dumps(photos or []),
            seller_asking_price,
            suggested_price,
            listed_price,
            floor_price,
            json.dumps(appraisal_data) if appraisal_data else None,
            brand,
            model,
            version,
            usage_state,
            condition,
            has_receipt,
            json.dumps(info_photos or []) if info_photos is not None else None,
            seller_minimum_price,
            json.dumps(web_info) if web_info else None,
            seller_city,
            vision_analysis,
        )

    async def find_by_id(self, listing_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM listings WHERE id = $1", listing_id)

    async def find_available(
        self,
        category: str | None = None,
        limit: int = 20,
        city: str | None = None,
        neighborhood: str | None = None,
    ) -> list[asyncpg.Record]:
        """Find available listings with optional category and location filters."""
        conditions = ["status = 'available'"]
        params: list = []
        idx = 1

        if category:
            conditions.append(f"category ILIKE ${idx}")
            params.append(f"%{category}%")
            idx += 1

        if neighborhood:
            conditions.append(f"seller_neighborhood ILIKE ${idx}")
            params.append(f"%{neighborhood}%")
            idx += 1
        elif city:
            conditions.append(f"seller_city ILIKE ${idx}")
            params.append(f"%{city}%")
            idx += 1

        params.append(limit)
        where = " AND ".join(conditions)
        query = f"SELECT * FROM listings WHERE {where} ORDER BY created_at DESC LIMIT ${idx}"
        return await self._db.fetch_all(query, *params)

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
        listed_price: float,
        floor_price: float,
        suggested_price: float | None = None,
        appraisal_data: dict | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE listings SET
                listed_price    = $1,
                floor_price     = $2,
                suggested_price = COALESCE($3, suggested_price),
                appraisal_data  = COALESCE($4, appraisal_data),
                updated_at      = now()
            WHERE id = $5
            """,
            listed_price,
            floor_price,
            suggested_price,
            json.dumps(appraisal_data) if appraisal_data else None,
            listing_id,
        )

    async def lock_for_negotiation(self, listing_id: int) -> asyncpg.Record | None:
        """Attempt to lock a listing for negotiation. Returns None if unavailable."""
        async with self._db._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM listings WHERE id = $1 AND status = 'available' FOR UPDATE",
                    listing_id,
                )
                if row:
                    await conn.execute(
                        "UPDATE listings SET status = 'in_negotiation', updated_at = now() WHERE id = $1",
                        listing_id,
                    )
                return row

    async def find_similar_sold(self, category: str, limit: int = 10) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT l.*, n.current_price AS final_price
            FROM listings l
            JOIN negotiations n ON n.listing_id = l.id
            WHERE l.category ILIKE $1 AND l.status = 'sold' AND n.status = 'paid'
            ORDER BY l.updated_at DESC LIMIT $2
            """,
            f"%{category}%",
            limit,
        )

    async def add_to_interest_queue(
        self, listing_id: int, buyer_id: int, initial_offer: float | None = None
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO interest_queue (listing_id, buyer_id, initial_offer)
            VALUES ($1, $2, $3)
            ON CONFLICT (listing_id, buyer_id) DO NOTHING
            """,
            listing_id,
            buyer_id,
            initial_offer,
        )

    async def next_in_queue(self, listing_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM interest_queue WHERE listing_id = $1 ORDER BY created_at ASC LIMIT 1",
            listing_id,
        )

    async def remove_from_queue(self, queue_id: int) -> None:
        await self._db.execute("DELETE FROM interest_queue WHERE id = $1", queue_id)

    async def get_queue_count(self, listing_id: int) -> int:
        return await self._db.fetch_val(
            "SELECT COUNT(*) FROM interest_queue WHERE listing_id = $1", listing_id
        ) or 0
