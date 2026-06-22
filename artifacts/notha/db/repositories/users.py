import asyncpg
from db.connection import DB


class PhoneInfoRepository:
    """Stores and retrieves parsed phone number data from user_phone_numbers."""

    def __init__(self, db: DB):
        self._db = db

    async def get(self, phone: str) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM user_phone_numbers WHERE phone = $1", phone
        )

    async def save(self, phone: str, info) -> None:
        """Persist PhoneInfo fields into user_phone_numbers for this phone."""
        from datetime import datetime, timezone
        await self._db.execute(
            """
            UPDATE user_phone_numbers SET
                country_code = $1,
                country_iso  = $2,
                country_name = $3,
                region       = $4,
                carrier      = $5,
                timezone     = $6,
                number_type  = $7,
                is_valid     = $8,
                parsed_at    = $9
            WHERE phone = $10
            """,
            info.country_code or None,
            info.country_iso or None,
            info.country_name or None,
            info.region or None,
            info.carrier or None,
            info.timezone or None,
            info.number_type or None,
            info.is_valid,
            datetime.now(timezone.utc),
            phone,
        )

    async def needs_parsing(self, phone: str) -> bool:
        """Returns True if this phone has never been parsed by phonenumbers."""
        row = await self._db.fetch_one(
            "SELECT parsed_at FROM user_phone_numbers WHERE phone = $1", phone
        )
        return row is None or row["parsed_at"] is None


