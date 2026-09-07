"""
Analysis agent — runs last in the pipeline (Research -> Content -> Analysis
per the orchestrator's supervisor routing). Reviews what Research found and
what Content drafted, and flags anything that should push the confidence
score down and force a human look — separate from, and in addition to, the
BehaviorProfileManager's jitter/rate-limit checks.
"""
from typing import Dict, Any, Optional

from agents.base_agent import BaseAgent


class AnalysisAgent(BaseAgent):
    def __init__(self, personality: Optional[Dict[str, Any]] = None):
        super().__init__(name="Analysis", role="Content quality and compliance reviewer",
                          personality=personality)

    async def process_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        previous = task.get("previous_outputs", {})
        research_output = previous.get("Research", {}).get("output", {})
        content_output = previous.get("Content", {}).get("output", {})

        draft = content_output.get("submit_post") or content_output.get("reply_comment") or {}

        response = await self._generate(
            prompt=(
                "Review this drafted Reddit content against the research it was based on. "
                "Flag anything that reads as promotional, off-topic, factually unsupported by "
                "the research, or likely to violate subreddit norms. "
                "Return JSON: {\"risk_flags\": [str], \"quality_score\": float (0-1), "
                "\"recommendation\": \"proceed\" | \"revise\" | \"block\"}"
            ),
            context={"draft": draft, "research_summary": research_output.get("summary")},
        )
        structured = response.get("structured_output", {})
        quality_score = structured.get("quality_score", response.get("confidence", 0.6))

        return {
            "output": {
                "risk_flags": structured.get("risk_flags", []),
                "quality_score": quality_score,
                "recommendation": structured.get("recommendation", "revise"),
            },
            # Analysis's own confidence tracks how sure it is in *its
            # assessment*, not the underlying content's quality — those are
            # deliberately different numbers.
            "confidence": response.get("confidence", 0.6),
        }