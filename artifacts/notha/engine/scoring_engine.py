"""
ScoringEngine — engine de scoring multi-fator (seção 8 do documento).

Sub-camadas:
  8.1 Feature Store comportamental (agregações sobre debts/payments)
  8.2 Contexto geográfico (location_risk_events + location_market_metrics)
  8.3 Scorecard ponderado versionado (risk_score_models + risk_score_weights)

Tudo determinístico — não é ML. Os pesos ficam em risk_score_weights.
Trocar por ML no futuro = só mudar a fonte dos pesos; a interface permanece igual.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger("notha.scoring_engine")

_ZERO = Decimal("0")
_ONE  = Decimal("1")


# ── 8.1 Feature Store comportamental ─────────────────────────────────────────

async def recalculate_behavior_metrics(db, user_id: int) -> dict:
    """
    Recalcula user_behavior_metrics via COUNT/SUM/AVG sobre debts, payments,
    payment_allocations. Persiste o resultado. Retorna o dict calculado.
    """
    from db.repositories.scoring import ScoringRepository

    scoring_repo = ScoringRepository(db)

    # Total emprestado e pago
    row_amounts = await db.fetch_one(
        """
        SELECT
            COALESCE(SUM(d.principal), 0)  AS total_borrowed,
            COALESCE((
                SELECT SUM(p.amount_paid)
                FROM payments p
                JOIN debts d2 ON d2.id = p.debt_id
                JOIN loan_requests lr2 ON lr2.id = d2.loan_request_id
                WHERE lr2.user_id = $1
            ), 0) AS total_repaid
        FROM debts d
        JOIN loan_requests lr ON lr.id = d.loan_request_id
        WHERE lr.user_id = $1
        """,
        user_id,
    )

    # Frequência de solicitações nos últimos 90 dias
    freq_90d = await db.fetch_val(
        """
        SELECT COUNT(*) FROM loan_requests
        WHERE user_id = $1
          AND requested_at >= NOW() - INTERVAL '90 days'
        """,
        user_id,
    ) or 0

    # Pagamentos: pontualidade (parcelas pagas na data vs. total pago)
    pay_stats = await db.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (WHERE di.status = 'paid')                    AS paid_count,
            COUNT(*) FILTER (WHERE di.status IN ('overdue','defaulted'))   AS late_count,
            COUNT(*) FILTER (WHERE di.status = 'paid'
                               AND di.due_date >= CURRENT_DATE
                               AND di.remaining_amount = 0)                AS on_time_count
        FROM debt_installments di
        JOIN debts d ON d.id = di.debt_id
        JOIN loan_requests lr ON lr.id = d.loan_request_id
        WHERE lr.user_id = $1
        """,
        user_id,
    )

    paid_count   = int(pay_stats["paid_count"] or 0)   if pay_stats else 0
    late_count   = int(pay_stats["late_count"] or 0)   if pay_stats else 0
    on_time      = int(pay_stats["on_time_count"] or 0) if pay_stats else 0

    total_closed = paid_count + late_count
    payment_frequency_score = (
        Decimal(str(on_time)) / Decimal(str(total_closed))
        if total_closed > 0 else _ZERO
    )

    # Inadimplências (dívidas com status 'defaulted')
    defaults_count = await db.fetch_val(
        """
        SELECT COUNT(*) FROM debts d
        JOIN loan_requests lr ON lr.id = d.loan_request_id
        WHERE lr.user_id = $1 AND d.status = 'defaulted'
        """,
        user_id,
    ) or 0

    # Total investido e frequência de investimentos nos últimos 90 dias
    inv_row = await db.fetch_one(
        """
        SELECT
            COALESCE(SUM(i.amount_invested), 0) AS total_invested,
            COUNT(*) FILTER (
                WHERE i.invested_at >= NOW() - INTERVAL '90 days'
            ) AS inv_freq_90d
        FROM investments i
        WHERE i.investor_user_id = $1
          AND i.status IN ('active', 'matured')
        """,
        user_id,
    )
    total_invested_amount    = Decimal(str(inv_row["total_invested"]  if inv_row else 0))
    investment_frequency_90d = int(inv_row["inv_freq_90d"]            if inv_row else 0)

    metrics = {
        "total_borrowed_amount":      Decimal(str(row_amounts["total_borrowed"] if row_amounts else 0)),
        "total_repaid_amount":        Decimal(str(row_amounts["total_repaid"]   if row_amounts else 0)),
        "loan_request_frequency_90d": int(freq_90d),
        "payment_frequency_score":    payment_frequency_score.quantize(Decimal("0.0001")),
        "late_payments_count":        int(late_count),
        "defaults_count":             int(defaults_count),
        "total_invested_amount":      total_invested_amount,
        "investment_frequency_90d":   investment_frequency_90d,
    }

    await scoring_repo.upsert_behavior_metrics(user_id, metrics)
    logger.info("user_behavior_metrics recalculado para user_id=%d", user_id)
    return metrics


