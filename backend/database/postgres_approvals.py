"""
PostgreSQL-backed approval store, implementing the ApprovalStore interface
from hitl_reddit_orchestrator.py.

Adapted from the reference repo's backend/database/supabase_db.py — same
"approvals" / "audit_logs" table shape, minus the Supabase REST dependency
and demo-mode branching (you don't need that; just point DATABASE_URL at
your real Postgres instance for local dev too).

Requires: asyncpg  (pip install asyncpg)
"""
import json
import uuid
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import asyncpg
from loguru import logger

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_description TEXT,
    thread_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | timeout
    approved_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_approvals_workspace_status
    ON approvals (workspace_id, status);

CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT NOT NULL,
    user_id TEXT,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    sensitive_flag BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_workspace
    ON audit_logs (workspace_id, created_at DESC);
"""


class PostgresApprovalStore:
    """Implements the ApprovalStore interface used by HITLRedditOrchestrator."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or os.getenv("DATABASE_URL")
        if not self.dsn:
            raise ValueError("DATABASE_URL not set and no dsn provided")
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("PostgresApprovalStore connected and schema ensured")

    async def close(self):
        if self._pool:
            await self._pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if not self._pool:
            raise RuntimeError("PostgresApprovalStore.connect() must be called before use")
        return self._pool

    # --- ApprovalStore interface -----------------------------------

    async def create_approval_request(self, workspace_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        pool = self._require_pool()
        approval_id = uuid.uuid4()
        expires_at = datetime.utcnow() + timedelta(hours=24)

        row = await pool.fetchrow(
            """
            INSERT INTO approvals
                (id, workspace_id, agent_name, action_type, action_description,
                 thread_id, payload, reasoning, status, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9)
            RETURNING *
            """,
            approval_id,
            workspace_id,
            request.get("agent_name", ""),
            request.get("action_type", ""),
            request.get("action_description", ""),
            request["thread_id"],
            json.dumps(request.get("payload", {})),
            request.get("reasoning", ""),
            expires_at,
        )
        result = dict(row)
        result["id"] = str(result["id"])
        logger.info(f"Created approval request {result['id']} for {result['agent_name']}")
        return result

    async def log_audit_event(self, workspace_id: str, user_id: str, agent: str,
                               action: str, details: Dict[str, Any], sensitive: bool = True) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            INSERT INTO audit_logs (workspace_id, user_id, agent, action, details, sensitive_flag)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            workspace_id, user_id, agent, action, json.dumps(details), sensitive,
        )

    # --- Extra methods your approvals API routes will need ------------
    # (not part of the orchestrator's interface, but the same store
    # backs the human-approval endpoints, so add them here rather than
    # opening a second connection pool elsewhere.)

    async def get_pending_approvals(self, workspace_id: str) -> List[Dict[str, Any]]:
        pool = self._require_pool()
        rows = await pool.fetch(
            "SELECT * FROM approvals WHERE workspace_id = $1 AND status = 'pending' ORDER BY created_at",
            workspace_id,
        )
        return [dict(r) | {"id": str(r["id"])} for r in rows]

    async def update_approval_status(self, approval_id: str, status: str,
                                      approved_by: str, reasoning: str = "") -> Dict[str, Any]:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            UPDATE approvals
            SET status = $2, approved_by = $3, reasoning = COALESCE(NULLIF($4, ''), reasoning),
                updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            uuid.UUID(approval_id), status, approved_by, reasoning,
        )
        if not row:
            return {"error": "Approval not found"}
        result = dict(row)
        result["id"] = str(result["id"])
        return result