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
        """Salva um alerta de interesse de produto para o usuário."""
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
        """Retorna todos os alertas ativos para verificar contra novos anúncios."""
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

    async def cancel_all_by_user(self, user_id: int) -> int:
        """Cancela todos os alertas ativos do usuário. Retorna quantos foram cancelados."""
        result = await self._db.execute(
            "UPDATE saved_searches SET status = 'cancelled' WHERE user_id = $1 AND status = 'active'",
            user_id,
        )
        try:
            return int(result.split()[-1])
        except Exception:
            return 0

    async def cancel_by_description(self, user_id: int, description: str) -> list[asyncpg.Record]:
        """Cancela alertas cujo search_description contenha as palavras-chave da descrição.

        Retorna os registros cancelados para que o agente possa confirmar quais foram removidos.
        """
        _STOP = {"de", "da", "do", "das", "dos", "em", "um", "uma", "e", "ou", "para", "com"}
        words = [
            w for w in description.lower().split()
            if len(w) >= 3 and w not in _STOP
        ]
        if not words:
            return []

        active = await self.find_by_user(user_id)
        matched = []
        for alert in active:
            alert_text = (alert["search_description"] or "").lower()
            if any(w in alert_text for w in words):
                matched.append(alert)

        for alert in matched:
            await self.cancel(alert["id"])

        return matched

    async def record_notification(self, search_id: int) -> None:
        """Atualiza o timestamp da última notificação enviada para este alerta."""
        from datetime import datetime, timezone
        await self._db.execute(
            "UPDATE saved_searches SET last_notified_at = $1 WHERE id = $2",
            datetime.now(timezone.utc),
            search_id,
        )

    def matches(self, search: asyncpg.Record, listing: dict) -> bool:
        """Verifica se um novo anúncio corresponde a um alerta salvo.

        Critérios:
        - Pelo menos uma palavra significativa da descrição do alerta aparece
          na descrição ou categoria do anúncio (case-insensitive)
        - Se o alerta tem search_city: a cidade do anúncio deve ser compatível
          (ou seller_city vazio — benefício da dúvida)
        """
        listing_description = (listing.get("description") or "").lower()
        listing_category    = (listing.get("category") or "").lower()
        listing_text = f"{listing_description} {listing_category}"

        _STOP = {"de", "da", "do", "das", "dos", "em", "um", "uma", "e", "ou", "para", "com"}
        words = [
            w for w in search["search_description"].lower().split()
            if len(w) >= 3 and w not in _STOP
        ]
        if not words:
            return False

        if not any(w in listing_text for w in words):
            return False

        search_city  = (search["search_city"] or "").lower().strip()
        listing_city = (listing.get("seller_city") or "").lower().strip()
        if search_city and listing_city:
            if search_city not in listing_city and listing_city not in search_city:
                return False

        return True