# ── 8.2 Contexto geográfico ───────────────────────────────────────────────────

async def recalculate_location_market_metrics(db, geohash: str) -> None:
    """
    Recalcula location_market_metrics para um geohash específico via agregação
    sobre loan_requests e wallets dos usuários nessa localização.
    """
    from db.repositories.scoring import ScoringRepository

    scoring_repo = ScoringRepository(db)

    row = await db.fetch_one(
        """
        SELECT
            COALESCE(SUM(lr.requested_amount) FILTER (WHERE lr.status = 'pending'), 0)
                AS active_demand,
            COALESCE(AVG(lr.requested_amount), 0)
                AS avg_requested,
            COUNT(DISTINCT ul.user_id) FILTER (WHERE lr.status = 'pending')
                AS investor_count,
            COALESCE(SUM(w.balance_cache) FILTER (WHERE w.owner_type = 'user'), 0)
                AS available_investment
        FROM user_locations ul
        JOIN loan_requests lr ON lr.user_id = ul.user_id
        LEFT JOIN wallets w ON w.owner_type = 'user' AND w.owner_id = ul.user_id
        WHERE ul.geohash = $1
        """,
        geohash,
    )

    if not row:
        return

    metrics = {
        "active_loan_demand_local":   Decimal(str(row["active_demand"] or 0)),
        "avg_requested_amount_local": Decimal(str(row["avg_requested"] or 0)),
        "active_investors_count":     int(row["investor_count"] or 0),
        "available_investment_local": Decimal(str(row["available_investment"] or 0)),
    }
    await scoring_repo.upsert_market_metrics(geohash, metrics)
    logger.info("location_market_metrics recalculado para geohash=%s", geohash)


# ── 8.3 Scorecard ponderado ───────────────────────────────────────────────────

def _normalize(value: float, min_val: float, max_val: float) -> float:
    """Normaliza um valor para [0, 1] dado min/max esperados."""
    if max_val == min_val:
        return 0.0
    return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))


