import asyncpg
from db.connection import DB


class SavedSearchRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        user_id: int,
        phone: str,
        search_description: str,
        category: str | None = None,
        search_city: str | None = None,
        search_neighborhood: str | None = None,
    ) -> asyncpg.Record:
        """Save a product interest search for the user."""
        return await self._db.fetch_one(
            """
            INSERT INTO saved_searches
                (user_id, phone, search_description, category, search_city, search_neighborhood)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            user_id,
            phone,
            search_description,
            category,
            search_city,
            search_neighborhood,
        )

    async def find_active(self) -> list[asyncpg.Record]:
        """Return all active searches to check against new listings."""
        return await self._db.fetch_all(
            "SELECT * FROM saved_searches WHERE status = 'active' ORDER BY created_at ASC"
        )

    async def find_by_user(self, user_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM saved_searches
            WHERE user_id = $1 AND status = 'active'
            ORDER BY created_at DESC
            """,
            user_id,
        )

    async def cancel(self, search_id: int) -> None:
        await self._db.execute(
            "UPDATE saved_searches SET status = 'cancelled' WHERE id = $1",
            search_id,
        )

    async def cancel_all_by_user(self, user_id: int) -> None:
        await self._db.execute(
            "UPDATE saved_searches SET status = 'cancelled' WHERE user_id = $1 AND status = 'active'",
            user_id,
        )

    async def record_notification(self, search_id: int) -> None:
        """Update the timestamp of the last notification sent for this search."""
        from datetime import datetime, timezone
        await self._db.execute(
            "UPDATE saved_searches SET last_notified_at = $1 WHERE id = $2",
            datetime.now(timezone.utc),
            search_id,
        )

    def matches(self, search: asyncpg.Record, listing: dict) -> bool:
        """Check if a newly-created listing matches a saved search.

        Criteria:
        - At least one significant word from search_description appears in
          the listing's description or category (case-insensitive)
        - If the search has a search_city: listing.seller_city must be compatible
          (or seller_city is null/empty — benefit of the doubt)
        """
        listing_description = (listing.get("description") or "").lower()
        listing_category    = (listing.get("category") or "").lower()
        listing_text = f"{listing_description} {listing_category}"

        # Words with >= 3 chars (ignore articles, prepositions)
        _STOP = {"de", "da", "do", "das", "dos", "em", "um", "uma", "e", "ou", "para", "com"}
        words = [
            w for w in search["search_description"].lower().split()
            if len(w) >= 3 and w not in _STOP
        ]
        if not words:
            return False

        if not any(w in listing_text for w in words):
            return False

        # Geographic filter: if the search specifies a city, the listing must match
        search_city  = (search["search_city"] or "").lower().strip()
        listing_city = (listing.get("seller_city") or "").lower().strip()
        if search_city and listing_city:
            if search_city not in listing_city and listing_city not in search_city:
                return False

        return True
