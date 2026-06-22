"""
Analytics Repository — persists observability and intelligence data.

Tables:
  product_searches    — every search executed (query, location, results)
  tool_execution_logs — every tool call (name, args, result, duration)
  restriction_checks  — every check_restriction call and outcome
  guardrail_events    — when guardrail rejects or corrects a response
  pipeline_events     — one row per message processed by the 4-phase pipeline
"""
import json
import logging
import time
from db.connection import DB

logger = logging.getLogger("notha.db.analytics")


class AnalyticsRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Product searches ──────────────────────────────────────────────────────

    async def log_search(
        self,
        user_id: int,
        phone: str,
        query: str,
        category: str | None,
        search_city: str | None,
        search_neighborhood: str | None,
        results_count: int,
        results_listing_ids: list[int],
        had_fallback: bool = False,
        fallback_level: str | None = None,
        objective: str | None = None,
        intent: str | None = None,
    ) -> None:
        try:
            await self._db.execute(
                """
                INSERT INTO product_searches (
                    user_id, phone, query, category,
                    search_city, search_neighborhood,
                    results_count, results_listing_ids,
                    had_fallback, fallback_level,
                    objective, intent
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                """,
                user_id, phone, query, category,
                search_city, search_neighborhood,
                results_count, json.dumps(results_listing_ids),
                had_fallback, fallback_level,
                objective, intent,
            )
        except Exception as e:
            logger.warning("log_search failed: %s", e)

    # ── Tool execution logs ───────────────────────────────────────────────────

    async def log_tool(
        self,
        phone: str,
        tool_name: str,
        args: dict,
        result_summary: str,
        success: bool = True,
        error_message: str | None = None,
        duration_ms: int | None = None,
        step_number: int | None = None,
        user_id: int | None = None,
    ) -> None:
        try:
            await self._db.execute(
                """
                INSERT INTO tool_execution_logs (
                    user_id, phone, tool_name, args,
                    result_summary, success, error_message,
                    duration_ms, step_number
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                user_id, phone, tool_name,
                json.dumps(args) if args else None,
                result_summary[:800] if result_summary else None,
                success, error_message,
                duration_ms, step_number,
            )
        except Exception as e:
            logger.warning("log_tool failed: %s", e)

    # ── Restriction checks ────────────────────────────────────────────────────

    async def log_restriction_check(
        self,
        phone: str,
        product_description: str,
        result: str,
        restriction_category: str | None = None,
        restriction_reason: str | None = None,
        state: str | None = None,
        municipality: str | None = None,
        intent: str | None = None,
        user_id: int | None = None,
    ) -> None:
        try:
            await self._db.execute(
                """
                INSERT INTO restriction_checks (
                    user_id, phone, product_description, result,
                    restriction_category, restriction_reason,
                    state, municipality, intent
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                user_id, phone, product_description, result,
                restriction_category, restriction_reason,
                state, municipality, intent,
            )
        except Exception as e:
            logger.warning("log_restriction_check failed: %s", e)

    # ── Guardrail events ──────────────────────────────────────────────────────

    async def log_guardrail_event(
        self,
        category: str,
        reason: str,
        was_corrected: bool = False,
        used_fallback: bool = False,
        objective: str | None = None,
        phone: str | None = None,
        user_id: int | None = None,
    ) -> None:
        try:
            await self._db.execute(
                """
                INSERT INTO guardrail_events (
                    user_id, phone, category, reason,
                    was_corrected, used_fallback, objective
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                user_id, phone, category, reason,
                was_corrected, used_fallback, objective,
            )
        except Exception as e:
            logger.warning("log_guardrail_event failed: %s", e)

    # ── Pipeline events ───────────────────────────────────────────────────────

    async def log_pipeline_event(
        self,
        phone: str,
        objective: str | None,
        intent: str | None,
        flow: str | None,
        needs_tools: bool,
        steps_planned: int,
        steps_executed: int,
        outcome: str,
        duration_ms: int | None = None,
        user_id: int | None = None,
    ) -> None:
        try:
            await self._db.execute(
                """
                INSERT INTO pipeline_events (
                    user_id, phone, objective, intent, flow,
                    needs_tools, steps_planned, steps_executed,
                    outcome, duration_ms
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                user_id, phone, objective, intent, flow,
                needs_tools, steps_planned, steps_executed,
                outcome, duration_ms,
            )
        except Exception as e:
            logger.warning("log_pipeline_event failed: %s", e)

    # ── Admin queries ─────────────────────────────────────────────────────────

    async def get_search_stats(self, days: int = 7) -> dict:
        """Returns aggregate search stats for the last N days."""
        try:
            row = await self._db.fetch_one(
                """
                SELECT
                    COUNT(*)                                    AS total_searches,
                    COUNT(*) FILTER (WHERE results_count = 0)  AS zero_result_searches,
                    COUNT(*) FILTER (WHERE had_fallback)        AS fallback_searches,
                    ROUND(AVG(results_count), 2)                AS avg_results,
                    COUNT(DISTINCT user_id)                     AS unique_users
                FROM product_searches
                WHERE created_at >= NOW() - ($1 || ' days')::interval
                """,
                str(days),
            )
            return dict(row) if row else {}
        except Exception as e:
            logger.warning("get_search_stats failed: %s", e)
            return {}

    async def get_top_queries(self, days: int = 7, limit: int = 10) -> list[dict]:
        """Returns most searched queries in the last N days."""
        try:
            rows = await self._db.fetch_all(
                """
                SELECT query, COUNT(*) AS count
                FROM product_searches
                WHERE created_at >= NOW() - ($1 || ' days')::interval
                GROUP BY query
                ORDER BY count DESC
                LIMIT $2
                """,
                str(days), limit,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("get_top_queries failed: %s", e)
            return []

    async def get_restriction_violations(self, days: int = 30) -> list[dict]:
        """Returns RESTRICTED checks in the last N days — compliance monitoring."""
        try:
            rows = await self._db.fetch_all(
                """
                SELECT
                    product_description, restriction_category,
                    restriction_reason, phone, created_at
                FROM restriction_checks
                WHERE result = 'RESTRICTED'
                  AND created_at >= NOW() - ($1 || ' days')::interval
                ORDER BY created_at DESC
                LIMIT 100
                """,
                str(days),
            )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("get_restriction_violations failed: %s", e)
            return []
