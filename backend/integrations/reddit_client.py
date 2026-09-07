"""
Reddit API client, implementing the RedditClient interface from
hitl_reddit_orchestrator.py.

Follows the reference repo's integrations/base_integration.py pattern
(IntegrationConfig + authenticate()/health_check()) since that's the shape
every other integration in the repo (hubspot_client.py, slack_client.py)
uses — but there's no Reddit equivalent to adapt from, this is net new.

Uses asyncpraw (async PRAW) since the orchestrator and everything calling
into this client is async end to end.

    pip install asyncpraw

Reddit app setup: create a "script" type app at
https://www.reddit.com/prefs/apps to get client_id/client_secret, then
authenticate with the Reddit account each persona/agent is allowed to post
as (username/password) or a refresh token per persona if you're managing
multiple authorized accounts — see the note on multi-persona auth below.
"""
from typing import Dict, Any, Optional

import asyncpraw
from pydantic import BaseModel
from loguru import logger

from integrations.base_integration import BaseIntegration, IntegrationConfig


class RedditIntegrationConfig(IntegrationConfig):
    """Reddit-specific config, extending the shared IntegrationConfig shape.
    api_key/base_url from the parent class are unused here (Reddit's OAuth
    flow doesn't fit that shape) but kept so this still satisfies
    BaseIntegration's __init__ signature without special-casing it."""
    client_id: str
    client_secret: str
    username: str
    password: str
    user_agent: str = "reddit-automation-platform/1.0"


class RedditClient(BaseIntegration):
    """Implements the RedditClient interface used by HITLRedditOrchestrator.

    One instance = one authorized Reddit account. If you're running
    multiple personas across multiple Reddit accounts (per your platform
    brief), construct one RedditClient per persona and route agent output
    to the right instance in the orchestrator's side-effects step rather
    than trying to make a single client multi-account — Reddit's OAuth
    session is inherently single-account per client.
    """

    def __init__(self, config: RedditIntegrationConfig):
        super().__init__(config)
        self.config: RedditIntegrationConfig = config
        self._reddit: Optional[asyncpraw.Reddit] = None

    async def authenticate(self) -> bool:
        try:
            self._reddit = asyncpraw.Reddit(
                client_id=self.config.client_id,
                client_secret=self.config.client_secret,
                username=self.config.username,
                password=self.config.password,
                user_agent=self.config.user_agent,
            )
            me = await self._reddit.user.me()
            logger.info(f"Reddit client authenticated as u/{me}")
            return True
        except Exception as e:
            logger.error(f"Reddit authentication failed: {e}")
            return False

    async def health_check(self) -> Dict[str, Any]:
        if not self._reddit:
            return {"status": "not_authenticated"}
        try:
            me = await self._reddit.user.me()
            return {"status": "ok", "authenticated_as": str(me)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _require_reddit(self) -> asyncpraw.Reddit:
        if not self._reddit:
            raise RuntimeError("Call authenticate() before using RedditClient")
        return self._reddit

    # --- RedditClient interface (used by the orchestrator's side-effects step) --

    async def submit_post(self, subreddit: str, title: str, body: str) -> Dict[str, Any]:
        reddit = self._require_reddit()
        try:
            sub = await reddit.subreddit(subreddit)
            submission = await sub.submit(title=title, selftext=body)
            logger.info(f"Submitted post {submission.id} to r/{subreddit}")
            return {
                "status": "success",
                "post_id": submission.id,
                "url": f"https://reddit.com{submission.permalink}",
            }
        except Exception as e:
            logger.error(f"Failed to submit post to r/{subreddit}: {e}")
            return {"status": "error", "error": str(e)}

    async def reply_comment(self, comment_id: str, body: str) -> Dict[str, Any]:
        reddit = self._require_reddit()
        try:
            comment = await reddit.comment(id=comment_id)
            reply = await comment.reply(body=body)
            logger.info(f"Replied to comment {comment_id} with {reply.id}")
            return {
                "status": "success",
                "reply_id": reply.id,
                "url": f"https://reddit.com{reply.permalink}",
            }
        except Exception as e:
            logger.error(f"Failed to reply to comment {comment_id}: {e}")
            return {"status": "error", "error": str(e)}

    # --- Read-only helpers your Research agent will want ------------------
    # Not part of the orchestrator's RedditClient interface (that only
    # covers write actions gated by approval), but the same authenticated
    # client is what your Research agent should use for read-only lookups,
    # so it's included here rather than duplicating auth elsewhere.

    async def search_subreddit(self, subreddit: str, query: str, limit: int = 10) -> Dict[str, Any]:
        reddit = self._require_reddit()
        try:
            sub = await reddit.subreddit(subreddit)
            results = []
            async for post in sub.search(query, limit=limit):
                results.append({
                    "id": post.id,
                    "title": post.title,
                    "score": post.score,
                    "num_comments": post.num_comments,
                    "url": f"https://reddit.com{post.permalink}",
                })
            return {"status": "success", "results": results}
        except Exception as e:
            logger.error(f"Failed to search r/{subreddit} for '{query}': {e}")
            return {"status": "error", "error": str(e)}

    async def close(self):
        if self._reddit:
            await self._reddit.close()