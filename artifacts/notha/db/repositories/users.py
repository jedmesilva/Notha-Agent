import asyncpg
from db.connection import DB


class PhoneInfoRepository:
    """Armazena e recupera dados de número de telefone parseados de user_phone_numbers."""

    def __init__(self, db: DB):
        self._db = db

    async def get(self, phone: str) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM user_phone_numbers WHERE phone = $1", phone
        )

    async def save(self, phone: str, info) -> None:
        """Persiste campos de PhoneInfo em user_phone_numbers para este telefone."""
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
        """Retorna True se este telefone nunca foi parseado pelo phonenumbers."""
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

    async def update_full_address(
        self,
        user_id: int,
        street: str | None = None,
        street_number: str | None = None,
        neighborhood: str | None = None,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
        zip_code: str | None = None,
    ) -> None:
        """Atualiza o endereço completo do usuário."""
        await self._db.execute(
            """
            UPDATE users SET
                street        = COALESCE($1, street),
                street_number = COALESCE($2, street_number),
                neighborhood  = COALESCE($3, neighborhood),
                city          = COALESCE($4, city),
                state         = COALESCE($5, state),
                country       = COALESCE($6, country),
                zip_code      = COALESCE($7, zip_code),
                updated_at    = now()
            WHERE id = $8
            """,
            street,
            street_number,
            neighborhood,
            city,
            state,
            country,
            zip_code,
            user_id,
        )

    async def update_profile(
        self,
        user_id: int,
        gender: str | None = None,
        date_of_birth: str | None = None,
        preferred_language: str | None = None,
        full_name: str | None = None,
        tax_id: str | None = None,
        nickname: str | None = None,
    ) -> None:
        """Atualiza campos de perfil pessoal do usuário."""
        from datetime import date

        dob = None
        if date_of_birth:
            try:
                # Aceita formatos YYYY-MM-DD e DD/MM/YYYY
                if "/" in date_of_birth:
                    parts = date_of_birth.split("/")
                    if len(parts) == 3:
                        dob = date(int(parts[2]), int(parts[1]), int(parts[0]))
                else:
                    parts = date_of_birth.split("-")
                    if len(parts) == 3:
                        dob = date(int(parts[0]), int(parts[1]), int(parts[2]))
            except Exception:
                pass

        await self._db.execute(
            """
            UPDATE users SET
                gender             = COALESCE($1, gender),
                date_of_birth      = COALESCE($2, date_of_birth),
                preferred_language = COALESCE($3, preferred_language),
                full_name          = COALESCE($4, full_name),
                tax_id             = COALESCE($5, tax_id),
                nickname           = COALESCE($6, nickname),
                updated_at         = now()
            WHERE id = $7
            """,
            gender,
            dob,
            preferred_language,
            full_name,
            tax_id,
            nickname,
            user_id,
        )

    async def get_full_profile(self, user_id: int) -> dict:
        """Retorna um dicionário completo do perfil do usuário para exibição."""
        user = await self.find_by_id(user_id)
        if not user:
            return {}

        seller = await self.get_seller_profile(user_id)
        buyer  = await self.get_buyer_profile(user_id)

        profile = dict(user)
        profile["pix_key"]         = seller["pix_key"]         if seller else None
        profile["pickup_address"]  = seller["pickup_address"]  if seller else None
        profile["delivery_address"] = buyer["delivery_address"] if buyer else None
        return profile

    async def update_identity_status(self, user_id: int, status: str) -> None:
        """Atualiza o status de verificação de identidade.

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
        """Registra um documento de identidade e define o status como under_review."""
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
        """Retorna todos os documentos enviados pelo usuário, mais recentes primeiro."""
        return await self._db.fetch_all(
            "SELECT * FROM identity_documents WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )

    async def get_pending_document(self, user_id: int) -> asyncpg.Record | None:
        """Retorna o documento mais recente ainda em análise."""
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
        """Encontra usuário pelo telefone; cria registro vazio se for o primeiro contato."""
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

    async def check_missing_fields(self, user_id: int, operation: str) -> dict:
        """Verifica quais campos obrigatórios estão faltando para a operação especificada.

        Usa o módulo guardrails para a definição dos requisitos.
        Retorna dict com 'missing' (lista de campos) e 'reason'.
        """
        from guardrails import check_requirements, OPERATION_REQUIREMENTS

        user = await self.find_by_id(user_id)
        if not user:
            return {"missing": ["full_name", "tax_id"], "reason": "user_not_found"}

        # Monta o perfil para verificação
        seller  = await self.get_seller_profile(user_id)
        profile = {
            "full_name":         user.get("full_name") or "",
            "nickname":          user.get("nickname")  or "",
            "tax_id":            user.get("tax_id")    or "",
            "city":              user.get("city")      or "",
            "neighborhood":      user.get("neighborhood") or "",
            "street":            user.get("street")    or "",
            "state":             user.get("state")     or "",
            "zip_code":          user.get("zip_code")  or "",
            "gender":            user.get("gender")    or "",
            "date_of_birth":     user.get("date_of_birth"),
            "preferred_language": user.get("preferred_language") or "",
            "identity_status":   user.get("identity_status") or "unverified",
            "pix_key":           (seller["pix_key"]        if seller else "") or "",
            "pickup_address":    (seller["pickup_address"] if seller else "") or "",
            "phone_valid":       True,  # telefone sempre existe (é por onde vieram)
        }

        missing = check_requirements(operation, profile)
        if not missing:
            return {"missing": [], "reason": None}

        op_label = OPERATION_REQUIREMENTS.get(operation, {}).get("label", operation)
        return {
            "missing": missing,
            "reason": f"incomplete_profile_for_{operation}",
            "operation_label": op_label,
        }

    async def build_guardrail_context(self, user_id: int) -> str:
        """Gera texto de contexto com a completude do perfil para operações comuns.

        Inclui o que está faltando para cada operação — usado no prompt do agente.
        """
        from guardrails import check_requirements, OPERATION_REQUIREMENTS, FIELD_LABELS

        user   = await self.find_by_id(user_id)
        seller = await self.get_seller_profile(user_id)
        if not user:
            return "perfil: não encontrado"

        profile = {
            "full_name":        user.get("full_name")    or "",
            "nickname":         user.get("nickname")     or "",
            "tax_id":           user.get("tax_id")       or "",
            "city":             user.get("city")         or "",
            "neighborhood":     user.get("neighborhood") or "",
            "street":           user.get("street")       or "",
            "state":            user.get("state")        or "",
            "zip_code":         user.get("zip_code")     or "",
            "gender":           user.get("gender")       or "",
            "date_of_birth":    user.get("date_of_birth"),
            "identity_status":  user.get("identity_status") or "unverified",
            "pix_key":          (seller["pix_key"]        if seller else "") or "",
            "pickup_address":   (seller["pickup_address"] if seller else "") or "",
            "phone_valid":      True,
        }

        lines = []
        key_ops = ["search_product", "save_alert", "buy_product", "list_product", "receive_payment"]
        for op in key_ops:
            missing = check_requirements(op, profile)
            label   = OPERATION_REQUIREMENTS[op]["label"]
            if missing:
                missing_names = [FIELD_LABELS.get(f, f) for f in missing]
                lines.append(f"para {label}: faltam {', '.join(missing_names)}")
            else:
                lines.append(f"para {label}: ok")

        return "completude_perfil: " + " | ".join(lines)
