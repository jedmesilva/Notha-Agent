"""
NOTHA periodic background jobs — financial platform.

Jobs:
  check_overdue_installments    : marca parcelas vencidas como 'overdue' (a cada hora)
  check_expired_loan_requests   : expira solicitações pendentes > 7 dias (a cada hora)
  snapshot_liquidity            : registra snapshot de liquidez por nível (a cada 5 min)
  snapshot_liquidity_for_level  : snapshot pontual fire-and-forget para um nível
  recalculate_behavior_metrics  : recalcula métricas comportamentais (diário)
  recalculate_risk_scores       : recalcula scores de risco (diário)
  recalculate_location_metrics  : recalcula location_market_metrics (a cada 4h)
  reconcile_wallet_caches       : reconcilia balance_cache de todas as wallets (diário)
  expire_opportunities          : expira oportunidades abertas fora do prazo (a cada hora)
  distribute_investment_payouts : processa payouts de investimento vencidos (a cada minuto)
  recalculate_investor_metrics  : recalcula métricas de investidores (a cada hora)
  expire_investment_offers      : expira ofertas de investimento vencidas (a cada 5 min)
"""
import asyncio
import logging
from datetime import datetime, timezone

from db.connection import get_db

logger = logging.getLogger("notha.jobs")


# ── Jobs existentes ───────────────────────────────────────────────────────────

async def check_overdue_installments() -> None:
    """Marca parcelas cujo due_date passou e status ainda é 'pending'."""
    db = get_db()
    if not db:
        return
    try:
        result = await db.execute("""
            UPDATE debt_installments
               SET status   = 'overdue'
             WHERE status   = 'pending'
               AND due_date < CURRENT_DATE
        """)
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("check_overdue_installments: %d parcela(s) marcada(s) como overdue.", count)
    except Exception as e:
        logger.error("Error in check_overdue_installments: %s", e)


async def check_expired_loan_requests() -> None:
    """Expira solicitações de empréstimo pendentes há mais de 7 dias."""
    db = get_db()
    if not db:
        return
    try:
        result = await db.execute("""
            UPDATE loan_requests
               SET status       = 'expired',
                   decided_at   = NOW(),
                   decided_by   = 'system'
             WHERE status       = 'pending'
               AND requested_at < NOW() - INTERVAL '7 days'
        """)
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("check_expired_loan_requests: %d solicitação(ões) expirada(s).", count)
    except Exception as e:
        logger.error("Error in check_expired_loan_requests: %s", e)


async def snapshot_liquidity() -> None:
    """Registra um snapshot de liquidez para cada nível ativo."""
    db = get_db()
    if not db:
        return
    try:
        await db.execute("""
            INSERT INTO liquidity_snapshots
                        (level_id, total_available_investment, total_active_loan_demand, captured_at)
            SELECT
                lv.id,
                COALESCE((
                    SELECT SUM(wt.amount)
                    FROM   wallet_transactions wt
                    JOIN   wallets w ON w.id = wt.wallet_id
                    WHERE  w.owner_type = 'level' AND w.owner_id = lv.id
                ), 0),
                COALESCE((
                    SELECT SUM(d.principal)
                    FROM   debts d
                    JOIN   loan_requests lr ON lr.id = d.loan_request_id
                    WHERE  lr.level_id = lv.id
                      AND  d.status   = 'active'
                ), 0),
                NOW()
            FROM levels lv
            WHERE lv.status = 'active'
        """)
        logger.info("snapshot_liquidity: snapshot de liquidez registrado para todos os níveis.")
    except Exception as e:
        logger.error("Error in snapshot_liquidity: %s", e)


async def snapshot_liquidity_for_level(db, level_id: int) -> None:
    """
    Snapshot pontual de liquidez para um nível específico.

    Chamado de forma fire-and-forget via asyncio.create_task() pelos engines
    logo após qualquer evento que mude a liquidez do nível (desembolso de
    empréstimo, aporte de investimento, pagamento recebido). Isso torna o
    liquidity_multiplier das cotações praticamente em tempo real, sem depender
    exclusivamente do job periódico de 5 min.
    """
    if not db:
        return
    try:
        await db.execute(
            """
            INSERT INTO liquidity_snapshots
                        (level_id, total_available_investment, total_active_loan_demand, captured_at)
            SELECT
                lv.id,
                COALESCE((
                    SELECT SUM(wt.amount)
                    FROM   wallet_transactions wt
                    JOIN   wallets w ON w.id = wt.wallet_id
                    WHERE  w.owner_type = 'level' AND w.owner_id = lv.id
                ), 0),
                COALESCE((
                    SELECT SUM(d.principal)
                    FROM   debts d
                    JOIN   loan_requests lr ON lr.id = d.loan_request_id
                    WHERE  lr.level_id = lv.id
                      AND  d.status   = 'active'
                ), 0),
                NOW()
            FROM levels lv
            WHERE lv.id = $1
            """,
            level_id,
        )
        logger.debug("snapshot_liquidity_for_level: nível %d atualizado.", level_id)
    except Exception as e:
        logger.error("snapshot_liquidity_for_level level_id=%d: %s", level_id, e)


