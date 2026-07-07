"""
LevelRepository — levels, level_policies, level_term_curve,
                  liquidity_snapshots, user_level_history, credit_limits.

Criação de nível completo em um único fluxo:
  create_full() — level + wallet + policy + term_curve

Transição de nível:
  transition_user_level() — atualiza users.current_level + registra user_level_history
"""
import logging
from decimal import Decimal
from db.connection import DB

logger = logging.getLogger("notha.levels")

_ZERO = Decimal("0")

UPGRADE_TRIGGER_PCT = Decimal("0.95")


class LevelRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Nível base ─────────────────────────────────────────────────────────────

    async def get_by_id(self, level_id: int):
        return await self._db.fetch_one(
            "SELECT * FROM levels WHERE id = $1", level_id
        )

    async def list_all(self, status: str | None = None) -> list:
        if status:
            return await self._db.fetch_all(
                "SELECT * FROM levels WHERE status = $1 ORDER BY id ASC", status
            )
        return await self._db.fetch_all("SELECT * FROM levels ORDER BY id ASC")

    async def update_status(self, level_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE levels SET status = $1 WHERE id = $2", status, level_id
        )

    # ── Policy ─────────────────────────────────────────────────────────────────

    async def set_policy(
        self,
        level_id: int,
        base_borrowing_rate: Decimal,
        base_investment_rate: Decimal,
        min_spread: Decimal,
        max_aggregate_exposure: Decimal,
        max_per_user_limit: Decimal | None = None,
        spread_violation_strategy: str = "reject_investment",
        term_rate_formula: str = "bands",
        term_rate_base_bps: Decimal | None = None,
        term_rate_scale: Decimal | None = None,
        default_individual_limit: Decimal | None = None,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO level_policies
                (level_id, base_borrowing_rate, base_investment_rate,
                 min_spread, spread_violation_strategy,
                 term_rate_formula, term_rate_base_bps, term_rate_scale,
                 max_aggregate_exposure, max_per_user_limit,
                 current_exposure_cache, default_individual_limit)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, $11)
            RETURNING id
            """,
            level_id, base_borrowing_rate, base_investment_rate,
            min_spread, spread_violation_strategy,
            term_rate_formula, term_rate_base_bps, term_rate_scale,
            max_aggregate_exposure, max_per_user_limit,
            default_individual_limit,
        )

    async def get_policy(self, level_id: int):
        return await self._db.fetch_one(
            """
            SELECT * FROM level_policies
            WHERE level_id = $1
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            level_id,
        )

    async def increment_exposure(self, level_id: int, amount: Decimal) -> None:
        await self._db.execute(
            """
            UPDATE level_policies
               SET current_exposure_cache = current_exposure_cache + $1
             WHERE level_id = $2
               AND id = (
                   SELECT id FROM level_policies
                   WHERE level_id = $2
                   ORDER BY effective_from DESC
                   LIMIT 1
               )
            """,
            amount, level_id,
        )

    async def decrement_exposure(self, level_id: int, amount: Decimal) -> None:
        await self._db.execute(
            """
            UPDATE level_policies
               SET current_exposure_cache = GREATEST(0, current_exposure_cache - $1)
             WHERE level_id = $2
               AND id = (
                   SELECT id FROM level_policies
                   WHERE level_id = $2
                   ORDER BY effective_from DESC
                   LIMIT 1
               )
            """,
            amount, level_id,
        )

    # ── Term curve ─────────────────────────────────────────────────────────────

    async def set_term_curve(self, level_id: int, bands: list[dict]) -> None:
        """
        Recria a curva de prazo do nível.
        Cada banda: {min_term_days, max_term_days, adjustment_bps}
        """
        await self._db.execute(
            "DELETE FROM level_term_curve WHERE level_id = $1", level_id
        )
        for b in bands:
            await self._db.execute(
                """
                INSERT INTO level_term_curve (level_id, min_term_days, max_term_days, adjustment_bps)
                VALUES ($1, $2, $3, $4)
                """,
                level_id,
                int(b["min_term_days"]),
                int(b["max_term_days"]),
                int(b.get("adjustment_bps", 0)),
            )

    async def get_term_curve(self, level_id: int) -> list:
        return await self._db.fetch_all(
            """
            SELECT * FROM level_term_curve
            WHERE level_id = $1
            ORDER BY min_term_days ASC
            """,
            level_id,
        )

    # ── Limite de crédito individual ───────────────────────────────────────────

    async def get_individual_limit(self, user_id: int, level_id: int):
        return await self._db.fetch_one(
            """
            SELECT * FROM credit_limits
            WHERE borrower_type   = 'user'
              AND borrower_id     = $1
              AND lender_level_id = $2
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            user_id, level_id,
        )

    async def set_individual_limit(
        self,
        user_id: int,
        level_id: int,
        *,
        mode: str = "score_band",
        limit_amount: Decimal | None = None,
        limit_percentage: Decimal | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO credit_limits
                (borrower_type, borrower_id, lender_level_id,
                 mode, limit_amount, limit_percentage)
            VALUES ('user', $1, $2, $3, $4, $5)
            """,
            user_id, level_id, mode, limit_amount, limit_percentage,
        )

    async def resolve_effective_limit(
        self,
        user_id: int,
        level_id: int,
        user_risk_score: Decimal,
    ) -> tuple[Decimal | None, str, Decimal | None]:
        """
        Retorna (effective_limit, mode_usado, limit_percentage_usada).

        Hierarquia de resolução:
          1. credit_limits individual (fixed / percentage / score_band)
          2. default_individual_limit da level_policy
          3. Fallback: max_per_user_limit da policy
          4. Nenhuma configuração → Decimal("0") (rejeita com mensagem clara)
        """
        policy = await self.get_policy(level_id)
        max_per_user = (
            Decimal(str(policy["max_per_user_limit"]))
            if policy and policy["max_per_user_limit"] is not None
            else None
        )

        ind = await self.get_individual_limit(user_id, level_id)
        mode = ind["mode"] if ind else "score_band"

        if mode == "fixed" and ind and ind["limit_amount"] is not None:
            return Decimal(str(ind["limit_amount"])), "fixed", None

        if mode == "percentage" and ind and ind["limit_percentage"] is not None:
            pct = Decimal(str(ind["limit_percentage"]))
            if max_per_user is not None:
                return (pct * max_per_user).quantize(Decimal("0.01")), "percentage", pct
            return None, "percentage_no_max", pct

        # score_band — deriva percentual do score normalizado
        if max_per_user is not None:
            score_norm = user_risk_score / Decimal("1000")
            score_norm = max(_ZERO, min(Decimal("1"), score_norm))
            pct = score_norm
            effective = (pct * max_per_user).quantize(Decimal("0.01"))
            return effective, "score_band", pct

        # Fallback: default_individual_limit da policy
        if policy and policy.get("default_individual_limit") is not None:
            return (
                Decimal(str(policy["default_individual_limit"])),
                "policy_default",
                None,
            )

        return Decimal("0"), "unconfigured", None

    async def validate_limits(
        self,
        user_id: int,
        level_id: int,
        requested_amount: Decimal,
        active_debt_total: Decimal,
        user_risk_score: Decimal = Decimal("500"),
    ) -> tuple[bool, str, dict]:
        """
        Valida limite individual + teto do pool do nível.
        Retorna (aprovado, motivo_rejeição, contexto).
        """
        policy = await self.get_policy(level_id)
        max_per_user = (
            Decimal(str(policy["max_per_user_limit"]))
            if policy and policy["max_per_user_limit"] is not None
            else None
        )

        effective_limit, mode, pct = await self.resolve_effective_limit(
            user_id, level_id, user_risk_score
        )

        if effective_limit is not None:
            projected = active_debt_total + requested_amount
            if projected > effective_limit:
                return False, (
                    f"Limite de crédito excedido: dívidas ativas "
                    f"R$ {active_debt_total:.2f} + solicitado R$ {requested_amount:.2f} "
                    f"= R$ {projected:.2f} > limite R$ {effective_limit:.2f} "
                    f"(modo={mode}, score={user_risk_score:.0f})"
                ), {
                    "effective_limit": effective_limit, "mode": mode, "pct": pct,
                    "max_per_user_limit": max_per_user, "level_upgrade_candidate": False,
                }

        if policy:
            max_exp = Decimal(str(policy["max_aggregate_exposure"]))
            cur_exp = Decimal(str(policy["current_exposure_cache"]))
            if cur_exp + requested_amount > max_exp:
                return False, (
                    f"Teto de exposição do nível {level_id} atingido: "
                    f"exposição atual R$ {cur_exp:.2f} + solicitado R$ {requested_amount:.2f} "
                    f"> máximo R$ {max_exp:.2f}"
                ), {
                    "effective_limit": effective_limit, "mode": mode, "pct": pct,
                    "max_per_user_limit": max_per_user, "level_upgrade_candidate": False,
                }

        level_upgrade_candidate = False
        if max_per_user is not None and effective_limit is not None:
            projected = active_debt_total + requested_amount
            level_upgrade_candidate = projected >= (UPGRADE_TRIGGER_PCT * max_per_user)

        ctx = {
            "effective_limit":         effective_limit,
            "mode":                    mode,
            "pct":                     pct,
            "max_per_user_limit":      max_per_user,
            "level_upgrade_candidate": level_upgrade_candidate,
        }
        return True, "", ctx

    # ── Histórico de nível ─────────────────────────────────────────────────────

    async def transition_user_level(
        self,
        user_id: int,
        to_level: int,
        trigger_score: Decimal | None = None,
        reason: str | None = None,
        changed_by: str = "system",
    ) -> dict:
        """
        Muda o nível atual do usuário e registra no histórico.
        Retorna {ok, from_level, to_level}.
        """
        user = await self._db.fetch_one(
            "SELECT current_level FROM users WHERE id = $1", user_id
        )
        if not user:
            return {"ok": False, "error": "Usuário não encontrado"}

        from_level = user["current_level"]
        if from_level == to_level:
            return {"ok": True, "from_level": from_level, "to_level": to_level, "changed": False}

        await self._db.execute(
            "UPDATE users SET current_level = $1, updated_at = NOW() WHERE id = $2",
            to_level, user_id,
        )
        await self._db.execute(
            """
            INSERT INTO user_level_history
                (user_id, from_level, to_level, trigger_score, reason, changed_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user_id, from_level, to_level, trigger_score, reason, changed_by,
        )
        logger.info(
            "Nível do usuário %d: %d → %d (score=%s, motivo=%s)",
            user_id, from_level, to_level, trigger_score, reason,
        )
        return {"ok": True, "from_level": from_level, "to_level": to_level, "changed": True}

    async def get_level_history(self, user_id: int, limit: int = 20) -> list:
        return await self._db.fetch_all(
            """
            SELECT h.*, lf.name AS from_level_name, lt.name AS to_level_name
            FROM user_level_history h
            LEFT JOIN levels lf ON lf.id = h.from_level
            JOIN levels lt ON lt.id = h.to_level
            WHERE h.user_id = $1
            ORDER BY h.changed_at DESC
            LIMIT $2
            """,
            user_id, limit,
        )

    # ── Liquidez ────────────────────────────────────────────────────────────────

    async def get_latest_liquidity(self, level_id: int):
        return await self._db.fetch_one(
            """
            SELECT * FROM liquidity_snapshots
            WHERE level_id = $1
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            level_id,
        )

    # ── Visão consolidada do nível ─────────────────────────────────────────────

    async def get_full_profile(self, level_id: int) -> dict | None:
        level = await self.get_by_id(level_id)
        if not level:
            return None

        policy    = await self.get_policy(level_id)
        term_curve = await self.get_term_curve(level_id)
        liquidity  = await self.get_latest_liquidity(level_id)

        wallet = await self._db.fetch_one(
            "SELECT * FROM wallets WHERE owner_type = 'level' AND owner_id = $1",
            level_id,
        )

        member_count = await self._db.fetch_val(
            "SELECT COUNT(*) FROM users WHERE current_level = $1",
            level_id,
        ) or 0

        return {
            "level":          dict(level),
            "policy":         dict(policy) if policy else None,
            "term_curve":     [dict(r) for r in term_curve],
            "liquidity":      dict(liquidity) if liquidity else None,
            "member_count":   member_count,
            "wallet_balance": float(wallet["balance_cache"]) if wallet else 0.0,
        }

    # ── Histórico por usuário (alias público) ──────────────────────────────────

    async def get_user_level(self, user_id: int):
        """Retorna o nível atual do usuário com dados do nível."""
        return await self._db.fetch_one(
            """
            SELECT u.current_level, l.name, l.status, l.min_score, l.max_score
            FROM users u
            JOIN levels l ON l.id = u.current_level
            WHERE u.id = $1
            """,
            user_id,
        )

    async def get_user_level_history(self, user_id: int, limit: int = 20) -> list:
        """Alias de get_level_history — histórico de transições de nível do usuário."""
        return await self.get_level_history(user_id, limit=limit)

    # ── Sugestões de upgrade de nível ─────────────────────────────────────────

    async def list_upgrade_candidates(self, status: str = "suggested") -> list:
        """
        Lista sugestões de upgrade de nível.
        Status: 'suggested' | 'accepted' | 'rejected'
        """
        return await self._db.fetch_all(
            """
            SELECT s.*, u.name AS user_name, u.phone,
                   lf.name AS from_level_name, lt.name AS to_level_name
            FROM level_upgrade_suggestions s
            JOIN users  u  ON u.id  = s.user_id
            JOIN levels lf ON lf.id = s.from_level_id
            JOIN levels lt ON lt.id = s.to_level_id
            WHERE s.status = $1
            ORDER BY s.suggested_at DESC
            """,
            status,
        )

    async def suggest_upgrade(
        self,
        user_id: int,
        from_level_id: int,
        to_level_id: int,
        trigger_score: float | None = None,
        reason: str | None = None,
    ) -> int:
        """Registra sugestão de upgrade de nível para revisão manual."""
        return await self._db.fetch_val(
            """
            INSERT INTO level_upgrade_suggestions
                (user_id, from_level_id, to_level_id, trigger_score, reason, status)
            VALUES ($1, $2, $3, $4, $5, 'suggested')
            ON CONFLICT (user_id)
              WHERE status = 'suggested'
              DO UPDATE SET
                to_level_id   = EXCLUDED.to_level_id,
                trigger_score = EXCLUDED.trigger_score,
                reason        = EXCLUDED.reason,
                suggested_at  = NOW()
            RETURNING id
            """,
            user_id, from_level_id, to_level_id, trigger_score, reason,
        )

    async def resolve_upgrade(
        self,
        event_id: int,
        resolution: str,
        to_level_id: int | None = None,
        transition_reason: str | None = None,
    ) -> dict:
        """
        Aceita ou rejeita uma sugestão de upgrade.
        Se aceito, executa a transição de nível do usuário.
        """
        suggestion = await self._db.fetch_one(
            "SELECT * FROM level_upgrade_suggestions WHERE id = $1", event_id
        )
        if not suggestion:
            return {"ok": False, "error": "Sugestão de upgrade não encontrada"}

        if suggestion["status"] != "suggested":
            return {
                "ok": False,
                "error": f"Sugestão já resolvida com status={suggestion['status']}",
            }

        resolved_to = to_level_id or suggestion["to_level_id"]

        await self._db.execute(
            """
            UPDATE level_upgrade_suggestions
            SET status = $1, resolved_at = NOW(), resolved_to_level_id = $2
            WHERE id = $3
            """,
            resolution, resolved_to, event_id,
        )

        if resolution == "accepted":
            transition = await self.transition_user_level(
                user_id=suggestion["user_id"],
                to_level=resolved_to,
                trigger_score=suggestion.get("trigger_score"),
                reason=transition_reason or suggestion.get("reason") or "Upgrade manual aprovado",
                changed_by="admin",
            )
            return {
                "ok": True,
                "event_id": event_id,
                "user_id": suggestion["user_id"],
                "resolution": resolution,
                **transition,
            }

        return {
            "ok": True,
            "event_id": event_id,
            "user_id": suggestion["user_id"],
            "resolution": resolution,
        }

    # ── create_full ────────────────────────────────────────────────────────────

    async def create_full(self, payload: dict) -> dict:
        """
        Configura um nível completo em uma única chamada:
          - policy (taxas, spread, limites de exposição, fórmula de prazo)
          - wallet (owner_type='level')
          - term_curve (lista opcional de faixas de prazo)

        payload esperado:
        {
          "level_id": int (1–10),
          "base_borrowing_rate": float,
          "base_investment_rate": float,
          "min_spread": float,
          "max_aggregate_exposure": float,
          "max_per_user_limit": float | None,
          "spread_violation_strategy": str,
          "term_rate_formula": str,       # "bands" | "linear" | "log" | "sqrt"
          "term_rate_base_bps": float | None,
          "term_rate_scale": float | None,
          "default_individual_limit": float | None,
          "term_curve": [                 # opcional — apenas para formula="bands"
            {"min_term_days": 1, "max_term_days": 30, "adjustment_bps": 0},
          ],
        }
        """
        level_id = int(payload["level_id"])

        await self.set_policy(
            level_id=level_id,
            base_borrowing_rate=Decimal(str(payload["base_borrowing_rate"])),
            base_investment_rate=Decimal(str(payload["base_investment_rate"])),
            min_spread=Decimal(str(payload["min_spread"])),
            max_aggregate_exposure=Decimal(str(payload["max_aggregate_exposure"])),
            max_per_user_limit=(
                Decimal(str(payload["max_per_user_limit"]))
                if payload.get("max_per_user_limit") is not None else None
            ),
            spread_violation_strategy=payload.get(
                "spread_violation_strategy", "reject_investment"
            ),
            term_rate_formula=payload.get("term_rate_formula", "bands"),
            term_rate_base_bps=(
                Decimal(str(payload["term_rate_base_bps"]))
                if payload.get("term_rate_base_bps") is not None else None
            ),
            term_rate_scale=(
                Decimal(str(payload["term_rate_scale"]))
                if payload.get("term_rate_scale") is not None else None
            ),
            default_individual_limit=(
                Decimal(str(payload["default_individual_limit"]))
                if payload.get("default_individual_limit") is not None else None
            ),
        )

        await self._db.execute(
            """
            INSERT INTO wallets (owner_type, owner_id, balance_cache)
            VALUES ('level', $1, 0)
            ON CONFLICT (owner_type, owner_id) DO NOTHING
            """,
            level_id,
        )

        if payload.get("term_curve"):
            await self.set_term_curve(level_id, payload["term_curve"])

        logger.info(
            "Nível %d configurado: max_aggregate=%.2f max_per_user=%s",
            level_id,
            float(payload["max_aggregate_exposure"]),
            payload.get("max_per_user_limit"),
        )

        return await self.get_full_profile(level_id)
