"""
GroupRepository — groups, user_groups, group_pool_limits,
                  group_rate_policies, term_rate_curve, score_limit_bands.

Criação de grupo completo em um único fluxo:
  create_full() — grupo + wallet + pool_limit + rate_policy + term_curve + score_bands
"""
import logging
from decimal import Decimal
from datetime import datetime, timezone
from db.connection import DB

logger = logging.getLogger("notha.groups")

_ZERO = Decimal("0")


class GroupRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Grupo base ─────────────────────────────────────────────────────────────

    async def create(self, name: str, description: str | None = None) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO groups (name, description, status)
            VALUES ($1, $2, 'active')
            RETURNING id
            """,
            name, description,
        )

    async def get_by_id(self, group_id: int):
        return await self._db.fetch_one(
            "SELECT * FROM groups WHERE id = $1", group_id
        )

    async def list_all(self, status: str | None = None) -> list:
        if status:
            return await self._db.fetch_all(
                "SELECT * FROM groups WHERE status = $1 ORDER BY created_at DESC", status
            )
        return await self._db.fetch_all(
            "SELECT * FROM groups ORDER BY created_at DESC"
        )

    async def update_status(self, group_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE groups SET status = $1 WHERE id = $2", status, group_id
        )

    # ── Pool limit ─────────────────────────────────────────────────────────────

    async def set_pool_limit(
        self,
        group_id: int,
        max_aggregate_exposure: Decimal,
        max_per_user_limit: Decimal | None = None,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO group_pool_limits
                (group_id, max_aggregate_exposure, max_per_user_limit, current_exposure_cache)
            VALUES ($1, $2, $3, 0)
            RETURNING id
            """,
            group_id, max_aggregate_exposure, max_per_user_limit,
        )

    async def get_pool_limit(self, group_id: int):
        return await self._db.fetch_one(
            """
            SELECT * FROM group_pool_limits
            WHERE group_id = $1
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            group_id,
        )

    # ── Rate policy ────────────────────────────────────────────────────────────

    async def set_rate_policy(
        self,
        group_id: int,
        base_borrowing_rate: Decimal,
        base_investment_rate: Decimal,
        min_spread: Decimal,
        spread_violation_strategy: str = "reject_investment",
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO group_rate_policies
                (group_id, base_borrowing_rate, base_investment_rate,
                 min_spread, spread_violation_strategy)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            group_id, base_borrowing_rate, base_investment_rate,
            min_spread, spread_violation_strategy,
        )

    async def get_rate_policy(self, group_id: int):
        return await self._db.fetch_one(
            """
            SELECT * FROM group_rate_policies
            WHERE group_id = $1
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            group_id,
        )

    # ── Term rate curve ────────────────────────────────────────────────────────

    async def set_term_curve(self, group_id: int, bands: list[dict]) -> None:
        """
        Recria a curva de prazo do grupo.
        Cada banda: {min_term_days, max_term_days, adjustment_bps}
        """
        await self._db.execute(
            "DELETE FROM term_rate_curve WHERE group_id = $1", group_id
        )
        for b in bands:
            await self._db.execute(
                """
                INSERT INTO term_rate_curve (group_id, min_term_days, max_term_days, adjustment_bps)
                VALUES ($1, $2, $3, $4)
                """,
                group_id,
                int(b["min_term_days"]),
                int(b["max_term_days"]),
                int(b.get("adjustment_bps", 0)),
            )

    async def get_term_curve(self, group_id: int) -> list:
        return await self._db.fetch_all(
            """
            SELECT * FROM term_rate_curve
            WHERE group_id = $1
            ORDER BY min_term_days ASC
            """,
            group_id,
        )

    # ── Score limit bands ──────────────────────────────────────────────────────

    async def set_score_bands(self, group_id: int, bands: list[dict]) -> None:
        """
        Recria as faixas de score → percentual do teto por usuário.
        Cada banda: {min_score, max_score, limit_percentage, label?}
        """
        await self._db.execute(
            "DELETE FROM score_limit_bands WHERE group_id = $1", group_id
        )
        for b in bands:
            await self._db.execute(
                """
                INSERT INTO score_limit_bands
                    (group_id, min_score, max_score, limit_percentage, label)
                VALUES ($1, $2, $3, $4, $5)
                """,
                group_id,
                Decimal(str(b["min_score"])),
                Decimal(str(b["max_score"])),
                Decimal(str(b["limit_percentage"])),
                b.get("label"),
            )

    async def get_score_bands(self, group_id: int) -> list:
        return await self._db.fetch_all(
            """
            SELECT * FROM score_limit_bands
            WHERE group_id = $1
            ORDER BY min_score ASC
            """,
            group_id,
        )

    # ── Membros ────────────────────────────────────────────────────────────────

    async def add_member(
        self,
        group_id: int,
        user_id: int,
        allocation_reason: str | None = None,
    ) -> int:
        # Encerra alocação ativa anterior no mesmo grupo, se existir
        await self._db.execute(
            """
            UPDATE user_groups
               SET left_at = NOW()
             WHERE user_id  = $1
               AND group_id = $2
               AND left_at IS NULL
            """,
            user_id, group_id,
        )
        return await self._db.fetch_val(
            """
            INSERT INTO user_groups (user_id, group_id, allocation_reason)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            user_id, group_id, allocation_reason,
        )

    async def remove_member(self, group_id: int, user_id: int) -> bool:
        result = await self._db.execute(
            """
            UPDATE user_groups
               SET left_at = NOW()
             WHERE user_id  = $1
               AND group_id = $2
               AND left_at IS NULL
            """,
            user_id, group_id,
        )
        return result == "UPDATE 1"

    async def list_members(self, group_id: int, active_only: bool = True) -> list:
        if active_only:
            return await self._db.fetch_all(
                """
                SELECT ug.*, u.full_name, u.nickname, u.tax_id, u.identity_status
                FROM user_groups ug
                JOIN users u ON u.id = ug.user_id
                WHERE ug.group_id = $1 AND ug.left_at IS NULL
                ORDER BY ug.joined_at DESC
                """,
                group_id,
            )
        return await self._db.fetch_all(
            """
            SELECT ug.*, u.full_name, u.nickname, u.tax_id, u.identity_status
            FROM user_groups ug
            JOIN users u ON u.id = ug.user_id
            WHERE ug.group_id = $1
            ORDER BY ug.joined_at DESC
            """,
            group_id,
        )

    async def get_user_groups(self, user_id: int, active_only: bool = True) -> list:
        if active_only:
            return await self._db.fetch_all(
                """
                SELECT ug.*, g.name, g.status AS group_status
                FROM user_groups ug
                JOIN groups g ON g.id = ug.group_id
                WHERE ug.user_id = $1 AND ug.left_at IS NULL
                ORDER BY ug.joined_at DESC
                """,
                user_id,
            )
        return await self._db.fetch_all(
            """
            SELECT ug.*, g.name, g.status AS group_status
            FROM user_groups ug
            JOIN groups g ON g.id = ug.group_id
            WHERE ug.user_id = $1
            ORDER BY ug.joined_at DESC
            """,
            user_id,
        )

    # ── Upgrade events ─────────────────────────────────────────────────────────

    async def list_upgrade_candidates(self, status: str = "suggested") -> list:
        return await self._db.fetch_all(
            """
            SELECT e.*, u.full_name, u.nickname,
                   gf.name AS from_group_name,
                   gt.name AS to_group_name
            FROM group_upgrade_events e
            JOIN users u ON u.id = e.user_id
            LEFT JOIN groups gf ON gf.id = e.from_group_id
            LEFT JOIN groups gt ON gt.id = e.to_group_id
            WHERE e.status = $1
            ORDER BY e.created_at DESC
            """,
            status,
        )

    async def resolve_upgrade(
        self,
        event_id: int,
        resolution: str,          # accepted | rejected
        to_group_id: int | None = None,
        allocation_reason: str | None = None,
    ) -> dict:
        """
        Resolve uma sugestão de upgrade.
        Se accepted e to_group_id fornecido, move o usuário para o novo grupo.
        """
        event = await self._db.fetch_one(
            "SELECT * FROM group_upgrade_events WHERE id = $1", event_id
        )
        if not event:
            return {"ok": False, "error": "Evento não encontrado"}
        if event["status"] != "suggested":
            return {"ok": False, "error": f"Evento já resolvido: {event['status']}"}

        await self._db.execute(
            """
            UPDATE group_upgrade_events
               SET status = $1, to_group_id = COALESCE($2, to_group_id), resolved_at = NOW()
             WHERE id = $3
            """,
            resolution, to_group_id, event_id,
        )

        if resolution == "accepted" and to_group_id:
            await self.add_member(
                group_id=to_group_id,
                user_id=event["user_id"],
                allocation_reason=allocation_reason or f"Upgrade automático do grupo {event['from_group_id']}",
            )

        return {"ok": True, "event_id": event_id, "resolution": resolution}

    # ── Visão consolidada de um grupo ──────────────────────────────────────────

    async def get_full_profile(self, group_id: int) -> dict | None:
        group = await self.get_by_id(group_id)
        if not group:
            return None

        pool        = await self.get_pool_limit(group_id)
        rate_policy = await self.get_rate_policy(group_id)
        term_curve  = await self.get_term_curve(group_id)
        score_bands = await self.get_score_bands(group_id)
        members     = await self.list_members(group_id, active_only=True)

        # Wallet do grupo
        wallet = await self._db.fetch_one(
            "SELECT * FROM wallets WHERE owner_type = 'group' AND owner_id = $1",
            group_id,
        )

        return {
            "group":       dict(group),
            "pool_limit":  dict(pool) if pool else None,
            "rate_policy": dict(rate_policy) if rate_policy else None,
            "term_curve":  [dict(r) for r in term_curve],
            "score_bands": [dict(r) for r in score_bands],
            "members":     [dict(r) for r in members],
            "wallet_balance": float(wallet["balance_cache"]) if wallet else 0.0,
        }

    # ── create_full ────────────────────────────────────────────────────────────

    async def create_full(self, payload: dict) -> dict:
        """
        Cria um grupo completo em uma única chamada:
          - grupo base
          - wallet (owner_type='group')
          - pool_limit  (max_aggregate_exposure + max_per_user_limit)
          - rate_policy (base_borrowing_rate, base_investment_rate, min_spread, ...)
          - term_curve  (lista de faixas de prazo)
          - score_bands (lista de faixas de score → percentual)

        payload esperado (todos os campos opcionais têm default seguro):
        {
          "name": str,
          "description": str | None,
          "max_aggregate_exposure": float,
          "max_per_user_limit": float | None,
          "base_borrowing_rate": float,       ex: 0.04 = 4% a.m.
          "base_investment_rate": float,      ex: 0.025 = 2.5% a.m.
          "min_spread": float,                ex: 0.01 = 1%
          "spread_violation_strategy": str,   "reject_investment" | "raise_borrowing_rate"
          "term_curve": [                     opcional — default: sem ajuste de prazo
            {"min_term_days": 1, "max_term_days": 30, "adjustment_bps": 0},
            ...
          ],
          "score_bands": [                    opcional — default: sem faixas (limite fixo)
            {"min_score": 0, "max_score": 300, "limit_percentage": 0.20, "label": "Iniciante"},
            ...
          ]
        }
        """
        # 1. Grupo base
        group_id = await self.create(
            name=payload["name"],
            description=payload.get("description"),
        )

        # 2. Wallet do grupo
        await self._db.execute(
            """
            INSERT INTO wallets (owner_type, owner_id, balance_cache)
            VALUES ('group', $1, 0)
            ON CONFLICT (owner_type, owner_id) DO NOTHING
            """,
            group_id,
        )

        # 3. Pool limit
        await self.set_pool_limit(
            group_id=group_id,
            max_aggregate_exposure=Decimal(str(payload["max_aggregate_exposure"])),
            max_per_user_limit=(
                Decimal(str(payload["max_per_user_limit"]))
                if payload.get("max_per_user_limit") is not None
                else None
            ),
        )

        # 4. Rate policy
        await self.set_rate_policy(
            group_id=group_id,
            base_borrowing_rate=Decimal(str(payload["base_borrowing_rate"])),
            base_investment_rate=Decimal(str(payload["base_investment_rate"])),
            min_spread=Decimal(str(payload["min_spread"])),
            spread_violation_strategy=payload.get(
                "spread_violation_strategy", "reject_investment"
            ),
        )

        # 5. Curva de prazo (opcional)
        if payload.get("term_curve"):
            await self.set_term_curve(group_id, payload["term_curve"])

        # 6. Faixas de score (opcional)
        if payload.get("score_bands"):
            await self.set_score_bands(group_id, payload["score_bands"])

        logger.info(
            "Grupo criado: id=%d name=%s max_aggregate=%.2f max_per_user=%s",
            group_id,
            payload["name"],
            float(payload["max_aggregate_exposure"]),
            payload.get("max_per_user_limit"),
        )

        return await self.get_full_profile(group_id)
