"""
BehaviorProfileManager, implementing the BehaviorProfileManager interface
from hitl_reddit_orchestrator.py.

No reference code exists for this anywhere in the source repo — it's the
piece specific to your compliance requirement (no vote manipulation, ban
evasion, or automated-looking activity patterns). Backed by Redis since
that's already in your stack and this needs fast counters/timestamps, not
durable relational storage.

Enforces three things per persona (workspace_id), independent of whatever
confidence score the Content agent reports:
  1. Minimum jittered gap between actions of the same type (no fixed-interval
     posting cadence)
  2. Hourly and daily action caps per type
  3. An "active hours" window, so a persona doesn't post at 4am every day

NOTE ON THE ORCHESTRATOR CONTRACT: the stub in hitl_reddit_orchestrator.py
declares check(self, agent_name, action_type) — but rate limits must be
scoped per PERSONA (workspace_id), not per agent role, since "Content" is
the same role name across every persona. The orchestrator's
_hitl_checkpoint_node needs a one-line update to pass workspace_id through:

    profile_check = await self.behavior_mgr.check(
        state["workspace_id"], agent_name, action_type
    )

and after a successful Reddit side effect, _execute_reddit_side_effects (or
the caller in _side_effects_executor_node) should call:

    await self.behavior_mgr.record_action(state["workspace_id"], action_type)

so the limits actually track real activity. See the bottom of this file
for the exact patch.
"""
import json
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Any, Optional

import redis.asyncio as redis
from loguru import logger


@dataclass
class BehaviorProfile:
    """Per-persona activity limits. Defaults model a moderately active,
    clearly-human-paced account — tune per persona via PersonaConfig if
    some accounts should look more or less active than others."""
    min_interval_seconds: int = 180          # floor gap between same-type actions
    jitter_seconds: int = 420                # random extra 0..jitter added on top of the floor
    max_actions_per_hour: int = 4
    max_actions_per_day: int = 15
    active_hour_start: int = 7               # persona's local hour, 0-23
    active_hour_end: int = 23

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "BehaviorProfile":
        return cls(**json.loads(raw))


class BehaviorProfileManager:
    """Implements the BehaviorProfileManager interface used by
    HITLRedditOrchestrator, keyed by (workspace_id/persona_id, action_type)."""

    def __init__(self, redis_url: str, default_profile: Optional[BehaviorProfile] = None):
        self.redis_url = redis_url
        self.default_profile = default_profile or BehaviorProfile()
        self._redis: Optional[redis.Redis] = None

    async def connect(self):
        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        await self._redis.ping()
        logger.info("BehaviorProfileManager connected to Redis")

    async def close(self):
        if self._redis:
            await self._redis.aclose()

    def _require_redis(self) -> redis.Redis:
        if not self._redis:
            raise RuntimeError("BehaviorProfileManager.connect() must be called before use")
        return self._redis

    # --- Profile configuration ------------------------------------------

    async def set_profile(self, workspace_id: str, profile: BehaviorProfile) -> None:
        r = self._require_redis()
        await r.set(f"behavior:profile:{workspace_id}", profile.to_json())

    async def get_profile(self, workspace_id: str) -> BehaviorProfile:
        r = self._require_redis()
        raw = await r.get(f"behavior:profile:{workspace_id}")
        return BehaviorProfile.from_json(raw) if raw else self.default_profile

    # --- BehaviorProfileManager interface --------------------------------

    async def check(self, workspace_id: str, agent_name: str, action_type: str) -> Dict[str, Any]:
        """Called from the orchestrator's hitl_checkpoint before an action
        is allowed to auto-execute. Does NOT record the action — that
        happens separately in record_action() once the action actually
        goes through, so a rejected/never-executed action doesn't consume
        the persona's activity budget."""
        r = self._require_redis()
        profile = await self.get_profile(workspace_id)
        now = datetime.now()

        # 1. Active-hours window
        if not (profile.active_hour_start <= now.hour < profile.active_hour_end):
            return {
                "within_limits": False,
                "reason": f"Outside active hours ({profile.active_hour_start}:00-{profile.active_hour_end}:00)",
            }

        # 2. Minimum jittered gap since the last action of this type
        last_ts_raw = await r.get(f"behavior:last_action:{workspace_id}:{action_type}")
        if last_ts_raw:
            elapsed = time.time() - float(last_ts_raw)
            required_gap = profile.min_interval_seconds + random.uniform(0, profile.jitter_seconds)
            if elapsed < required_gap:
                return {
                    "within_limits": False,
                    "reason": f"Too soon since last {action_type} "
                              f"({elapsed:.0f}s elapsed, need ~{required_gap:.0f}s)",
                }

        # 3. Hourly / daily caps
        hour_key = f"behavior:count:hour:{workspace_id}:{action_type}:{now.strftime('%Y%m%d%H')}"
        day_key = f"behavior:count:day:{workspace_id}:{action_type}:{now.strftime('%Y%m%d')}"
        hour_count = int(await r.get(hour_key) or 0)
        day_count = int(await r.get(day_key) or 0)

        if hour_count >= profile.max_actions_per_hour:
            return {
                "within_limits": False,
                "reason": f"Hourly cap reached ({hour_count}/{profile.max_actions_per_hour} {action_type})",
            }
        if day_count >= profile.max_actions_per_day:
            return {
                "within_limits": False,
                "reason": f"Daily cap reached ({day_count}/{profile.max_actions_per_day} {action_type})",
            }

        return {"within_limits": True, "reason": ""}

    async def record_action(self, workspace_id: str, action_type: str) -> None:
        """Call this after a Reddit action actually executes (i.e. from the
        orchestrator's side-effects step, on success only), so the
        activity budget reflects what really happened."""
        r = self._require_redis()
        now = datetime.now()

        await r.set(f"behavior:last_action:{workspace_id}:{action_type}", time.time())

        hour_key = f"behavior:count:hour:{workspace_id}:{action_type}:{now.strftime('%Y%m%d%H')}"
        day_key = f"behavior:count:day:{workspace_id}:{action_type}:{now.strftime('%Y%m%d')}"
        async with r.pipeline() as pipe:
            pipe.incr(hour_key)
            pipe.expire(hour_key, 3600 * 2)
            pipe.incr(day_key)
            pipe.expire(day_key, 3600 * 26)
            await pipe.execute()

        logger.info(f"Recorded {action_type} action for persona {workspace_id}")


# ---------------------------------------------------------------------------
# Patch needed in workflows/hitl_reddit_orchestrator.py — apply both edits:
#
# 1. In _hitl_checkpoint_node, inside the `for action_type in action_keys:`
#    loop, change:
#        profile_check = await self.behavior_mgr.check(agent_name, action_type)
#    to:
#        profile_check = await self.behavior_mgr.check(
#            state["workspace_id"], agent_name, action_type
#        )
#
# 2. In _execute_reddit_side_effects, after each successful client call,
#    record it:
#        if "submit_post" in output_data:
#            p = output_data["submit_post"]
#            result = await self.reddit_client.submit_post(p["subreddit"], p["title"], p["body"])
#            if result.get("status") == "success":
#                await self.behavior_mgr.record_action(workspace_id, "submit_post")
#        if "reply_comment" in output_data:
#            r_ = output_data["reply_comment"]
#            result = await self.reddit_client.reply_comment(r_["comment_id"], r_["body"])
#            if result.get("status") == "success":
#                await self.behavior_mgr.record_action(workspace_id, "reply_comment")
#    (this also means _execute_reddit_side_effects needs workspace_id passed
#    in — thread it through from _side_effects_executor_node the same way
#    output_data already is)
# ---------------------------------------------------------------------------