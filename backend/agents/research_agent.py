"""
Research agent — gathers subreddit/topic context for the Content agent to
draft from. Read-only by design, so it sits in the low-approval-bar tier
of the orchestrator's hitl_config (see hitl_reddit_orchestrator.py).
"""
from typing import Dict, Any, Optional

from agents.base_agent import BaseAgent
from integrations.reddit_client import RedditClient


class ResearchAgent(BaseAgent):
    def __init__(self, reddit_client: RedditClient, personality: Optional[Dict[str, Any]] = None):
        super().__init__(name="Research", role="Reddit research analyst", personality=personality)
        self.reddit_client = reddit_client

    async def process_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        brief = task.get("brief", "")
        context = task.get("context", {})
        subreddit = context.get("subreddit") or self.personality.get("default_subreddit")

        findings = {"query": brief, "subreddit": subreddit, "posts": []}
        if subreddit:
            search_result = await self.reddit_client.search_subreddit(subreddit, brief, limit=10)
            if search_result.get("status") == "success":
                findings["posts"] = search_result["results"]
            else:
                findings["search_error"] = search_result.get("error")

        # Ask the LLM to synthesize the raw findings into a research brief
        # the Content agent can draft from.
        summary = await self._generate(
            prompt=(
                f"Summarize the following Reddit research for the brief: '{brief}'. "
                f"Identify themes, tone, and any gaps a new post/comment should address. "
                f"Return JSON: {{\"summary\": str, \"themes\": [str], \"suggested_angle\": str}}"
            ),
            context=findings,
        )

        structured = summary.get("structured_output", {})
        return {
            "output": {
                "raw_findings": findings,
                "summary": structured.get("summary", summary.get("content", "")),
                "themes": structured.get("themes", []),
                "suggested_angle": structured.get("suggested_angle", ""),
            },
            "confidence": summary.get("confidence", 0.6),
        }