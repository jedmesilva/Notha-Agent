"""SessionRepository — manages user sessions and re-authentication flows."""
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from db.connection import DB

logger = logging.getLogger("notha.repo.sessions")


class SessionRepository:
    SESSION_ACTIVE_THRESHOLD_DAYS   = 7    # no re-auth below this
    SESSION_CPF_THRESHOLD_DAYS      = 30   # CPF tier up to here
    SESSION_SELFIE_THRESHOLD_DAYS   = 90   # selfie tier up to here
    # > 90 days → link tier
    VERIFICATION_TOKEN_TTL_MINUTES  = 15
    MAX_REAUTH_ATTEMPTS             = 3

    def __init__(self, db: DB):
        self._db = db

    async def get_session(self, phone: str) -> dict | None:
        """Returns the current active or pending_reauth session for a phone."""
        row = await self._db.fetch_one(
            """
            SELECT * FROM sessions
            WHERE phone = $1
              AND status IN ('active', 'pending_reauth')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            phone,
        )
        return dict(row) if row else None

    async def create_session(self, user_id: int, phone: str) -> dict:
        """Creates a new active session. Replaces any existing active/pending session."""
        await self._db.execute(
            "UPDATE sessions SET status = 'revoked' WHERE phone = $1 AND status IN ('active', 'pending_reauth')",
            phone,
        )
        row = await self._db.fetch_one(
            "INSERT INTO sessions (user_id, phone, status) VALUES ($1, $2, 'active') RETURNING *",
            user_id, phone,
        )
        return dict(row) if row else {}

    async def touch(self, phone: str) -> None:
        """Updates last_activity_at for the active session — call on every successful interaction."""
        await self._db.execute(
            "UPDATE sessions SET last_activity_at = NOW() WHERE phone = $1 AND status = 'active'",
            phone,
        )

    async def start_reauth(self, phone: str, tier: str) -> dict:
        """Transitions active session to pending_reauth with the given tier."""
        row = await self._db.fetch_one(
            """
            UPDATE sessions
            SET status = 'pending_reauth', reauth_tier = $2, reauth_attempts = 0
            WHERE phone = $1 AND status = 'active'
            RETURNING *
            """,
            phone, tier,
        )
        return dict(row) if row else {}

    async def complete_reauth(self, phone: str) -> None:
        """Transitions pending_reauth session back to active after successful re-auth."""
        await self._db.execute(
            """
            UPDATE sessions
            SET status = 'active',
                reauth_tier = NULL,
                reauth_attempts = 0,
                reauthed_at = NOW(),
                last_activity_at = NOW()
            WHERE phone = $1 AND status = 'pending_reauth'
            """,
            phone,
        )

    async def increment_reauth_attempts(self, phone: str) -> int:
        """Increments failed re-auth counter. Returns new count."""
        row = await self._db.fetch_one(
            """
            UPDATE sessions
            SET reauth_attempts = reauth_attempts + 1
            WHERE phone = $1 AND status = 'pending_reauth'
            RETURNING reauth_attempts
            """,
            phone,
        )
        return row["reauth_attempts"] if row else 0

    async def revoke(self, phone: str) -> None:
        """Revokes all sessions for a phone (too many failed attempts or admin action)."""
        await self._db.execute(
            "UPDATE sessions SET status = 'revoked' WHERE phone = $1",
            phone,
        )
        logger.warning("Session revoked for phone=%s", phone)

    def inactivity_days(self, session: dict) -> float:
        """Returns days elapsed since last_activity_at."""
        last = session.get("last_activity_at")
        if not last:
            return 0.0
        now = datetime.now(timezone.utc)
        if hasattr(last, "tzinfo") and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() / 86400

    def determine_tier(self, session: dict) -> str | None:
        """Returns re-auth tier based on inactivity, or None if session is still valid."""
        days = self.inactivity_days(session)
        if days < self.SESSION_ACTIVE_THRESHOLD_DAYS:
            return None
        if days < self.SESSION_CPF_THRESHOLD_DAYS:
            return "cpf"
        if days < self.SESSION_SELFIE_THRESHOLD_DAYS:
            return "selfie"
        return "link"

    async def create_verification_token(
        self, session_id: int, user_id: int, phone: str
    ) -> str:
        """Creates a pending_verification record and returns the token.

        Expires any previous pending verification for this phone before inserting
        the new one — avoids ON CONFLICT with partial index (PostgreSQL limitation).
        """
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.VERIFICATION_TOKEN_TTL_MINUTES
        )
        # Expire any existing pending verification for this phone
        await self._db.execute(
            "UPDATE pending_verifications SET status = 'expired' WHERE phone = $1 AND status = 'pending'",
            phone,
        )
        # Insert fresh verification record
        await self._db.execute(
            """
            INSERT INTO pending_verifications (session_id, user_id, phone, token, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            session_id, user_id, phone, token, expires_at,
        )
        return token

    async def get_pending_verification(self, token: str) -> dict | None:
        """Returns a pending_verification if valid and not yet expired."""
        row = await self._db.fetch_one(
            """
            SELECT * FROM pending_verifications
            WHERE token = $1
              AND status = 'pending'
              AND expires_at > NOW()
            """,
            token,
        )
        return dict(row) if row else None

    async def complete_verification(
        self, token: str, success: bool, result: dict
    ) -> None:
        """Records the outcome of a link-based facial verification."""
        status = "completed" if success else "failed"
        await self._db.execute(
            """
            UPDATE pending_verifications
            SET status = $2, result = $3::jsonb
            WHERE token = $1
            """,
            token, status, json.dumps(result),
        )