class UserRepository:
    def __init__(self, db: DB):
        self._db = db

    async def find_by_phone(self, phone: str) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT u.* FROM users u
            JOIN user_phone_numbers p ON p.user_id = u.id
            WHERE p.phone = $1 AND p.active = TRUE
            """,
            phone,
        )

    async def find_by_tax_id(self, tax_id: str) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM users WHERE tax_id = $1", tax_id)

    async def find_by_id(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM users WHERE id = $1", user_id)

    async def create(self, full_name: str | None = None, tax_id: str | None = None) -> asyncpg.Record:
        return await self._db.fetch_one(
            "INSERT INTO users (full_name, tax_id) VALUES ($1, $2) RETURNING *",
            full_name,
            tax_id,
        )

    async def update(
        self,
        user_id: int,
        full_name: str | None = None,
        tax_id: str | None = None,
        nickname: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE users SET
                full_name  = COALESCE($1, full_name),
                tax_id     = COALESCE($2, tax_id),
                nickname   = COALESCE($3, nickname),
                updated_at = now()
            WHERE id = $4
            """,
            full_name,
            tax_id,
            nickname,
            user_id,
        )

    async def update_nickname(self, user_id: int, nickname: str) -> None:
        await self._db.execute(
            "UPDATE users SET nickname = $1, updated_at = now() WHERE id = $2",
            nickname,
            user_id,
        )

    async def update_location(self, user_id: int, city: str | None = None, neighborhood: str | None = None) -> None:
        await self._db.execute(
            """
            UPDATE users SET
                city         = COALESCE($1, city),
                neighborhood = COALESCE($2, neighborhood),
                updated_at   = now()
            WHERE id = $3
            """,
            city,
            neighborhood,
            user_id,
        )

    async def update_identity_status(self, user_id: int, status: str) -> None:
        """Update identity verification status.

        status: unverified | under_review | verified | rejected
        """
        await self._db.execute(
            "UPDATE users SET identity_status = $1, updated_at = now() WHERE id = $2",
            status,
            user_id,
        )

    async def register_identity_document(
        self,
        user_id: int,
        image_url: str,
        doc_type: str = "unknown",
        whatsapp_media_id: str | None = None,
    ) -> asyncpg.Record:
        """Register an identity document and set user status to under_review."""
        doc = await self._db.fetch_one(
            """
            INSERT INTO identity_documents
                (user_id, doc_type, image_url, whatsapp_media_id, status)
            VALUES ($1, $2, $3, $4, 'under_review')
            RETURNING *
            """,
            user_id,
            doc_type,
            image_url,
            whatsapp_media_id,
        )
        await self.update_identity_status(user_id, "under_review")
        return doc

    async def get_identity_documents(self, user_id: int) -> list[asyncpg.Record]:
        """Return all documents submitted by the user, most recent first."""
        return await self._db.fetch_all(
            "SELECT * FROM identity_documents WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )

    async def get_pending_document(self, user_id: int) -> asyncpg.Record | None:
        """Return the most recent document still under review."""
        return await self._db.fetch_one(
            """
            SELECT * FROM identity_documents
            WHERE user_id = $1 AND status = 'under_review'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
        )

    async def add_phone(self, user_id: int, phone: str) -> None:
        async with self._db._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE user_phone_numbers SET active = FALSE WHERE user_id = $1",
                    user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO user_phone_numbers (user_id, phone, active)
                    VALUES ($1, $2, TRUE)
                    ON CONFLICT (phone) DO UPDATE SET user_id = $1, active = TRUE
                    """,
                    user_id,
                    phone,
                )

    async def find_or_create_by_phone(self, phone: str) -> asyncpg.Record:
        """Find user by phone; create empty record if first contact."""
        existing = await self.find_by_phone(phone)
        if existing:
            return existing
        return await self.create_with_phone(phone)

    async def create_with_phone(self, phone: str, full_name: str | None = None) -> asyncpg.Record:
        async with self._db._pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow(
                    "INSERT INTO users (full_name) VALUES ($1) RETURNING *", full_name
                )
                await conn.execute(
                    "INSERT INTO user_phone_numbers (user_id, phone, active) VALUES ($1, $2, TRUE)",
                    user["id"],
                    phone,
                )
                return user

    async def get_seller_profile(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM seller_profile WHERE user_id = $1", user_id
        )

    async def get_buyer_profile(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM buyer_profile WHERE user_id = $1", user_id
        )

    async def get_courier_profile(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM courier_profile WHERE user_id = $1", user_id
        )

    async def upsert_seller_profile(
        self,
        user_id: int,
        pickup_address: str | None = None,
        available_hours=None,
        pix_key: str | None = None,
        pix_holder_name: str | None = None,
    ) -> None:
        import json
        await self._db.execute(
            """
            INSERT INTO seller_profile (user_id, pickup_address, available_hours, pix_key, pix_holder_name)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                pickup_address  = COALESCE($2, seller_profile.pickup_address),
                available_hours = COALESCE($3, seller_profile.available_hours),
                pix_key         = COALESCE($4, seller_profile.pix_key),
                pix_holder_name = COALESCE($5, seller_profile.pix_holder_name)
            """,
            user_id,
            pickup_address,
            json.dumps(available_hours) if available_hours else None,
            pix_key,
            pix_holder_name,
        )

    async def upsert_buyer_profile(self, user_id: int, delivery_address: str | None = None) -> None:
        await self._db.execute(
            """
            INSERT INTO buyer_profile (user_id, delivery_address)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET
                delivery_address = COALESCE($2, buyer_profile.delivery_address)
            """,
            user_id,
            delivery_address,
        )

    async def upsert_courier_profile(
        self,
        user_id: int,
        pix_key: str | None = None,
        pix_holder_name: str | None = None,
        service_area: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO courier_profile (user_id, pix_key, pix_holder_name, service_area)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                pix_key         = COALESCE($2, courier_profile.pix_key),
                pix_holder_name = COALESCE($3, courier_profile.pix_holder_name),
                service_area    = COALESCE($4, courier_profile.service_area)
            """,
            user_id,
            pix_key,
            pix_holder_name,
            service_area,
        )

    async def check_missing_fields(self, user_id: int, action: str) -> dict:
        user = await self.find_by_id(user_id)
        if not user:
            return {"missing": ["full_name", "tax_id"], "reason": "user_not_found"}

        if not user["full_name"] or not user["tax_id"]:
            missing = []
            if not user["full_name"]:
                missing.append("full_name")
            if not user["tax_id"]:
                missing.append("tax_id")
            return {"missing": missing, "reason": "missing_identification"}

        if action == "list_product":
            seller = await self.get_seller_profile(user_id)
            missing = []
            for field in ["pickup_address", "available_hours", "pix_key"]:
                if not seller or not seller[field]:
                    missing.append(field)
            if missing:
                return {"missing": missing, "reason": "incomplete_seller_profile"}

        return {"missing": [], "reason": None}
