"""
SegmentRepository — segments, segment_parameters, user_segments.

Segmentos agrupam usuários com parâmetros em comum.
Um usuário pode estar em múltiplos segmentos simultaneamente.
Segmentos NÃO controlam limite financeiro — apenas aplicam ajustes
e contexto sobre as políticas do nível atual do usuário.
"""
import logging
from db.connection import DB

logger = logging.getLogger("notha.segments")


class SegmentRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Segmento base ──────────────────────────────────────────────────────────

    async def create(
        self,
        name: str,
        description: str | None = None,
        criteria_type: str = "manual",
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO segments (name, description, criteria_type, status)
            VALUES ($1, $2, $3, 'active')
            RETURNING id
            """,
            name, description, criteria_type,
        )

    async def get_by_id(self, segment_id: int):
        return await self._db.fetch_one(
            "SELECT * FROM segments WHERE id = $1", segment_id
        )

    async def list_all(self, status: str | None = None) -> list:
        if status:
            return await self._db.fetch_all(
                "SELECT * FROM segments WHERE status = $1 ORDER BY name ASC", status
            )
        return await self._db.fetch_all("SELECT * FROM segments ORDER BY name ASC")

    async def update_status(self, segment_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE segments SET status = $1 WHERE id = $2", status, segment_id
        )

    # ── Parâmetros ─────────────────────────────────────────────────────────────

    async def set_parameter(
        self,
        segment_id: int,
        key: str,
        value: str,
        value_type: str = "string",
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO segment_parameters (segment_id, key, value, value_type)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (segment_id, key) DO UPDATE SET
                value      = EXCLUDED.value,
                value_type = EXCLUDED.value_type
            """,
            segment_id, key, value, value_type,
        )

    async def delete_parameter(self, segment_id: int, key: str) -> None:
        await self._db.execute(
            "DELETE FROM segment_parameters WHERE segment_id = $1 AND key = $2",
            segment_id, key,
        )

    async def get_parameters(self, segment_id: int) -> dict:
        """Retorna os parâmetros do segmento como dicionário {key: value}."""
        rows = await self._db.fetch_all(
            "SELECT key, value, value_type FROM segment_parameters WHERE segment_id = $1",
            segment_id,
        )
        result = {}
        for row in rows:
            raw = row["value"]
            vtype = row["value_type"]
            if vtype == "number":
                try:
                    raw = float(raw)
                except ValueError:
                    pass
            elif vtype == "boolean":
                raw = raw.lower() in ("true", "1", "yes")
            elif vtype == "json":
                import json
                try:
                    raw = json.loads(raw)
                except Exception:
                    pass
            result[row["key"]] = raw
        return result

    async def set_parameters_bulk(
        self, segment_id: int, params: dict[str, tuple[str, str]]
    ) -> None:
        """
        Define múltiplos parâmetros de uma vez.
        params = {key: (value_str, value_type)}
        """
        for key, (value, vtype) in params.items():
            await self.set_parameter(segment_id, key, value, vtype)

    # ── Membros ─────────────────────────────────────────────────────────────────

    async def add_member(
        self,
        segment_id: int,
        user_id: int,
        reason: str | None = None,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO user_segments (user_id, segment_id, reason)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, segment_id) DO NOTHING
            RETURNING id
            """,
            user_id, segment_id, reason,
        )

    async def remove_member(self, segment_id: int, user_id: int) -> bool:
        result = await self._db.execute(
            "DELETE FROM user_segments WHERE user_id = $1 AND segment_id = $2",
            user_id, segment_id,
        )
        return result == "DELETE 1"

    async def list_members(self, segment_id: int, limit: int = 200) -> list:
        return await self._db.fetch_all(
            """
            SELECT us.*, u.full_name, u.nickname, u.identity_status, u.current_level
            FROM user_segments us
            JOIN users u ON u.id = us.user_id
            WHERE us.segment_id = $1
            ORDER BY us.joined_at DESC
            LIMIT $2
            """,
            segment_id, limit,
        )

    async def get_user_segments(self, user_id: int) -> list:
        """Retorna todos os segmentos do usuário com seus parâmetros."""
        return await self._db.fetch_all(
            """
            SELECT us.*, s.name, s.description, s.criteria_type, s.status AS segment_status
            FROM user_segments us
            JOIN segments s ON s.id = us.segment_id
            WHERE us.user_id = $1
              AND s.status = 'active'
            ORDER BY s.name ASC
            """,
            user_id,
        )

    async def get_user_segment_params(self, user_id: int) -> dict:
        """
        Retorna os parâmetros agregados de todos os segmentos ativos do usuário.
        Se o mesmo parâmetro aparece em múltiplos segmentos, o último prevalece.
        Use para enriquecer decisões financeiras com contexto do usuário.
        """
        segments = await self.get_user_segments(user_id)
        merged: dict = {}
        for seg in segments:
            params = await self.get_parameters(seg["segment_id"])
            merged.update(params)
        return merged

    # ── Visão consolidada ──────────────────────────────────────────────────────

    async def get_full_profile(self, segment_id: int) -> dict | None:
        segment = await self.get_by_id(segment_id)
        if not segment:
            return None
        params  = await self.get_parameters(segment_id)
        members = await self.list_members(segment_id, limit=50)
        member_count = await self._db.fetch_val(
            "SELECT COUNT(*) FROM user_segments WHERE segment_id = $1", segment_id
        ) or 0
        return {
            "segment":      dict(segment),
            "parameters":   params,
            "member_count": member_count,
            "members":      [dict(r) for r in members],
        }