# ── Jobs — Scoring ────────────────────────────────────────────────────────────

async def recalculate_behavior_metrics() -> None:
    """
    Recalcula user_behavior_metrics para todos os usuários com atividade financeira.
    Roda diariamente ou após eventos-chave (pagamento, inadimplência).
    """
    db = get_db()
    if not db:
        return
    try:
        from engine.scoring_engine import recalculate_behavior_metrics as _recalc

        rows = await db.fetch_all(
            "SELECT DISTINCT user_id FROM loan_requests ORDER BY user_id"
        )
        count = 0
        for row in rows:
            try:
                await _recalc(db, row["user_id"])
                count += 1
            except Exception as e:
                logger.error("recalculate_behavior_metrics user_id=%d: %s", row["user_id"], e)

        logger.info("recalculate_behavior_metrics: %d usuário(s) processado(s).", count)
    except Exception as e:
        logger.error("Error in recalculate_behavior_metrics job: %s", e)


async def recalculate_risk_scores() -> None:
    """
    Recalcula user_risk_scores para todos os usuários com score expirado ou ausente.
    Roda diariamente.
    """
    db = get_db()
    if not db:
        return
    try:
        from engine.scoring_engine import recalculate_risk_score
        from db.repositories.scoring import ScoringRepository

        scoring_repo = ScoringRepository(db)

        rows = await db.fetch_all(
            "SELECT DISTINCT user_id FROM loan_requests ORDER BY user_id"
        )
        count = 0
        for row in rows:
            uid = row["user_id"]
            try:
                if not await scoring_repo.is_score_valid(uid):
                    await recalculate_risk_score(db, uid)
                    count += 1
            except Exception as e:
                logger.error("recalculate_risk_scores user_id=%d: %s", uid, e)

        logger.info("recalculate_risk_scores: %d usuário(s) com score recalculado.", count)
    except Exception as e:
        logger.error("Error in recalculate_risk_scores job: %s", e)


async def recalculate_location_metrics() -> None:
    """
    Recalcula location_market_metrics para todos os geohashes ativos.
    Roda a cada 4 horas.
    """
    db = get_db()
    if not db:
        return
    try:
        from engine.scoring_engine import recalculate_location_market_metrics

        rows = await db.fetch_all(
            "SELECT DISTINCT geohash FROM user_locations WHERE geohash IS NOT NULL"
        )
        count = 0
        for row in rows:
            try:
                await recalculate_location_market_metrics(db, row["geohash"])
                count += 1
            except Exception as e:
                logger.error("recalculate_location_metrics geohash=%s: %s", row["geohash"], e)

        logger.info("recalculate_location_metrics: %d geohash(es) processado(s).", count)
    except Exception as e:
        logger.error("Error in recalculate_location_metrics job: %s", e)


# ── Job — Reconciliação de wallets ────────────────────────────────────────────

async def reconcile_wallet_caches() -> None:
    """
    Reconcilia balance_cache de todas as wallets com a soma real das transações.
    Detecta e corrige drifts de cache. Roda diariamente.
    """
    db = get_db()
    if not db:
        return
    try:
        from db.repositories.wallets import WalletRepository

        wallet_repo = WalletRepository(db)
        wallets = await db.fetch_all("SELECT id FROM wallets ORDER BY id")
        drifts = 0
        for w in wallets:
            try:
                true_bal = await wallet_repo.true_balance(w["id"])
                row = await db.fetch_one(
                    "SELECT balance_cache FROM wallets WHERE id = $1", w["id"]
                )
                if row and abs(float(row["balance_cache"]) - float(true_bal)) > 0.001:
                    await wallet_repo.reconcile_cache(w["id"])
                    drifts += 1
            except Exception as e:
                logger.error("reconcile_wallet_caches wallet_id=%d: %s", w["id"], e)

        if drifts:
            logger.warning("reconcile_wallet_caches: %d wallet(s) com cache corrigido.", drifts)
        else:
            logger.info("reconcile_wallet_caches: todos os caches OK.")
    except Exception as e:
        logger.error("Error in reconcile_wallet_caches job: %s", e)


# ── Runner genérico ───────────────────────────────────────────────────────────

async def _run_job(name: str, coro_fn, interval_seconds: int) -> None:
    while True:
        try:
            await coro_fn()
        except Exception as e:
            logger.error("Job '%s' falhou inesperadamente: %s", name, e)
        await asyncio.sleep(interval_seconds)


