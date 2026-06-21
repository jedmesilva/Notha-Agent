import json
import asyncpg
from db.connection import DB


class ListingFlowRepository:
    def __init__(self, db: DB):
        self._db = db

    async def get_active(self, phone: str) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT * FROM listing_flows
            WHERE phone = $1 AND step != 'done'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            phone,
        )

    async def create(self, user_id: int, phone: str) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO listing_flows (user_id, phone, step, data, photos)
            VALUES ($1, $2, 'product', '{}', '[]')
            RETURNING *
            """,
            user_id,
            phone,
        )

    async def update_step(
        self,
        flow_id: int,
        step: str,
        data: dict,
        photos: list | None = None,
    ) -> None:
        if photos is not None:
            await self._db.execute(
                """
                UPDATE listing_flows
                SET step = $1, data = $2::jsonb, photos = $3::jsonb, updated_at = NOW()
                WHERE id = $4
                """,
                step,
                json.dumps(data, ensure_ascii=False),
                json.dumps(photos, ensure_ascii=False),
                flow_id,
            )
        else:
            await self._db.execute(
                """
                UPDATE listing_flows
                SET step = $1, data = $2::jsonb, updated_at = NOW()
                WHERE id = $3
                """,
                step,
                json.dumps(data, ensure_ascii=False),
                flow_id,
            )

    async def add_photo(self, flow_id: int, media_id: str, mime_type: str, caption: str = "") -> None:
        await self._db.execute(
            """
            UPDATE listing_flows
            SET photos = photos || $1::jsonb, updated_at = NOW()
            WHERE id = $2
            """,
            json.dumps([{"media_id": media_id, "mime_type": mime_type, "caption": caption}]),
            flow_id,
        )

    async def mark_done(self, flow_id: int) -> None:
        await self._db.execute(
            "UPDATE listing_flows SET step = 'done', updated_at = NOW() WHERE id = $1",
            flow_id,
        )

    async def cancel(self, phone: str) -> None:
        await self._db.execute(
            """
            UPDATE listing_flows SET step = 'done', updated_at = NOW()
            WHERE phone = $1 AND step != 'done'
            """,
            phone,
        )
