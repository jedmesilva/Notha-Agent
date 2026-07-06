"""
RateEngine — calcula cotações de taxa de juros (seção 7 do documento).

Fórmula (determinística, não ML):
  final_borrowing_rate =
      base_borrowing_rate
    + risk_premium(score)
    + term_adjustment_bps / 10000
    × liquidity_multiplier(demanda / oferta)

  Constraint obrigatória:
    final_borrowing_rate - final_investment_rate >= min_spread
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger("notha.rate_engine")

_ZERO = Decimal("0")
_ONE  = Decimal("1")

# Razão demanda/oferta máxima antes de saturar o multiplicador
_LIQUIDITY_RATIO_CAP = Decimal("3")

# risk_premium é calculado a partir do score normalizado (0–1000)
# Quanto mais baixo o score, maior o prêmio de risco.
# Parâmetros ajustáveis: score 0 → premium máximo; score 1000 → premium 0.
_MAX_RISK_PREMIUM = Decimal("0.08")   # 8 pp para score = 0
_MIN_SCORE        = Decimal("0")
_MAX_SCORE        = Decimal("1000")


def _compute_risk_premium(score: Decimal) -> Decimal:
    """
    Prêmio de risco linear inversamente proporcional ao score.
    score 1000 → 0.00 (zero risco adicional)
    score    0 → MAX_RISK_PREMIUM (risco máximo)
    """
    score = max(_MIN_SCORE, min(_MAX_SCORE, score))
    normalized = (score - _MIN_SCORE) / (_MAX_SCORE - _MIN_SCORE)  # 0.0 – 1.0
    premium = _MAX_RISK_PREMIUM * (_ONE - normalized)
    return premium.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _compute_liquidity_multiplier(
    demand: Decimal, supply: Decimal
) -> Decimal:
    """
    Multiplicador de liquidez: razão demanda/oferta, com cap em _LIQUIDITY_RATIO_CAP.
    supply = 0 → multiplier = cap (liquidez crítica)
    demand/supply >= cap → multiplier = cap
    demand/supply < 1   → multiplier < 1 (oferta abundante, taxas caem)
    """
    if supply <= _ZERO:
        return _LIQUIDITY_RATIO_CAP
    ratio = demand / supply
    return min(ratio, _LIQUIDITY_RATIO_CAP).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )


async def compute_loan_quote(
    db,
    loan_request_id: int,
    group_id: int,
    term_days: int,
    user_risk_score: Decimal | None,
) -> dict:
    """
    Calcula e persiste uma cotação de taxa para a solicitação de empréstimo.

    Retorna dict com:
      base_rate, risk_premium, term_adjustment, liquidity_multiplier,
      final_rate, spread_ok, breakdown_json, quote_id
    """
    from db.repositories.rates import RateRepository
    from db.connection import get_db

    _db = db or get_db()
    rate_repo = RateRepository(_db)

    # ── Camada 1: política do grupo ───────────────────────────────────────────
    policy = await rate_repo.get_active_policy(group_id)
    if not policy:
        raise ValueError(f"Sem política de taxa configurada para o grupo {group_id}")

    base_rate        = Decimal(str(policy["base_borrowing_rate"]))
    base_invest_rate = Decimal(str(policy["base_investment_rate"]))
    min_spread       = Decimal(str(policy["min_spread"]))
    spread_strategy  = policy["spread_violation_strategy"]

    # ── Ajuste por prazo (basis points → fração) ──────────────────────────────
    adj_bps = await rate_repo.get_term_adjustment(group_id, term_days)
    term_adjustment = Decimal(str(adj_bps)) / Decimal("10000")

    # ── Camada 2: liquidez ────────────────────────────────────────────────────
    liquidity = await rate_repo.get_latest_liquidity(group_id)
    if liquidity:
        demand = Decimal(str(liquidity["total_active_loan_demand"]))
        supply = Decimal(str(liquidity["total_available_investment"]))
    else:
        demand, supply = _ONE, _ONE  # neutro se não houver snapshot

    liq_mult = _compute_liquidity_multiplier(demand, supply)

    # ── Prêmio de risco ───────────────────────────────────────────────────────
    score = user_risk_score if user_risk_score is not None else Decimal("500")
    risk_premium = _compute_risk_premium(score)

    # ── Taxa final do tomador ─────────────────────────────────────────────────
    #  final = (base + risk_premium + term_adjustment) × liquidity_multiplier
    raw_rate   = (base_rate + risk_premium + term_adjustment) * liq_mult
    final_rate = raw_rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    # ── Constraint de spread mínimo ───────────────────────────────────────────
    invest_rate = (base_invest_rate * liq_mult).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    spread = final_rate - invest_rate
    spread_ok = spread >= min_spread

    if not spread_ok and spread_strategy == "raise_borrowing_rate":
        # Eleva final_rate até garantir o spread mínimo
        final_rate = (invest_rate + min_spread).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        spread_ok = True
        logger.warning(
            "Spread violation: taxa elevada para %.6f (grupo %d)", final_rate, group_id
        )

    breakdown = {
        "base_rate":            str(base_rate),
        "risk_premium":         str(risk_premium),
        "term_adjustment":      str(term_adjustment),
        "term_adjustment_bps":  adj_bps,
        "liquidity_demand":     str(demand),
        "liquidity_supply":     str(supply),
        "liquidity_multiplier": str(liq_mult),
        "user_risk_score":      str(score),
        "final_rate":           str(final_rate),
        "investment_rate":      str(invest_rate),
        "spread":               str(spread),
        "spread_ok":            spread_ok,
    }

    # ── Persiste cotação ──────────────────────────────────────────────────────
    quote_id = await rate_repo.create_loan_quote(
        loan_request_id=loan_request_id,
        base_rate=base_rate,
        risk_premium=risk_premium,
        term_adjustment=term_adjustment,
        liquidity_multiplier=liq_mult,
        final_rate=final_rate,
        breakdown_json=breakdown,
    )

    logger.info(
        "loan_rate_quote id=%d | request=%d | final_rate=%.4f%% | spread_ok=%s",
        quote_id, loan_request_id, float(final_rate) * 100, spread_ok,
    )

    return {
        "quote_id":            quote_id,
        "base_rate":           base_rate,
        "risk_premium":        risk_premium,
        "term_adjustment":     term_adjustment,
        "liquidity_multiplier": liq_mult,
        "final_rate":          final_rate,
        "investment_rate":     invest_rate,
        "spread_ok":           spread_ok,
        "breakdown_json":      breakdown,
    }


async def compute_investment_quote(
    db, group_id: int, investment_id: int | None = None
) -> dict:
    """Cotação de taxa para investidores (seção 7, Camada 3)."""
    from db.repositories.rates import RateRepository
    from db.connection import get_db

    _db = db or get_db()
    rate_repo = RateRepository(_db)

    policy = await rate_repo.get_active_policy(group_id)
    if not policy:
        raise ValueError(f"Sem política de taxa para o grupo {group_id}")

    base_invest_rate = Decimal(str(policy["base_investment_rate"]))

    liquidity = await rate_repo.get_latest_liquidity(group_id)
    if liquidity:
        demand = Decimal(str(liquidity["total_active_loan_demand"]))
        supply = Decimal(str(liquidity["total_available_investment"]))
    else:
        demand, supply = _ONE, _ONE

    # Para investidores: oferta/demanda (inverso do tomador)
    liq_mult = _compute_liquidity_multiplier(supply, demand)
    final_rate = (base_invest_rate * liq_mult).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )

    quote_id = await rate_repo.create_investment_quote(
        investment_id=investment_id,
        base_rate=base_invest_rate,
        liquidity_multiplier=liq_mult,
        final_rate=final_rate,
    )

    return {
        "quote_id":            quote_id,
        "base_rate":           base_invest_rate,
        "liquidity_multiplier": liq_mult,
        "final_rate":          final_rate,
    }
