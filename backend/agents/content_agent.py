"""
Content agent — drafts the actual Reddit post or comment reply. This is
the one agent whose output the orchestrator treats as approval-gated by
default (see hitl_config["Content"] in hitl_reddit_orchestrator.py) since
it's the only agent whose output can trigger a real Reddit side effect.

IMPORTANT: the output dict keys here ("submit_post" / "reply_comment")
must match exactly what HITLRedditOrchestrator._execute_reddit_side_effects
looks for — that's the contract between this agent and the orchestrator,
not something enforced by types, so don't rename these keys without
updating the orchestrator too.
"""
from typing import Dict, Any, Optional

from agents.base_agent import BaseAgent


class ContentAgent(BaseAgent):
    def __init__(self, personality: Optional[Dict[str, Any]] = None):
        super().__init__(name="Content", role="Reddit content writer", personality=personality)

    async def process_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        brief = task.get("brief", "")
        context = task.get("context", {})
        previous = task.get("previous_outputs", {})
        research = previous.get("Research", {}).get("output", {})
        action_type = context.get("action_type", "submit_post")  # or "reply_comment"

        if action_type == "reply_comment":
            return await self._draft_reply(brief, context, research)
        return await self._draft_post(brief, context, research)

    async def _draft_post(self, brief: str, context: Dict, research: Dict) -> Dict[str, Any]:
        subreddit = context.get("subreddit") or research.get("raw_findings", {}).get("subreddit")
        response = await self._generate(
            prompt=(
                f"Draft a Reddit post for r/{subreddit} on: '{brief}'. "
                f"Use the research summary and suggested angle as grounding. "
                f"Match the natural tone of that subreddit — no promotional language, "
                f"no obvious AI phrasing. "
                f"Return JSON: {{\"title\": str, \"body\": str}}"
            ),
            context={"research_summary": research.get("summary"), "angle": research.get("suggested_angle")},
        )
        structured = response.get("structured_output", {})
        title = structured.get("title", "")
        body = structured.get("body", response.get("content", ""))

        return {
            "output": {
                "submit_post": {
                    "subreddit": subreddit,
                    "title": title,
                    "body": body,
                }
            },
            "confidence": response.get("confidence", 0.6),
        }

    async def _draft_reply(self, brief: str, context: Dict, research: Dict) -> Dict[str, Any]:
        comment_id = context.get("comment_id")
        response = await self._generate(
            prompt=(
                f"Draft a Reddit comment reply for: '{brief}'. "
                f"Keep it conversational and on-topic, matching how a genuine "
                f"community member would respond. "
                f"Return JSON: {{\"body\": str}}"
            ),
            context={"research_summary": research.get("summary")},
        )
        structured = response.get("structured_output", {})
        body = structured.get("body", response.get("content", ""))

        return {
            "output": {
                "reply_comment": {
                    "comment_id": comment_id,
                    "body": body,
                }
            },
            "confidence": response.get("confidence", 0.6),
        }