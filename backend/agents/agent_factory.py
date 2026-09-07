"""
Agent factory, replacing the reference repo's agents/agent_factory.py.

The original factory built a fixed set of business-role agents sharing one
memory_manager/approval_manager pair. Yours is different in one important
way: you have multiple PERSONAS (distinct Reddit accounts/characters), and
each persona needs its OWN authenticated RedditClient — Reddit's OAuth
session is single-account per client (see the note in reddit_client.py).
So this factory builds one full agent set + orchestrator PER PERSONA,
not one shared orchestrator for the whole platform.

ApprovalStore, Notifier, and BehaviorProfileManager ARE shared across
personas (platform-wide approval queue, one dashboard feed, one set of
rate-limit rules) — only RedditClient and the agents built on top of it
are per-persona.
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from loguru import logger

from agents.research_agent import ResearchAgent
from agents.content_agent import ContentAgent
from agents.analysis_agent import AnalysisAgent
from integrations.reddit_client import RedditClient, RedditIntegrationConfig
from workflows.hitl_reddit_orchestrator import (
    HITLRedditOrchestrator,
    ApprovalStore,
    Notifier,
    BehaviorProfileManager,
)


@dataclass
class PersonaConfig:
    """One authorized Reddit identity the platform is allowed to act as."""
    persona_id: str                 # also used as workspace_id in the orchestrator
    reddit_client_id: str
    reddit_client_secret: str
    reddit_username: str
    reddit_password: str
    default_subreddit: Optional[str] = None
    personality: Dict[str, Any] = field(default_factory=dict)
    user_agent: str = "reddit-automation-platform/1.0"


class AgentFactory:
    """Builds one authenticated RedditClient + {Research, Content, Analysis}
    agent set + HITLRedditOrchestrator per persona."""

    def __init__(self, approval_store: ApprovalStore, notifier: Notifier,
                 behavior_mgr: BehaviorProfileManager):
        self.approval_store = approval_store
        self.notifier = notifier
        self.behavior_mgr = behavior_mgr
        # persona_id -> {"reddit_client": ..., "agents": {...}, "orchestrator": ...}
        self._personas: Dict[str, Dict[str, Any]] = {}

    async def create_persona(self, config: PersonaConfig) -> HITLRedditOrchestrator:
        """Authenticate this persona's Reddit account, build its agents, and
        return a ready-to-use orchestrator. Call once per persona at startup
        (or on-demand the first time a persona is used)."""
        if config.persona_id in self._personas:
            logger.warning(f"Persona {config.persona_id} already created, returning existing orchestrator")
            return self._personas[config.persona_id]["orchestrator"]

        reddit_client = RedditClient(RedditIntegrationConfig(
            api_key="",       # unused by Reddit's OAuth flow, kept for BaseIntegration compatibility
            base_url="",
            client_id=config.reddit_client_id,
            client_secret=config.reddit_client_secret,
            username=config.reddit_username,
            password=config.reddit_password,
            user_agent=config.user_agent,
        ))
        authenticated = await reddit_client.authenticate()
        if not authenticated:
            raise RuntimeError(f"Failed to authenticate Reddit account for persona {config.persona_id}")

        agents = {
            "Research": ResearchAgent(
                reddit_client=reddit_client,
                personality={**config.personality, "default_subreddit": config.default_subreddit},
            ),
            "Content": ContentAgent(personality=config.personality),
            "Analysis": AnalysisAgent(personality=config.personality),
        }

        orchestrator = HITLRedditOrchestrator(
            agents=agents,
            workspace_id=config.persona_id,
            approval_store=self.approval_store,
            notifier=self.notifier,
            reddit_client=reddit_client,
            behavior_mgr=self.behavior_mgr,
        )

        self._personas[config.persona_id] = {
            "reddit_client": reddit_client,
            "agents": agents,
            "orchestrator": orchestrator,
        }
        logger.info(f"Created persona '{config.persona_id}' with 3 agents and an orchestrator")
        return orchestrator

    def get_orchestrator(self, persona_id: str) -> Optional[HITLRedditOrchestrator]:
        entry = self._personas.get(persona_id)
        return entry["orchestrator"] if entry else None

    def list_personas(self) -> List[str]:
        return list(self._personas.keys())

    def get_agent_statuses(self, persona_id: str) -> Dict[str, Any]:
        entry = self._personas.get(persona_id)
        if not entry:
            return {}
        return {name: agent.get_status() for name, agent in entry["agents"].items()}

    async def shutdown_persona(self, persona_id: str) -> None:
        """Close the Reddit session for a persona. Call on app shutdown or
        when deauthorizing an account."""
        entry = self._personas.pop(persona_id, None)
        if entry:
            await entry["reddit_client"].close()
            logger.info(f"Shut down persona '{persona_id}'")

    async def shutdown_all(self) -> None:
        for persona_id in list(self._personas.keys()):
            await self.shutdown_persona(persona_id)