async def expire_opportunities() -> None:
    """Marca investment_opportunities abertas que passaram do prazo como 'expired'."""
    db = get_db()
    if not db:
        return
    try:
        from db.repositories.opportunities import OpportunityRepository
        count = await OpportunityRepository(db).expire_stale()
        if count:
            logger.info("expire_opportunities: %d oportunidade(s) expirada(s).", count)
    except Exception as e:
        logger.error("Error in expire_opportunities: %s", e)


async def recalculate_investor_metrics() -> None:
    """
    Recalcula métricas históricas de todos os perfis de investidor ativos.
    """
    db = get_db()
    if not db:
        return
    try:
        from db.repositories.investor_profiles import InvestorProfileRepository
        profile_repo = InvestorProfileRepository(db)
        user_ids = await profile_repo.list_all_user_ids()
        updated = 0
        for uid in user_ids:
            row = await db.fetch_one(
                """
                SELECT
                    AVG(amount_invested)::NUMERIC(15,2)                     AS avg_amount,
                    AVG(
                        EXTRACT(EPOCH FROM (
                            COALESCE(maturity_at, NOW()) - created_at
                        )) / 86400
                    )::INT                                                   AS avg_days,
                    COALESCE(SUM(amount_invested), 0)::NUMERIC(15,2)        AS total_lifetime,
                    COUNT(*) FILTER (WHERE status = 'active')::INT          AS active_count
                FROM investments
                WHERE investor_user_id = $1
                """,
                uid,
            )
            if row:
                await profile_repo.update_metrics(
                    user_id=uid,
                    avg_investment_amount=row["avg_amount"],
                    avg_term_days=row["avg_days"],
                    total_invested_lifetime=row["total_lifetime"] or 0,
                    active_investment_count=row["active_count"] or 0,
                )
                updated += 1
        if updated:
            logger.info("recalculate_investor_metrics: %d perfil(is) atualizado(s).", updated)
    except Exception as e:
        logger.error("Error in recalculate_investor_metrics: %s", e)


async def expire_investment_offers() -> None:
    """
    Marca como 'expired' as ofertas de investimento cujo expires_at já passou
    e ainda estão com status 'pending'.
    """
    db = get_db()
    if not db:
        return
    try:
        result = await db.execute(
            """
            UPDATE investment_offers
               SET status       = 'expired',
                   responded_at = NOW()
             WHERE status    = 'pending'
               AND expires_at < NOW()
            """
        )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("expire_investment_offers: %d oferta(s) expirada(s).", count)
    except Exception as e:
        logger.error("Error in expire_investment_offers: %s", e)


async def distribute_investment_payouts() -> None:
    """Processa rendimentos de investimento com scheduled_date <= hoje."""
    db = get_db()
    if not db:
        return
    try:
        from engine.investment_engine import distribute_payouts
        result = await distribute_payouts(db)
        if result["paid_count"] or result["errors"]:
            logger.info(
                "distribute_investment_payouts: %d pago(s), R$%.2f distribuído(s), %d erro(s).",
                result["paid_count"], float(result["total_distributed"]), len(result["errors"]),
            )
    except Exception as e:
        logger.error("Error in distribute_investment_payouts: %s", e)


async def start_all_jobs() -> None:
    """Inicia todos os jobs periódicos como asyncio tasks."""
    logger.info("Iniciando jobs periódicos da NOTHA...")

    asyncio.create_task(_run_job("check_overdue_installments",  check_overdue_installments,  3600))
    asyncio.create_task(_run_job("check_expired_loan_requests", check_expired_loan_requests, 3600))
    asyncio.create_task(_run_job("snapshot_liquidity",          snapshot_liquidity,           300))

    asyncio.create_task(_run_job("recalculate_behavior_metrics", recalculate_behavior_metrics, 86400))
    asyncio.create_task(_run_job("recalculate_risk_scores",      recalculate_risk_scores,      86400))
    asyncio.create_task(_run_job("recalculate_location_metrics", recalculate_location_metrics, 14400))
    asyncio.create_task(_run_job("reconcile_wallet_caches",      reconcile_wallet_caches,      86400))

    asyncio.create_task(_run_job("expire_opportunities",          expire_opportunities,          3600))
    asyncio.create_task(_run_job("distribute_investment_payouts", distribute_investment_payouts,   60))
    asyncio.create_task(_run_job("recalculate_investor_metrics",  recalculate_investor_metrics,  3600))
    asyncio.create_task(_run_job("expire_investment_offers",      expire_investment_offers,       300))

    logger.info("Jobs periódicos iniciados: 11 jobs ativos.")