async def recalculate_risk_score(db, user_id: int) -> Decimal:
    """
    Calcula user_risk_scores usando o modelo ativo (scorecard ponderado).

    score = Σ (factor_value_normalizado × weight)

    Fatores suportados (mapeados abaixo para campos de behavior_metrics):
      - payment_frequency_score      (8.1, peso positivo)
      - defaults_count               (8.1, peso negativo)
      - loan_request_frequency_90d   (8.1, pode ser positivo ou negativo)
      - investment_frequency_90d     (8.1, peso positivo — confiança na plataforma)
      - location_severity            (8.2, peso negativo — incidentes recentes)
      - local_liquidity_pressure     (8.2, pressão demanda/oferta local)

    Retorna o score calculado (escala 0–1000).
    """
    from db.repositories.scoring import ScoringRepository

    scoring_repo = ScoringRepository(db)

    model = await scoring_repo.get_active_model()
    if not model:
        logger.warning("Nenhum modelo de scoring ativo — score padrão 500")
        return Decimal("500")

    model_id = model["id"]
    weights_rows = await scoring_repo.get_weights(model_id)
    if not weights_rows:
        logger.warning("Modelo %d sem pesos configurados — score padrão 500", model_id)
        return Decimal("500")

    weights: dict[str, float] = {
        row["factor_name"]: float(row["weight"]) for row in weights_rows
    }

    # Carrega métricas comportamentais (recalcula se necessário)
    behavior = await scoring_repo.get_behavior_metrics(user_id)
    if not behavior:
        behavior_dict = await recalculate_behavior_metrics(db, user_id)
    else:
        behavior_dict = dict(behavior)

    # Contexto geográfico
    location = await scoring_repo.get_user_location(user_id)
    geohash  = location["geohash"] if location else None

    max_severity = 0
    local_pressure = 1.0  # neutro
    if geohash:
        risk_events = await scoring_repo.get_recent_risk_events(geohash, days=30)
        if risk_events:
            max_severity = max(int(e["severity"]) for e in risk_events)
        market = await scoring_repo.get_market_metrics(geohash)
        if market:
            demand = float(market["active_loan_demand_local"] or 0)
            supply = float(market["available_investment_local"] or 1)
            local_pressure = demand / supply if supply > 0 else 3.0

    # Mapeamento de fatores para valores normalizados [0, 1]
    factor_values: dict[str, float] = {
        # Proporção de pagamentos em dia: 0 (ruim) → 1 (perfeito)
        "payment_frequency_score": float(behavior_dict.get("payment_frequency_score", 0)),
        # Inadimplências: normaliza 0→0 defaults (bom) / 5+ → 1 (muito ruim)
        "defaults_count": _normalize(
            float(behavior_dict.get("defaults_count", 0)), 0, 5
        ),
        # Frequência de solicitações 90d: normaliza 0→0 / 10+ → 1
        "loan_request_frequency_90d": _normalize(
            float(behavior_dict.get("loan_request_frequency_90d", 0)), 0, 10
        ),
        # Frequência de investimento: 0→0 / 12+ → 1 (bom, confiança)
        "investment_frequency_90d": _normalize(
            float(behavior_dict.get("investment_frequency_90d", 0)), 0, 12
        ),
        # Severidade de eventos: 0→0 / 5 → 1 (muito negativo)
        "location_severity": _normalize(float(max_severity), 0, 5),
        # Pressão de liquidez local: 0→0 / 3+ → 1 (negativo)
        "local_liquidity_pressure": _normalize(float(local_pressure), 0, 3),
    }

    # Scorecard: soma ponderada
    raw_score = 0.0
    factors_snapshot: dict = {}
    for factor_name, weight in weights.items():
        value = factor_values.get(factor_name, 0.0)
        contribution = value * weight
        raw_score += contribution
        factors_snapshot[factor_name] = {
            "value":        round(value, 4),
            "weight":       round(weight, 4),
            "contribution": round(contribution, 4),
        }

    # Normaliza para escala 0–1000
    # raw_score pode ser negativo (pesos negativos em fatores ruins)
    # Clipa para [0, 1] antes de multiplicar por 1000
    score_normalized = max(0.0, min(1.0, raw_score))
    final_score = Decimal(str(round(score_normalized * 1000, 2)))

    factors_snapshot["_meta"] = {
        "raw_score": round(raw_score, 6),
        "score_normalized": round(score_normalized, 6),
        "model_version": model["version"],
    }

    await scoring_repo.insert_score(
        user_id=user_id,
        model_id=model_id,
        score=final_score,
        factors_json=factors_snapshot,
    )

    logger.info("Risk score calculado: user_id=%d score=%.1f model=%s",
                user_id, float(final_score), model["version"])
    return final_score


async def get_or_compute_score(db, user_id: int) -> Decimal:
    """
    Retorna o score válido existente ou recalcula se expirado/ausente.
    Usado no fluxo síncrono de cotação (seção 11 do documento).
    """
    from db.repositories.scoring import ScoringRepository

    scoring_repo = ScoringRepository(db)
    if await scoring_repo.is_score_valid(user_id):
        row = await scoring_repo.get_latest_score(user_id)
        return Decimal(str(row["score"]))
    return await recalculate_risk_score(db, user_id)
