import json
import asyncpg
from datetime import datetime, timedelta
from db.connection import DB
from config import TIMEOUT_RODADA_MINUTOS, EXPIRACAO_TOTAL_HORAS


class NegotiationRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        listing_id: int,
        buyer_id: int,
        modo: str = "proxy",
        limite_comprador: dict | None = None,
        limite_vendedor: dict | None = None,
    ) -> asyncpg.Record:
        now = datetime.utcnow()
        return await self._db.fetch_one(
            """
            INSERT INTO negotiations
                (listing_id, buyer_id, modo, status, limite_comprador, limite_vendedor,
                 responder_until, expires_at)
            VALUES ($1, $2, $3, 'ativa', $4, $5, $6, $7)
            RETURNING *
            """,
            listing_id,
            buyer_id,
            modo,
            json.dumps(limite_comprador) if limite_comprador else None,
            json.dumps(limite_vendedor) if limite_vendedor else None,
            now + timedelta(minutes=TIMEOUT_RODADA_MINUTOS),
            now + timedelta(hours=EXPIRACAO_TOTAL_HORAS),
        )

    async def find_by_id(self, negotiation_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM negotiations WHERE id = $1", negotiation_id
        )

    async def find_active_by_listing(self, listing_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM negotiations WHERE listing_id = $1 AND status = 'ativa' ORDER BY created_at DESC LIMIT 1",
            listing_id,
        )

    async def find_active_by_buyer(self, buyer_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiations WHERE buyer_id = $1 AND status IN ('ativa', 'proposta_ao_vendedor', 'proposta_ao_comprador')",
            buyer_id,
        )

    async def set_status(self, negotiation_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE negotiations SET status = $1 WHERE id = $2",
            status,
            negotiation_id,
        )

    async def update_offer(self, negotiation_id: int, preco: float, status: str | None = None) -> None:
        if status:
            await self._db.execute(
                """
                UPDATE negotiations SET
                    preco_atual_proposto = $1,
                    status = $2,
                    tentativas_humanas = tentativas_humanas + 1,
                    responder_until = $3
                WHERE id = $4
                """,
                preco,
                status,
                datetime.utcnow() + timedelta(minutes=TIMEOUT_RODADA_MINUTOS),
                negotiation_id,
            )
        else:
            await self._db.execute(
                "UPDATE negotiations SET preco_atual_proposto = $1 WHERE id = $2",
                preco,
                negotiation_id,
            )

    async def update_seller_limit(self, negotiation_id: int, limite_vendedor: dict) -> None:
        await self._db.execute(
            "UPDATE negotiations SET limite_vendedor = $1 WHERE id = $2",
            json.dumps(limite_vendedor),
            negotiation_id,
        )

    async def find_timed_out(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiations WHERE status = 'ativa' AND responder_until < now()"
        )

    async def find_totally_expired(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiations WHERE status = 'ativa' AND expires_at < now()"
        )

    async def add_offer(
        self,
        negotiation_id: int,
        autor: str,
        valor_proposto: float,
        contexto_extra: str | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO negotiation_offers (negotiation_id, autor, valor_proposto, contexto_extra)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            negotiation_id,
            autor,
            valor_proposto,
            contexto_extra,
        )

    async def get_offers(self, negotiation_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM negotiation_offers WHERE negotiation_id = $1 ORDER BY timestamp ASC",
            negotiation_id,
        )

    async def add_proxy_round(
        self,
        negotiation_id: int,
        rodada: int,
        valor_proposto: float,
        argumento_vendedor: str | None = None,
        argumento_comprador: str | None = None,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO proxy_negotiation_rounds
                (negotiation_id, rodada, valor_proposto, argumento_vendedor, argumento_comprador)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            negotiation_id,
            rodada,
            valor_proposto,
            argumento_vendedor,
            argumento_comprador,
        )

    async def confirm_proxy_round(
        self,
        round_id: int,
        confirmado_pelo_vendedor: bool | None = None,
        confirmado_pelo_comprador: bool | None = None,
    ) -> None:
        if confirmado_pelo_vendedor is not None:
            await self._db.execute(
                "UPDATE proxy_negotiation_rounds SET confirmado_pelo_vendedor = $1 WHERE id = $2",
                confirmado_pelo_vendedor,
                round_id,
            )
        if confirmado_pelo_comprador is not None:
            await self._db.execute(
                "UPDATE proxy_negotiation_rounds SET confirmado_pelo_comprador = $1 WHERE id = $2",
                confirmado_pelo_comprador,
                round_id,
            )

    async def get_proxy_rounds(self, negotiation_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM proxy_negotiation_rounds WHERE negotiation_id = $1 ORDER BY rodada ASC",
            negotiation_id,
        )

    async def get_rejected_values(self, negotiation_id: int) -> list[float]:
        rows = await self._db.fetch_all(
            """
            SELECT valor_proposto FROM proxy_negotiation_rounds
            WHERE negotiation_id = $1
            AND (confirmado_pelo_vendedor = FALSE OR confirmado_pelo_comprador = FALSE)
            """,
            negotiation_id,
        )
        return [r["valor_proposto"] for r in rows]
