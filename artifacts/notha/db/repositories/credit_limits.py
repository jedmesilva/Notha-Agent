"""
CreditLimitRepository — credit_limits, group_pool_limits, score_limit_bands.

Três modos de limite individual:
  fixed      — limit_amount fixo (manual, retrocompatível)
  percentage — limit_percentage × group_pool_limits.max_per_user_limit
  score_band — percentual dinâmico via score_limit_bands por faixa de score

Limite agregado (group_pool_limits) permanece imutável.

Fluxo de resolução no engine:
  1. Busca score atual do usuário
  2. Resolve effective_limit via modo configurado
  3. Valida: active_debt + requested <= effective_limit
  4. Valida: pool.current_exposure + requested <= pool.max_aggregate_exposure
  5. Pós-aprovação: verifica se usuário é candidato a upgrade de grupo
"""
import logging
from decimal import Decimal
from db.connection import DB

logger = logging.getLogger("notha.credit_limits")

_ZERO = Decimal("0")
_ONE  = Decimal("1")

# Percentual do teto que dispara sugestão de upgrade (ex: 95%)
UPGRADE_TRIGGER_PCT = Decimal("0.95")


class CreditLimitRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── credit_limits (individual) ────────────────────────────────────────────

    async def get_individual_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        lender_group_id: int,
    ):
        return await self._db.fetch_one(
            """
            SELECT * FROM credit_limits
            WHERE borrower_type   = $1
              AND borrower_id     = $2
              AND lender_group_id = $3
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            borrower_type, borrower_id, lender_group_id,
        )

    async def set_individual_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        lender_group_id: int,
        *,
        mode: str = "fixed",
        limit_amount: Decimal | None = None,
        limit_percentage: Decimal | None = None,
    ) -> None:
        """
        Insere um novo registro de limite individual.
        mode='fixed'      → limit_amount obrigatório
        mode='percentage' → limit_percentage obrigatório
        mode='score_band' → nenhum valor fixo; usa score_limit_bands dinamicamente
        """
        await self._db.execute(
            """
            INSERT INTO credit_limits
                (borrower_type, borrower_id, lender_group_id,
                 mode, limit_amount, limit_percentage)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            borrower_type, borrower_id, lender_group_id,
            mode, limit_amount, limit_percentage,
        )

    # ── score_limit_bands ────────────────────────────────────────────────────

    async def get_band_for_score(self, group_id: int, score: Decimal):
        """Retorna a faixa de score correspondente ao score do usuário no grupo."""
        return await self._db.fetch_one(
            """
            SELECT * FROM score_limit_bands
            WHERE group_id  = $1
              AND min_score <= $2
              AND max_score >  $2
            ORDER BY min_score DESC
            LIMIT 1
            """,
            group_id, score,
        )

    async def list_bands(self, group_id: int) -> list:
        """Lista todas as faixas do grupo em ordem crescente de score."""
        return await self._db.fetch_all(
            """
            SELECT * FROM score_limit_bands
            WHERE group_id = $1
            ORDER BY min_score ASC
            """,
            group_id,
        )

    async def upsert_bands(self, group_id: int, bands: list[dict]) -> None:
        """
        Recria as faixas de score de um grupo.
        Cada item: {min_score, max_score, limit_percentage, label?}
        """
        await self._db.execute(
            "DELETE FROM score_limit_bands WHERE group_id = $1",
            group_id,
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

    # ── group_pool_limits (agregado) ──────────────────────────────────────────

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

    async def increment_exposure(self, group_id: int, amount: Decimal) -> None:
        await self._db.execute(
            """
            UPDATE group_pool_limits
               SET current_exposure_cache = current_exposure_cache + $1
             WHERE group_id = $2
               AND id = (
                   SELECT id FROM group_pool_limits
                   WHERE group_id = $2
                   ORDER BY effective_from DESC
                   LIMIT 1
               )
            """,
            amount, group_id,
        )

    async def decrement_exposure(self, group_id: int, amount: Decimal) -> None:
        await self._db.execute(
            """
            UPDATE group_pool_limits
               SET current_exposure_cache = GREATEST(0, current_exposure_cache - $1)
             WHERE group_id = $2
               AND id = (
                   SELECT id FROM group_pool_limits
                   WHERE group_id = $2
                   ORDER BY effective_from DESC
                   LIMIT 1
               )
            """,
            amount, group_id,
        )

    # ── resolução do limite efetivo ───────────────────────────────────────────

    async def resolve_effective_limit(
        self,
        borrower_type: str,
        borrower_id: int,
        group_id: int,
        user_risk_score: Decimal,
    ) -> tuple[Decimal | None, str, Decimal | None]:
        """
        Retorna (effective_limit, mode_usado, limit_percentage_usada).

        Hierarquia de resolução:
          1. Se existe credit_limits com mode='score_band' → usa score_limit_bands
          2. Se existe credit_limits com mode='percentage' → usa limit_percentage × max_per_user_limit
          3. Se existe credit_limits com mode='fixed'      → usa limit_amount
          4. Senão → fallback: score_band no grupo (se configurado)
          5. Nada configurado → None (sem limite individual — só pool valida)
        """
        pool = await self.get_pool_limit(group_id)
        max_per_user = (
            Decimal(str(pool["max_per_user_limit"]))
            if pool and pool["max_per_user_limit"] is not None
            else None
        )

        ind = await self.get_individual_limit(borrower_type, borrower_id, group_id)
        mode = ind["mode"] if ind else "score_band"

        if mode == "fixed" and ind and ind["limit_amount"] is not None:
            return Decimal(str(ind["limit_amount"])), "fixed", None

        if mode == "percentage" and ind and ind["limit_percentage"] is not None:
            pct = Decimal(str(ind["limit_percentage"]))
            if max_per_user is not None:
                return (pct * max_per_user).quantize(Decimal("0.01")), "percentage", pct
            return None, "percentage_no_max", pct

        # score_band (padrão para novos usuários ou quando mode='score_band')
        band = await self.get_band_for_score(group_id, user_risk_score)
        if band and max_per_user is not None:
            pct = Decimal(str(band["limit_percentage"]))
            effective = (pct * max_per_user).quantize(Decimal("0.01"))
            return effective, "score_band", pct

        return None, "unconfigured", None

    # ── validação completa ────────────────────────────────────────────────────

    async def validate_limits(
        self,
        borrower_type: str,
        borrower_id: int,
        group_id: int,
        requested_amount: Decimal,
        active_debt_total: Decimal,
        user_risk_score: Decimal = Decimal("500"),
    ) -> tuple[bool, str, dict]:
        """
        Valida limite individual (score-band/percentage/fixed) + teto do pool.
        Retorna (aprovado, motivo_rejeição, contexto).
        contexto inclui: effective_limit, mode, pct, max_per_user_limit, upgrade_candidate.
        """
        pool = await self.get_pool_limit(group_id)
        max_per_user = (
            Decimal(str(pool["max_per_user_limit"]))
            if pool and pool["max_per_user_limit"] is not None
            else None
        )

        # 1. Limite individual
        effective_limit, mode, pct = await self.resolve_effective_limit(
            borrower_type, borrower_id, group_id, user_risk_score
        )

        if effective_limit is not None:
            projected = active_debt_total + requested_amount
            if projected > effective_limit:
                return False, (
                    f"Limite de crédito excedido: dívidas ativas "
                    f"R$ {active_debt_total:.2f} + solicitado R$ {requested_amount:.2f} "
                    f"= R$ {projected:.2f} > limite R$ {effective_limit:.2f} "
                    f"(modo={mode}, score={user_risk_score:.0f})"
                ), {"effective_limit": effective_limit, "mode": mode, "pct": pct,
                    "max_per_user_limit": max_per_user, "upgrade_candidate": False}

        # 2. Teto do pool
        if pool:
            max_exp = Decimal(str(pool["max_aggregate_exposure"]))
            cur_exp = Decimal(str(pool["current_exposure_cache"]))
            if cur_exp + requested_amount > max_exp:
                return False, (
                    f"Teto de exposição do grupo atingido: exposição atual "
                    f"R$ {cur_exp:.2f} + solicitado R$ {requested_amount:.2f} "
                    f"> máximo R$ {max_exp:.2f}"
                ), {"effective_limit": effective_limit, "mode": mode, "pct": pct,
                    "max_per_user_limit": max_per_user, "upgrade_candidate": False}

        # 3. Verifica candidatura a upgrade
        upgrade_candidate = False
        if max_per_user is not None and effective_limit is not None:
            projected = active_debt_total + requested_amount
            upgrade_candidate = projected >= (UPGRADE_TRIGGER_PCT * max_per_user)

        ctx = {
            "effective_limit":    effective_limit,
            "mode":               mode,
            "pct":                pct,
            "max_per_user_limit": max_per_user,
            "upgrade_candidate":  upgrade_candidate,
        }
        return True, "", ctx

    # ── upgrade events ─────────────────────────────────────────────────────────

    async def record_upgrade_suggestion(
        self,
        user_id: int,
        from_group_id: int,
        trigger_score: Decimal,
        trigger_pct: Decimal | None,
        to_group_id: int | None = None,
    ) -> int:
        """Registra sugestão de upgrade de grupo para o usuário."""
        return await self._db.fetch_val(
            """
            INSERT INTO group_upgrade_events
                (user_id, from_group_id, to_group_id, trigger_score, trigger_pct)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            user_id, from_group_id, to_group_id, trigger_score, trigger_pct,
        )

    async def has_pending_upgrade(self, user_id: int, from_group_id: int) -> bool:
        """Verifica se já existe sugestão de upgrade pendente para evitar duplicar."""
        row = await self._db.fetch_one(
            """
            SELECT id FROM group_upgrade_events
            WHERE user_id       = $1
              AND from_group_id = $2
              AND status        = 'suggested'
            LIMIT 1
            """,
            user_id, from_group_id,
        )
        return row is not None
