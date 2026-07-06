"""
NOTHA periodic background jobs — financial platform.

- check_overdue_installments : marks debt installments past due_date as 'overdue' (every hour)
- check_expired_loan_requests: marks loan requests pending > 7 days as 'expired' (every hour)
- snapshot_liquidity         : records a liquidity snapshot per group (every 6 hours)
"""
import asyncio
import logging
from datetime import datetime, timezone

from db.connection import get_db

logger = logging.getLogger("notha.jobs")


async def check_overdue_installments() -> None:
    """Marks any debt installment whose due_date has passed and status is still 'pending'."""
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
            logger.info("check_overdue_installments: %d installment(s) marked overdue.", count)
    except Exception as e:
        logger.error("Error in check_overdue_installments: %s", e)


async def check_expired_loan_requests() -> None:
    """Marks loan requests that have been pending for more than 7 days as 'expired'."""
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
            logger.info("check_expired_loan_requests: %d request(s) expired.", count)
    except Exception as e:
        logger.error("Error in check_expired_loan_requests: %s", e)


async def snapshot_liquidity() -> None:
    """Records a liquidity snapshot for every active group."""
    db = get_db()
    if not db:
        return
    try:
        await db.execute("""
            INSERT INTO liquidity_snapshots
                        (group_id, total_available_investment, total_active_loan_demand, captured_at)
            SELECT
                g.id,
                COALESCE(SUM(w.balance_cache) FILTER (WHERE w.owner_type = 'group'), 0),
                COALESCE((
                    SELECT SUM(d.principal)
                    FROM   debts d
                    JOIN   loan_requests lr ON lr.id = d.loan_request_id
                    WHERE  lr.group_id = g.id
                      AND  d.status   = 'active'
                ), 0),
                NOW()
            FROM groups g
            LEFT JOIN wallets w ON w.owner_id = g.id AND w.owner_type = 'group'
            WHERE g.status = 'active'
            GROUP BY g.id
        """)
        logger.info("snapshot_liquidity: liquidity snapshot recorded.")
    except Exception as e:
        logger.error("Error in snapshot_liquidity: %s", e)


async def _run_job(name: str, coro_fn, interval_seconds: int) -> None:
    while True:
        try:
            await coro_fn()
        except Exception as e:
            logger.error("Job '%s' failed unexpectedly: %s", name, e)
        await asyncio.sleep(interval_seconds)


async def start_all_jobs() -> None:
    """Starts all periodic jobs as asyncio tasks."""
    logger.info("Starting NOTHA periodic jobs...")
    asyncio.create_task(_run_job("check_overdue_installments",  check_overdue_installments,  3600))
    asyncio.create_task(_run_job("check_expired_loan_requests", check_expired_loan_requests, 3600))
    asyncio.create_task(_run_job("snapshot_liquidity",          snapshot_liquidity,          21600))
    logger.info("Periodic jobs started.")
