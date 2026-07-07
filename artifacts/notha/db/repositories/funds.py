"""
FundRepository — funds, fund_policies, fund_users.

Funds are credit pools from which borrowers take loans.
Investors have NO direct relationship with funds.

Lifecycle:
  1. Admin creates a fund and sets its policies.
  2. System evaluates a user against fund policies → adds to fund_users if eligible.
  3. User (borrower) requests a loan referencing their fund (loan_requests.fund_id).
  4. Approved loan generates a debt → investment_opportunity (opportunity.fund_id).
  5. Investors see opportunities filtered by the borrower's segment; invest independently.
"""
import logging
from datetime import datetime, timezone
from db.connection import DB

logger = logging.getLogger("notha.funds")


class FundRepository:
    def __init__(self, db: DB):
        self._db = db

    # ── Fund CRUD ──────────────────────────────────────────────────────────────

    async def create(
        self,
        name: str,
        description: str | None = None,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO funds (name, description, status)
            VALUES ($1, $2, 'active')
            RETURNING id
            """,
            name, description,
        )

    async def get_by_id(self, fund_id: int) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM funds WHERE id = $1", fund_id
        )
        return dict(row) if row else None

    async def list_all(self, status: str | None = None) -> list[dict]:
        if status:
            rows = await self._db.fetch_all(
                "SELECT * FROM funds WHERE status = $1 ORDER BY name ASC", status
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM funds ORDER BY name ASC"
            )
        return [dict(r) for r in rows]

    async def update_status(self, fund_id: int, status: str) -> None:
        await self._db.execute(
            """
            UPDATE funds
               SET status = $1, updated_at = NOW()
             WHERE id = $2
            """,
            status, fund_id,
        )

    # ── Fund policies ──────────────────────────────────────────────────────────

    async def add_policy(
        self,
        fund_id: int,
        criteria_type: str,
        criteria_value: str = "",
        operator: str = "eq",
        logic_group: int = 0,
    ) -> int:
        return await self._db.fetch_val(
            """
            INSERT INTO fund_policies
                (fund_id, criteria_type, criteria_value, operator, logic_group)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            fund_id, criteria_type, criteria_value, operator, logic_group,
        )

    async def remove_policy(self, policy_id: int) -> None:
        await self._db.execute(
            "DELETE FROM fund_policies WHERE id = $1", policy_id
        )

    async def replace_policies(
        self,
        fund_id: int,
        policies: list[dict],
    ) -> None:
        """
        Replace all policies for a fund atomically.
        Each dict must have: criteria_type, criteria_value, operator, logic_group.
        """
        async with self._db.atomic():
            await self._db.execute(
                "DELETE FROM fund_policies WHERE fund_id = $1", fund_id
            )
            for p in policies:
                await self._db.execute(
                    """
                    INSERT INTO fund_policies
                        (fund_id, criteria_type, criteria_value, operator, logic_group)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    fund_id,
                    p["criteria_type"],
                    p.get("criteria_value", ""),
                    p.get("operator", "eq"),
                    p.get("logic_group", 0),
                )

    async def get_policies(self, fund_id: int) -> list[dict]:
        rows = await self._db.fetch_all(
            """
            SELECT * FROM fund_policies
             WHERE fund_id = $1
             ORDER BY logic_group ASC, id ASC
            """,
            fund_id,
        )
        return [dict(r) for r in rows]

    # ── Eligibility check ──────────────────────────────────────────────────────

    async def check_eligibility(self, fund_id: int, user_id: int) -> bool:
        """
        Evaluate fund_policies for the given user.

        Logic:
          - Rows in the same logic_group are AND-ed.
          - Different logic_groups are OR-ed.
          - A fund with no policies accepts anyone (open fund).
          - A 'manual_only' policy always returns False (admin must add manually).

        Returns True if the user satisfies at least one complete logic_group.
        """
        policies = await self.get_policies(fund_id)
        if not policies:
            return True

        user_row = await self._db.fetch_one(
            "SELECT current_level, identity_status, created_at FROM users WHERE id = $1",
            user_id,
        )
        if not user_row:
            return False

        user_segments_rows = await self._db.fetch_all(
            """
            SELECT s.name, s.id::text AS segment_id
              FROM user_segments us
              JOIN segments s ON s.id = us.segment_id
             WHERE us.user_id = $1 AND s.status = 'active'
            """,
            user_id,
        )
        user_segment_names = {r["name"].lower() for r in user_segments_rows}
        user_segment_ids   = {r["segment_id"] for r in user_segments_rows}

        account_age_days = (
            datetime.now(timezone.utc) - user_row["created_at"]
        ).days

        groups: dict[int, list[dict]] = {}
        for p in policies:
            groups.setdefault(p["logic_group"], []).append(p)

        for group_policies in groups.values():
            if self._evaluate_group(
                group_policies,
                user_row["current_level"],
                user_row["identity_status"],
                account_age_days,
                user_segment_names,
                user_segment_ids,
            ):
                return True

        return False

    def _evaluate_group(
        self,
        policies: list[dict],
        current_level: int,
        identity_status: str,
        account_age_days: int,
        segment_names: set[str],
        segment_ids: set[str],
    ) -> bool:
        """All policies in the group must pass (AND logic)."""
        for p in policies:
            ctype  = p["criteria_type"]
            cvalue = p["criteria_value"]
            op     = p["operator"]

            if ctype == "manual_only":
                return False

            elif ctype == "min_level":
                if not self._compare(current_level, int(cvalue), op):
                    return False

            elif ctype == "segment_membership":
                needle = cvalue.lower()
                if needle not in segment_names and cvalue not in segment_ids:
                    return False

            elif ctype == "min_kyc_tier":
                kyc_map = {"none": 0, "basic": 1, "full": 2}
                user_tier = kyc_map.get(identity_status, 0)
                if not self._compare(user_tier, int(cvalue), op):
                    return False

            elif ctype == "min_account_age_days":
                if not self._compare(account_age_days, int(cvalue), op):
                    return False

        return True

    @staticmethod
    def _compare(user_val: int | float, threshold: int | float, op: str) -> bool:
        if op == "eq":
            return user_val == threshold
        if op == "gte":
            return user_val >= threshold
        if op == "lte":
            return user_val <= threshold
        return False

    # ── Fund users (allocation / deallocation) ─────────────────────────────────

    async def add_user(
        self,
        fund_id: int,
        user_id: int,
        added_by: str = "system",
        bypass_policies: bool = False,
    ) -> dict:
        """
        Add user to fund.
        - Checks eligibility via policies unless bypass_policies=True.
        - If already active in fund, returns existing record (idempotent).
        - If previously removed, creates a new active row (history preserved).

        Returns: {"ok": bool, "reason": str, "fund_user_id": int | None}
        """
        existing = await self._db.fetch_one(
            """
            SELECT id FROM fund_users
             WHERE fund_id = $1 AND user_id = $2 AND removed_at IS NULL
            """,
            fund_id, user_id,
        )
        if existing:
            return {"ok": True, "reason": "already_active", "fund_user_id": existing["id"]}

        if not bypass_policies:
            eligible = await self.check_eligibility(fund_id, user_id)
            if not eligible:
                return {"ok": False, "reason": "not_eligible", "fund_user_id": None}

        fund_user_id = await self._db.fetch_val(
            """
            INSERT INTO fund_users (fund_id, user_id, added_by)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            fund_id, user_id, added_by,
        )
        logger.info("user %s added to fund %s by %s", user_id, fund_id, added_by)
        return {"ok": True, "reason": "added", "fund_user_id": fund_user_id}

    async def remove_user(
        self,
        fund_id: int,
        user_id: int,
        reason: str | None = None,
    ) -> bool:
        """
        Soft-delete: sets removed_at on the active fund_users row.
        Returns True if a row was updated, False if user was not active.
        """
        result = await self._db.execute(
            """
            UPDATE fund_users
               SET removed_at = NOW(), removal_reason = $3
             WHERE fund_id = $1 AND user_id = $2 AND removed_at IS NULL
            """,
            fund_id, user_id, reason,
        )
        updated = result == "UPDATE 1"
        if updated:
            logger.info("user %s removed from fund %s: %s", user_id, fund_id, reason)
        return updated

    async def get_user_funds(self, user_id: int, active_only: bool = True) -> list[dict]:
        """Return all funds a user belongs to (or has belonged to)."""
        if active_only:
            rows = await self._db.fetch_all(
                """
                SELECT fu.*, f.name AS fund_name, f.status AS fund_status
                  FROM fund_users fu
                  JOIN funds f ON f.id = fu.fund_id
                 WHERE fu.user_id = $1 AND fu.removed_at IS NULL
                 ORDER BY fu.added_at DESC
                """,
                user_id,
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT fu.*, f.name AS fund_name, f.status AS fund_status
                  FROM fund_users fu
                  JOIN funds f ON f.id = fu.fund_id
                 WHERE fu.user_id = $1
                 ORDER BY fu.added_at DESC
                """,
                user_id,
            )
        return [dict(r) for r in rows]

    async def list_fund_users(
        self,
        fund_id: int,
        active_only: bool = True,
        limit: int = 200,
    ) -> list[dict]:
        """Return users in a fund with basic user info."""
        where = "fu.fund_id = $1"
        if active_only:
            where += " AND fu.removed_at IS NULL"
        rows = await self._db.fetch_all(
            f"""
            SELECT fu.*, u.full_name, u.nickname, u.current_level, u.identity_status
              FROM fund_users fu
              JOIN users u ON u.id = fu.user_id
             WHERE {where}
             ORDER BY fu.added_at DESC
             LIMIT $2
            """,
            fund_id, limit,
        )
        return [dict(r) for r in rows]

    async def is_user_active(self, fund_id: int, user_id: int) -> bool:
        row = await self._db.fetch_one(
            """
            SELECT 1 FROM fund_users
             WHERE fund_id = $1 AND user_id = $2 AND removed_at IS NULL
            """,
            fund_id, user_id,
        )
        return row is not None

    # ── Full profile ───────────────────────────────────────────────────────────

    async def get_full_profile(self, fund_id: int) -> dict | None:
        fund = await self.get_by_id(fund_id)
        if not fund:
            return None
        policies     = await self.get_policies(fund_id)
        members      = await self.list_fund_users(fund_id, active_only=True, limit=50)
        member_count = await self._db.fetch_val(
            "SELECT COUNT(*) FROM fund_users WHERE fund_id = $1 AND removed_at IS NULL",
            fund_id,
        ) or 0
        return {
            "fund":         fund,
            "policies":     policies,
            "member_count": member_count,
            "members":      members,
        }
