"""
NOTHA periodic background jobs — financial platform.

Jobs:
  check_overdue_installments    : marca parcelas P2P vencidas como 'overdue' (a cada hora)
  snapshot_liquidity            : registra snapshot de liquidez por nível (a cada 5 min)
  snapshot_liquidity_for_level  : snapshot pontual fire-and-forget para um nível
  recalculate_behavior_metrics  : recalcula métricas comportamentais (diário)
  recalculate_risk_scores       : recalcula scores de risco (diário)
  recalculate_location_metrics  : recalcula location_market_metrics (a cada 4h)
  reconcile_wallet_caches       : reconcilia balance_cache de todas as wallets (diário)
  recalculate_investor_metrics  : recalcula métricas de investidores P2P (a cada hora)
  expire_investment_offers      : expira ofertas de investimento vencidas (a cada 5 min)
  expire_stale_p2p_orders       : expira capture_orders P2P sem quórum após deadline (a cada hora)
"""
import asyncio
import logging
from datetime import datetime, timezone

from db.connection import get_db

logger = logging.getLogger("notha.jobs")


# ── Jobs — Parcelas P2P ───────────────────────────────────────────────────────

async def check_overdue_installments() -> None:
    """
    Marca parcelas de instrumentos de crédito P2P cujo due_date já passou
    e status ainda é 'pending' como 'overdue'.
    """
    db = get_db()
    if not db:
        return
    try:
        result = await db.execute("""
            UPDATE credit_instrument_installments
               SET status   = 'overdue'
             WHERE status   = 'pending'
               AND due_date < CURRENT_DATE
        """)
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info(
                "check_overdue_installments: %d parcela(s) P2P marcada(s) como overdue.", count
            )
    except Exception as e:
        logger.error("Error in check_overdue_installments: %s", e)


# ── Jobs — Liquidez ───────────────────────────────────────────────────────────

async def snapshot_liquidity() -> None:
    """
    Registra um snapshot de liquidez para cada nível ativo.
    Demanda ativa = total de capture_requests em captação.
    Oferta disponível = saldo da wallet do nível.
    """
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
                    SELECT SUM(cr.requested_amount)
                    FROM   capture_requests cr
                    WHERE  cr.level_id = lv.id
                      AND  cr.status   = 'in_capture'
                ), 0),
                NOW()
            FROM levels lv
        """)
        logger.info("snapshot_liquidity: snapshot de liquidez registrado para todos os níveis.")
    except Exception as e:
        logger.error("Error in snapshot_liquidity: %s", e)


async def snapshot_liquidity_for_level(db, level_id: int) -> None:
    """
    Snapshot pontual de liquidez para um nível específico.

    Chamado de forma fire-and-forget via asyncio.create_task() pelos engines
    logo após qualquer evento que mude a liquidez do nível (desembolso,
    aporte, pagamento). Torna o liquidity_multiplier praticamente em tempo real.
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
                    SELECT SUM(cr.requested_amount)
                    FROM   capture_requests cr
                    WHERE  cr.level_id = lv.id
                      AND  cr.status   = 'in_capture'
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
    Recalcula user_behavior_metrics para todos os usuários com atividade P2P.
    Roda diariamente ou após eventos-chave (pagamento, inadimplência).
    """
    db = get_db()
    if not db:
        return
    try:
        from engine.scoring_engine import recalculate_behavior_metrics as _recalc

        # Usuários com qualquer atividade P2P (como tomador ou credor)
        rows = await db.fetch_all(
            """
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM capture_requests
                UNION
                SELECT creditor_user_id AS user_id FROM creditor_positions
            ) t
            ORDER BY user_id
            """
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

        # Usuários com qualquer atividade P2P
        rows = await db.fetch_all(
            """
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM capture_requests
                UNION
                SELECT creditor_user_id AS user_id FROM creditor_positions
            ) t
            ORDER BY user_id
            """
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


# ── Jobs — Investidor P2P ─────────────────────────────────────────────────────

async def recalculate_investor_metrics() -> None:
    """
    Recalcula métricas históricas de todos os perfis de investidor ativos
    usando dados de creditor_positions (modelo P2P).
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
                    AVG(cp.committed_amount)::NUMERIC(15,2)                     AS avg_amount,
                    AVG(
                        EXTRACT(EPOCH FROM (
                            COALESCE(co.capture_deadline, NOW()) - cp.reserved_at
                        )) / 86400
                    )::INT                                                        AS avg_days,
                    COALESCE(SUM(cp.committed_amount), 0)::NUMERIC(15,2)         AS total_lifetime,
                    COUNT(*) FILTER (WHERE cp.status = 'confirmed')::INT          AS active_count
                FROM creditor_positions cp
                JOIN capture_orders co ON co.id = cp.capture_order_id
                WHERE cp.creditor_user_id = $1
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


async def _expire_stale_p2p_orders() -> None:
    """Expire P2P capture_orders that passed their deadline without reaching the target amount."""
    db = get_db()
    if not db:
        return
    try:
        from engine.p2p_engine import expire_stale_orders
        result = await expire_stale_orders(db)
        expired = result.get("expired", 0) if isinstance(result, dict) else (result or 0)
        if expired:
            logger.info("expire_stale_p2p_orders: %d order(s) expired.", expired)
    except Exception as e:
        logger.error("Error in expire_stale_p2p_orders: %s", e)


async def start_all_jobs() -> None:
    """Inicia todos os jobs periódicos como asyncio tasks."""
    logger.info("Iniciando jobs periódicos da NOTHA...")

    asyncio.create_task(_run_job("check_overdue_installments",  check_overdue_installments,  3600))
    asyncio.create_task(_run_job("snapshot_liquidity",          snapshot_liquidity,           300))

    asyncio.create_task(_run_job("recalculate_behavior_metrics", recalculate_behavior_metrics, 86400))
    asyncio.create_task(_run_job("recalculate_risk_scores",      recalculate_risk_scores,      86400))
    asyncio.create_task(_run_job("recalculate_location_metrics", recalculate_location_metrics, 14400))
    asyncio.create_task(_run_job("reconcile_wallet_caches",      reconcile_wallet_caches,      86400))

    asyncio.create_task(_run_job("recalculate_investor_metrics",  recalculate_investor_metrics, 3600))
    asyncio.create_task(_run_job("expire_investment_offers",      expire_investment_offers,      300))

    # P2P jobs
    asyncio.create_task(_run_job("expire_stale_p2p_orders",      _expire_stale_p2p_orders,    3600))

    logger.info("Jobs periódicos iniciados: 9 jobs ativos.")
